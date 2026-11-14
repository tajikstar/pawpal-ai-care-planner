"""
The PawPal+ care-planning agent.

    owner message
      -> [1 screen]   guardrails.check_request (no AI call for empty/off-topic/emergency)
      -> [2 parse]    planner.parse_request: pets, ages, conditions, busy hours
      -> [3 retrieve] knowledge base search per pet (species + life stage aware)
      -> [4 draft]    planner.draft_plan using only the retrieved excerpts
      -> [5 verify]   guardrails.validate_plan (+ the original Scheduler's conflict check)
      -> [6 revise]   if errors: planner.revise_plan with the error list, then verify again
                      (at most MAX_REVISIONS times)
      -> [7 finalize] drop tasks that still fail, score confidence, save the trace
      -> owner approves or rejects each task (human in the loop) -> apply_plan()

Every step is written to logs/pawpal_ai.log and to a JSON trace in logs/traces/.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from pawpal_system import Owner, Pet, Task

from .guardrails import MEDICAL_NOTE, check_request, errors_only, validate_plan
from .models import CareRequest, Issue, ProposedTask
from .retriever import KnowledgeBase, Retrieved

MAX_REVISIONS = 2
STRONG_RETRIEVAL_SCORE = 0.5  # a top score at or above this counts as fully grounded

logger = logging.getLogger("pawpal_ai")


def setup_logging(log_dir: str = "logs") -> None:
    """Logs INFO and above to logs/pawpal_ai.log (once per process)."""
    if logger.handlers:
        return
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(log_dir, "pawpal_ai.log"), encoding="utf8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@dataclass
class PlanResult:
    status: str  # planned | needs_review | refused | emergency | no_pets | unsupported_species | error
    message: str = ""
    request: Optional[CareRequest] = None
    tasks: List[ProposedTask] = field(default_factory=list)      # passed every check
    rejected: List[Dict] = field(default_factory=list)          # {"task": ..., "issues": [...]}
    warnings: List[Issue] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    confidence: float = 0.0
    revisions: int = 0
    trace: List[Dict] = field(default_factory=list)
    trace_path: str = ""


class CarePlannerAgent:
    def __init__(self, planner, kb: KnowledgeBase, max_revisions: int = MAX_REVISIONS,
                 log_dir: str = "logs", save_traces: bool = True):
        self.planner = planner
        self.kb = kb
        self.max_revisions = max_revisions
        self.log_dir = log_dir
        self.save_traces = save_traces
        setup_logging(log_dir)

    # ------------------------------------------------------------------
    def run(self, text: str, existing_owner: Optional[Owner] = None) -> PlanResult:
        trace: List[Dict] = []

        def step(name: str, detail: str, **data):
            trace.append({"step": name, "detail": detail, **data})
            logger.info("%s | %s", name, detail)

        result = self._run(text, existing_owner, step)
        result.trace = trace
        if self.save_traces:
            result.trace_path = self._save_trace(text, result)
        logger.info("result | status=%s tasks=%d rejected=%d confidence=%.2f",
                    result.status, len(result.tasks), len(result.rejected), result.confidence)
        return result

    def _run(self, text, existing_owner, step) -> PlanResult:
        # 1. Screen the request before any AI call.
        check = check_request(text)
        step("screen", f"status={check.status} flags={check.flags}", message=check.message)
        if check.status == "emergency":
            return PlanResult("emergency", check.message)
        if not check.ok:
            return PlanResult("refused", check.message)
        notes = [MEDICAL_NOTE] if "medical_question" in check.flags else []

        # 2. Parse the message into pets and constraints.
        try:
            request = self.planner.parse_request(text)
        except Exception as exc:  # API/network/JSON problems never crash the app
            logger.exception("parse failed")
            step("parse", f"FAILED: {type(exc).__name__}: {exc}")
            return PlanResult("error", f"Couldn't understand the request ({type(exc).__name__}). Please try again.")
        step("parse", f"{len(request.pets)} pet(s), busy={request.busy_windows}", request=request.to_dict())
        if not request.pets:
            return PlanResult("no_pets", "I couldn't find any pets in that message. "
                              "Try something like: 'a 2-year-old dog named Max, I work 9 to 5'.",
                              request=request, notes=notes)

        unsupported = [p for p in request.pets if not self.kb.species_supported(p.species)]
        if unsupported:
            names = ", ".join(f"{p.name} ({p.species})" for p in unsupported)
            notes.append(f"No care guides for {names} yet, so no tasks were planned for them.")
            request.pets = [p for p in request.pets if p not in unsupported]
            step("parse", f"dropped unsupported species: {names}")
            if not request.pets:
                return PlanResult("unsupported_species",
                                  f"PawPal+ only has care guides for dogs and cats right now ({names}).",
                                  request=request, notes=notes)

        # 3. Retrieve guide excerpts for each pet.
        context: Dict[str, List[Retrieved]] = {}
        for pet in request.pets:
            query = (f"{pet.species} {pet.life_stage} {' '.join(pet.conditions)} "
                     "feeding meals exercise walk play litter water routine health checkup")
            results = self.kb.retrieve(query, species=pet.species, life_stage=pet.life_stage)
            if pet.conditions or notes:
                results += [r for r in self.kb.retrieve("medication safety vet emergency", species="any",
                                                       top_k=2) if r.chunk.doc_id == "vet-safety"]
            context[pet.name] = results
            step("retrieve", f"{pet.name}: {[(r.chunk.chunk_id, r.score) for r in results]}")
        retrieved_ids = {r.chunk.chunk_id for results in context.values() for r in results}

        # 4. Draft, 5. verify, 6. revise.
        try:
            tasks = self.planner.draft_plan(request, context)
            step("draft", f"{len(tasks)} task(s) proposed", tasks=[t.to_dict() for t in tasks])
            issues = validate_plan(request, tasks, retrieved_ids, existing_owner)
            step("verify", _issue_summary(issues), issues=[i.to_dict() for i in issues])
            revisions = 0
            while errors_only(issues) and revisions < self.max_revisions:
                revisions += 1
                tasks = self.planner.revise_plan(request, context, tasks, issues)
                step("revise", f"attempt {revisions}: {len(tasks)} task(s)", tasks=[t.to_dict() for t in tasks])
                issues = validate_plan(request, tasks, retrieved_ids, existing_owner)
                step("verify", _issue_summary(issues), issues=[i.to_dict() for i in issues])
        except Exception as exc:
            logger.exception("planning failed")
            step("draft", f"FAILED: {type(exc).__name__}: {exc}")
            return PlanResult("error", f"The planner failed ({type(exc).__name__}). Please try again.",
                              request=request, notes=notes)

        # 7. Keep only tasks with no errors; score confidence.
        bad = {}
        for issue in errors_only(issues):
            if issue.task_index is not None:
                bad.setdefault(issue.task_index, []).append(issue.message)
        good = [t for i, t in enumerate(tasks) if i not in bad]
        rejected = [{"task": tasks[i].to_dict(), "issues": msgs} for i, msgs in sorted(bad.items())]
        warnings = [i for i in issues if i.level == "warning"]
        confidence = _confidence(len(good), len(tasks), context, len(warnings))
        status = "planned" if good and not rejected else "needs_review"
        message = (f"Drafted {len(good)} task(s) for {', '.join(p.name for p in request.pets)}."
                   if good else "I couldn't build a plan that passes the safety checks. Please adjust your request.")
        if rejected:
            message += f" {len(rejected)} task(s) still failed checks after {revisions} revision(s) and were left out."
        step("finalize", f"status={status} kept={len(good)} rejected={len(rejected)} confidence={confidence}")
        return PlanResult(status, message, request, good, rejected, warnings, notes, confidence, revisions)

    # ------------------------------------------------------------------
    def _save_trace(self, text: str, result: PlanResult) -> str:
        folder = os.path.join(self.log_dir, "traces")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"run-{datetime.now():%Y%m%d-%H%M%S-%f}.json")
        payload = {
            "planner": getattr(self.planner, "name", type(self.planner).__name__),
            "input": text,
            "status": result.status,
            "confidence": result.confidence,
            "revisions": result.revisions,
            "steps": result.trace,
            "final_tasks": [t.to_dict() for t in result.tasks],
            "rejected": result.rejected,
        }
        with open(path, "w", encoding="utf8") as f:
            json.dump(payload, f, indent=2, default=str)
        return path


def _issue_summary(issues: List[Issue]) -> str:
    errors = errors_only(issues)
    if not issues:
        return "all checks passed"
    codes = sorted({i.code for i in errors})
    return f"{len(errors)} error(s) {codes}, {len(issues) - len(errors)} warning(s)"


def _confidence(kept: int, total: int, context: Dict[str, List[Retrieved]], warnings: int) -> float:
    """
    0.7 x share of drafted tasks that passed every check
  + 0.3 x retrieval strength (each pet's best excerpt score vs STRONG_RETRIEVAL_SCORE)
  - 0.03 per warning. Clamped to 0..1. Computed from checks, not self-reported by the model.
    """
    if total == 0:
        return 0.0
    strengths = [min(1.0, (results[0].score if results else 0) / STRONG_RETRIEVAL_SCORE)
                 for results in context.values()]
    retrieval = sum(strengths) / len(strengths) if strengths else 0.0
    score = 0.7 * kept / total + 0.3 * retrieval - 0.03 * warnings
    return round(max(0.0, min(1.0, score)), 2)


def apply_plan(owner: Owner, request: CareRequest, tasks: List[ProposedTask]) -> int:
    """Adds approved tasks to the owner's PawPal+ data, creating pets as needed. Returns tasks added."""
    species = {p.name.lower(): p.species for p in request.pets} if request else {}
    added = 0
    for task in tasks:
        pet = next((p for p in owner.pets if p.name.lower() == task.pet.lower()), None)
        if pet is None:
            pet = Pet(task.pet, species.get(task.pet.lower(), "other"))
            owner.add_pet(pet)
        pet.add_task(Task(task.description, task.time, frequency=task.frequency))
        added += 1
    logger.info("apply | %d approved task(s) added to %s's schedule", added, owner.name)
    return added
