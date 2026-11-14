"""
Tests for the AI care planner. No API key needed: Gemini is replaced by a
scripted fake planner, so these tests check the agent loop and guardrails,
not the model.
"""

import json
from types import SimpleNamespace

import pytest

from pawpal_ai.agent import CarePlannerAgent, apply_plan
from pawpal_ai.guardrails import check_request, in_window, validate_plan
from pawpal_ai.models import CareRequest, PetProfile, ProposedTask, life_stage_for
from pawpal_ai.planners import (ClaudePlanner, RulePlanner, _parse_busy_windows, available_ai_planner,
                                make_planner)
from pawpal_ai.retriever import KnowledgeBase
from pawpal_system import Owner, Pet, Task


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase.load()


def make_agent(planner, kb, tmp_path):
    return CarePlannerAgent(planner, kb, log_dir=str(tmp_path), save_traces=True)


class ScriptedPlanner:
    """Fake LLM planner: returns canned parse/draft/revise results and records calls."""
    name = "scripted"

    def __init__(self, request, drafts):
        self.request = request
        self.drafts = list(drafts)  # first is the draft, the rest are revisions
        self.calls = []

    def parse_request(self, text):
        self.calls.append("parse")
        return self.request

    def draft_plan(self, request, context):
        self.calls.append("draft")
        self.context = context
        return self.drafts.pop(0)

    def revise_plan(self, request, context, tasks, issues):
        self.calls.append("revise")
        self.issues_seen = issues
        return self.drafts.pop(0) if self.drafts else tasks


def puppy_request():
    return CareRequest([PetProfile("Biscuit", "dog", 0.3)], [["09:00", "17:00"]])


# --- Retrieval --------------------------------------------------------------

def test_knowledge_base_loads_chunks_routines_and_sources(kb):
    assert len(kb.chunks) > 20
    assert {"dog-puppy", "cat-senior", "vet-safety"} <= set(kb.routines)
    assert all(kb.sources[d] for d in kb.routines)


def test_retrieval_respects_species_and_prefers_life_stage(kb):
    results = kb.retrieve("senior cat kidney water feeding", species="cat", life_stage="senior")
    assert results, "expected matches"
    assert results[0].chunk.doc_id == "cat-senior"
    assert all(r.chunk.species in ("cat", "any") for r in results)


def test_retrieval_returns_nothing_for_unrelated_query(kb):
    assert kb.retrieve("quantum stock market poetry", species="dog") == []


def test_life_stage_mapping():
    assert life_stage_for("dog", 0.5) == "puppy"
    assert life_stage_for("cat", 0.5) == "kitten"
    assert life_stage_for("dog", 8) == "senior"
    assert life_stage_for("cat", 8) == "adult"
    assert life_stage_for("cat", None) == "adult"


# --- Input guardrail ----------------------------------------------------------

@pytest.mark.parametrize("text,status", [
    ("", "empty"),
    ("   ", "empty"),
    ("x" * 2000 + " dog", "too_long"),
    ("Write a poem about the stock market", "off_topic"),
    ("My dog ate chocolate an hour ago", "emergency"),
    ("my cat is having a seizure", "emergency"),
    ("I have a 2 year old dog named Max", "ok"),
])
def test_check_request_statuses(text, status):
    assert check_request(text).status == status


def test_medication_question_is_flagged_not_refused():
    check = check_request("How many mg of Benadryl can I give my dog?")
    assert check.ok and "medical_question" in check.flags


# --- Plan guardrail -----------------------------------------------------------

def test_validate_plan_catches_each_rule():
    request = puppy_request()
    tasks = [
        ProposedTask("Biscuit", "Breakfast", "07:00", "daily", "dog-puppy#feeding"),        # ok
        ProposedTask("Biscuit", "Lunch", "12:00", "daily", "dog-puppy#feeding"),            # busy hours
        ProposedTask("Rex", "Walk", "18:00", "daily", "dog-puppy#exercise-and-play"),       # unknown pet
        ProposedTask("Biscuit", "Play", "25:00", "daily", "dog-puppy#exercise-and-play"),   # bad time
        ProposedTask("Biscuit", "Night walk", "03:00", "daily", "dog-puppy#feeding"),       # asleep
        ProposedTask("Biscuit", "Brush", "19:00", "hourly", "dog-puppy#feeding"),           # bad frequency
        ProposedTask("Biscuit", "Cuddle", "19:10", "daily", "made-up#source"),              # ungrounded
        ProposedTask("Biscuit", "Give 5 mg Benadryl", "19:20", "daily", "dog-puppy#feeding"),  # dose
        ProposedTask("Biscuit", "Breakfast", "07:00", "daily", "dog-puppy#feeding"),        # duplicate
    ]
    issues = validate_plan(request, tasks, {"dog-puppy#feeding", "dog-puppy#exercise-and-play"})
    codes = {(i.task_index, i.code) for i in issues if i.level == "error"}
    assert (1, "during_busy_hours") in codes
    assert (2, "unknown_pet") in codes
    assert (3, "bad_time") in codes
    assert (4, "outside_waking_hours") in codes
    assert (5, "bad_frequency") in codes
    assert (6, "unsupported_source") in codes
    assert (7, "medication_dose") in codes
    assert (8, "duplicate_task") in codes
    assert not any(i.task_index == 0 for i in issues if i.level == "error")


def test_conflicts_use_original_scheduler_and_include_existing_tasks():
    owner = Owner("Jordan", [Pet("Biscuit", "dog", [Task("Vet visit", "18:00")])])
    tasks = [ProposedTask("Biscuit", "Dinner", "18:00", "daily", "dog-puppy")]
    issues = validate_plan(puppy_request(), tasks, {"dog-puppy#feeding"}, existing_owner=owner)
    assert [i.code for i in issues] == ["time_conflict"]
    assert issues[0].level == "warning"


def test_busy_windows_across_midnight():
    assert in_window("23:30", ("22:00", "06:00"))
    assert in_window("05:00", ("22:00", "06:00"))
    assert not in_window("12:00", ("22:00", "06:00"))


@pytest.mark.parametrize("text,expected", [
    ("I work 9 to 5", [["09:00", "17:00"]]),
    ("busy 9am-5pm", [["09:00", "17:00"]]),
    ("from 8:30 to 4:30", [["08:30", "16:30"]]),
    ("6am until 11pm", [["06:00", "23:00"]]),
    ("no schedule given", []),
])
def test_rule_planner_parses_busy_windows(text, expected):
    assert _parse_busy_windows(text) == expected


# --- Agent loop -----------------------------------------------------------------

def test_agent_refuses_without_calling_the_planner(kb, tmp_path):
    planner = ScriptedPlanner(puppy_request(), [])
    result = make_agent(planner, kb, tmp_path).run("tell me a joke about bitcoin")
    assert result.status == "refused"
    assert planner.calls == []


def test_agent_emergency_short_circuits(kb, tmp_path):
    planner = ScriptedPlanner(puppy_request(), [])
    result = make_agent(planner, kb, tmp_path).run("my puppy ate grapes")
    assert result.status == "emergency" and "Poison Control" in result.message
    assert planner.calls == []


def test_agent_revises_until_plan_passes(kb, tmp_path):
    bad = [ProposedTask("Biscuit", "Lunch", "12:00", "daily", "dog-puppy")]
    good = [ProposedTask("Biscuit", "Lunch", "17:30", "daily", "dog-puppy")]
    planner = ScriptedPlanner(puppy_request(), [bad, good])
    result = make_agent(planner, kb, tmp_path).run("4 month old puppy named Biscuit, I work 9 to 5")
    assert planner.calls == ["parse", "draft", "revise"]
    assert any(i.code == "during_busy_hours" for i in planner.issues_seen)
    assert result.status == "planned" and result.revisions == 1
    assert [t.time for t in result.tasks] == ["17:30"]
    assert [s["step"] for s in result.trace] == ["screen", "parse", "retrieve", "draft", "verify", "revise",
                                                  "verify", "finalize"]


def test_agent_drops_tasks_that_never_pass_and_stops_after_max_revisions(kb, tmp_path):
    ok = ProposedTask("Biscuit", "Dinner", "18:00", "daily", "dog-puppy")
    bad = ProposedTask("Biscuit", "Give 10 mg pill", "19:00", "daily", "dog-puppy")
    planner = ScriptedPlanner(puppy_request(), [[ok, bad], [ok, bad], [ok, bad]])
    result = make_agent(planner, kb, tmp_path).run("puppy named Biscuit, I work 9 to 5")
    assert planner.calls.count("revise") == 2
    assert result.status == "needs_review"
    assert [t.description for t in result.tasks] == ["Dinner"]
    assert "dose" in result.rejected[0]["issues"][0]
    assert 0 < result.confidence < 1


def test_agent_survives_planner_crash(kb, tmp_path):
    class Broken(ScriptedPlanner):
        def draft_plan(self, request, context):
            raise TimeoutError("API timed out")
    result = make_agent(Broken(puppy_request(), []), kb, tmp_path).run("puppy named Biscuit")
    assert result.status == "error" and "TimeoutError" in result.message


def test_agent_writes_trace_file(kb, tmp_path):
    planner = ScriptedPlanner(puppy_request(), [[ProposedTask("Biscuit", "Dinner", "18:00", "daily", "dog-puppy")]])
    result = make_agent(planner, kb, tmp_path).run("puppy named Biscuit")
    assert result.trace_path and (tmp_path / "traces").exists()


def test_rule_planner_end_to_end_keeps_tasks_out_of_work_hours(kb, tmp_path):
    agent = make_agent(RulePlanner(kb), kb, tmp_path)
    result = agent.run("I just adopted a 4-month-old puppy named Biscuit and I work 9 to 5.")
    assert result.status == "planned"
    assert result.request.pets[0].life_stage == "puppy"
    assert result.tasks and all(not in_window(t.time, ("09:00", "17:00")) for t in result.tasks)
    assert all(t.source == "dog-puppy" for t in result.tasks)


# --- Claude backend (fake client, no network) ------------------------------------

class FakeClaudeClient:
    """Stands in for anthropic.Anthropic(): records requests, returns queued replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        stop_reason, text = self.replies.pop(0)
        return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def claude_planner(replies):
    planner = ClaudePlanner.__new__(ClaudePlanner)  # skip __init__ so no API key is needed
    planner.model = "claude-opus-5-5"
    planner.client = FakeClaudeClient(replies)
    return planner


def test_claude_planner_runs_full_agent_loop_with_structured_outputs(kb, tmp_path):
    parse = json.dumps({"pets": [{"name": "Biscuit", "species": "dog", "age_years": 0.33, "conditions": []}],
                        "busy_windows": [["09:00", "17:00"]], "notes": ""})
    draft = json.dumps({"tasks": [
        {"pet": "Biscuit", "description": "Breakfast", "time": "07:00", "frequency": "daily",
         "source": "dog-puppy#feeding", "reason": "several small meals"},
        {"pet": "Biscuit", "description": "Lunch", "time": "12:00", "frequency": "daily",
         "source": "dog-puppy#feeding", "reason": "several small meals"}]})
    revised = json.dumps({"tasks": [
        {"pet": "Biscuit", "description": "Breakfast", "time": "07:00", "frequency": "daily",
         "source": "dog-puppy#feeding", "reason": "several small meals"},
        {"pet": "Biscuit", "description": "Early dinner", "time": "17:15", "frequency": "daily",
         "source": "dog-puppy#feeding", "reason": "several small meals"}]})
    planner = claude_planner([("end_turn", parse), ("end_turn", draft), ("end_turn", revised)])
    result = make_agent(planner, kb, tmp_path).run("4-month-old puppy Biscuit, I work 9 to 5")

    assert result.status == "planned" and result.revisions == 1
    assert [t.time for t in result.tasks] == ["07:00", "17:15"]
    first = planner.client.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["fallbacks"] == "default"
    assert "12:00 is during the owner's busy hours" in planner.client.requests[2]["messages"][0]["content"]


def test_claude_refusal_becomes_a_safe_error(kb, tmp_path):
    planner = claude_planner([("refusal", "")])
    result = make_agent(planner, kb, tmp_path).run("puppy named Biscuit")
    assert result.status == "error" and "ValueError" in result.message


def test_make_planner_auto_prefers_gemini_then_claude_then_rule(kb, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert available_ai_planner() is None
    assert make_planner("auto", kb).name == "rule-baseline"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert available_ai_planner() == "claude"
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert available_ai_planner() == "gemini"


# --- Gemini backend (fake client, no network) ------------------------------------

def gemini_planner(responses, monkeypatch):
    """GeminiPlanner with a fake client. responses: JSON strings, or exceptions to raise."""
    from pawpal_ai.planners import GeminiPlanner

    monkeypatch.setattr("pawpal_ai.planners.time.sleep", lambda s: None)  # don't actually wait on retries
    planner = GeminiPlanner.__new__(GeminiPlanner)
    planner.model, planner.config, planner.calls = "gemini-test", None, []
    queue = list(responses)

    def generate_content(model, contents, config):
        planner.calls.append(contents)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)

    planner.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return planner


def rate_limit_error():
    from google.genai import errors
    return errors.APIError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})


def test_gemini_planner_runs_agent_loop_and_retries_rate_limits(kb, tmp_path, monkeypatch):
    parse = json.dumps({"pets": [{"name": "Luna", "species": "cat", "age_years": 14, "conditions": ["kidney"]}],
                        "busy_windows": [["08:00", "18:00"]], "notes": ""})
    draft = json.dumps({"tasks": [
        {"pet": "Luna", "description": "Refresh water in every bowl", "time": "07:10", "frequency": "daily",
         "source": "cat-senior#kidney-health-and-water", "reason": "encourages drinking"}]})
    planner = gemini_planner([rate_limit_error(), parse, draft], monkeypatch)
    result = make_agent(planner, kb, tmp_path).run("My cat Luna is 14 with kidney disease, busy 8am to 6pm")
    assert result.status == "planned"
    assert result.request.pets[0].life_stage == "senior"
    assert result.tasks[0].source == "cat-senior#kidney-health-and-water"
    assert len(planner.calls) == 3  # one rate-limited attempt, then parse + draft


def test_gemini_gives_up_after_repeated_rate_limits(kb, tmp_path, monkeypatch):
    planner = gemini_planner([rate_limit_error()] * 4, monkeypatch)
    result = make_agent(planner, kb, tmp_path).run("puppy named Biscuit")
    assert result.status == "error" and len(planner.calls) == 4


def test_apply_plan_adds_approved_tasks_to_pawpal_model():
    owner = Owner("Jordan")
    request = puppy_request()
    tasks = [ProposedTask("Biscuit", "Dinner", "18:00", "daily", "dog-puppy")]
    assert apply_plan(owner, request, tasks) == 1
    assert owner.pets[0].name == "Biscuit" and owner.pets[0].species == "dog"
    assert owner.pets[0].tasks[0].frequency == "daily"
