# AI Interactions Log

This file documents the **agentic workflow stretch feature**: the PawPal+ care planner's multi-step reasoning chain (parse → retrieve → draft → verify → revise → finalize) and its saved reasoning traces.

Every run writes a full JSON trace to `logs/traces/` (git-ignored). Selected traces are committed in [`evidence/`](evidence/).

---

## How the agent decides

| Step | Decision made | Tool / check used |
|---|---|---|
| screen | Is this safe and on-topic to plan at all? | `check_request()`: regex for emergencies, pet context, length |
| parse | Which pets, ages, conditions and busy hours? | `planner.parse_request()` (Claude/Gemini JSON, or regex baseline) |
| retrieve | Which guide sections apply to each pet? | `KnowledgeBase.retrieve()`: TF-IDF + species filter + life-stage boost |
| draft | Which tasks, when, and backed by which excerpt? | `planner.draft_plan()` with only the retrieved excerpts |
| verify | Does each task obey the rules? | `validate_plan()` + original `Scheduler.check_conflicts()` |
| revise | How do I fix exactly the failing tasks? | `planner.revise_plan(errors)`, at most 2 rounds |
| finalize | What do I keep, and how confident am I? | drop failing tasks, compute confidence from the checks |
| (human) | Which tasks actually go on the schedule? | owner approves each task in the UI/CLI |

---

## Trace 1: puppy, owner works 9 to 5 (verify fails → revise → pass)

Full trace: [`evidence/trace_rule_puppy.json`](evidence/trace_rule_puppy.json) (offline baseline planner)

```text
screen    status=ok flags=[]
parse     1 pet(s), busy=[['09:00', '17:00']]   -> Biscuit, dog, 0.33 years (life stage: puppy)
retrieve  Biscuit: dog-puppy#exercise-and-play 0.469, dog-puppy#feeding 0.42,
          dog-puppy#potty-training 0.284, dog-puppy#training-and-socialization 0.25,
          dog-puppy#vet-care 0.25, dog-adult#feeding 0.08
draft     8 task(s) proposed (Midday meal 12:00, Midday potty break 12:20, ...)
verify    2 error(s) ['during_busy_hours']
          - 12:00 is during the owner's busy hours (09:00-17:00).
          - 12:20 is during the owner's busy hours (09:00-17:00).
revise    attempt 1: 8 task(s)  (Midday meal -> 08:50, Midday potty break -> 08:40)
verify    all checks passed
finalize  status=planned kept=8 rejected=0 confidence=0.98
```

**What this shows:** retrieval picked the puppy guide over the adult guide (life-stage boost), the verifier caught two rule violations, and the revision step fixed only those two tasks. **What I noticed:** the fix follows the rules but isn't ideal. A puppy meal at 08:50 is really a second breakfast. A smarter reviser, like the Gemini planner, could suggest a midday dog walker or a later meal instead.

## Trace 2: emergency (stopped at the first step)

```text
screen    status=emergency flags=[]
          "This sounds like it could be an emergency ... call the ASPCA Animal Poison Control Center at (888) 426-4435."
```

No parse, retrieval or model call happens, so a possible emergency is never handled by the AI.

## Trace 3: Gemini planner, senior cat with kidney disease (passes on the first draft)

Full trace: [`evidence/trace_gemini_senior_cat.json`](evidence/trace_gemini_senior_cat.json)
Input: *"My cat Luna is 14 and has kidney disease. I'm busy 8am to 6pm."*

```text
screen    status=ok flags=[]
parse     1 pet(s), busy=[['08:00', '18:00']]   -> Luna, cat, 14 years, conditions: kidney disease (life stage: senior)
retrieve  Luna: cat-senior#health-checks 0.54, cat-senior#kidney-health-and-water 0.507,
          cat-senior#feeding 0.386, cat-senior#comfort-and-mobility 0.365,
          vet-safety#medication-safety 0.404, vet-safety#emergencies 0.092
draft     6 task(s) proposed
verify    all checks passed
finalize  status=planned kept=6 rejected=0 confidence=1.0
```

**What this shows:** because Luna has a health condition, the agent also retrieved the vet-safety guide. Gemini cited specific sections (`cat-senior#kidney-health-and-water`, not just the file) and scheduled everything outside work hours, so verification passed with no revision. Compared with the baseline (Trace 1), the AI's value is in **understanding the request** and **choosing relevant tasks**. The safety guarantee still comes from the verify step.

---

## Prompt comparison: offline baseline vs Gemini planner

| | Option A: RulePlanner (no AI) | Option B: GeminiPlanner |
|-|----------|----------|
| **Approach** | Regex parsing + each guide's fixed routine list | Gemini parses to JSON and drafts from the retrieved excerpts with citations |
| **Strengths** | Deterministic, free, works offline, easy to test | Understands free-form wording ("Luna is 14", "cat Mimi"), tailors tasks to conditions |
| **Problems noticed** | Missed "Luna is 14" (no "years"), missed the name in "my cat Mimi", naive time shifting | Skipped the puppy's midday break instead of suggesting a dog walker; 20 to 30 seconds per plan on the free tier; needs a working API key |
| **Eval score** | 9/10 | 10/10 (average confidence 0.99, 0 revisions) |
| **Decision** | Kept as fallback and test double | Main planner when a key is available |
