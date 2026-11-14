"""
Guardrails around the AI planner.

Two checkpoints:
1. check_request() runs BEFORE any AI call. It stops empty, off-topic and
   emergency messages, and flags medication/dosing questions.
2. validate_plan() runs AFTER every draft. It checks each proposed task against
   hard rules (valid time, known pet, cited source that was actually retrieved,
   outside the owner's busy hours, no medication doses) and uses the original
   PawPal+ Scheduler to find time conflicts.

Errors reject a task (the agent then asks the planner to revise). Warnings are
shown to the owner, who makes the final call.
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from pawpal_system import Owner, Pet, Scheduler, Task

from .models import CareRequest, Issue, ProposedTask

MAX_REQUEST_CHARS = 1500
AWAKE_START, AWAKE_END = "06:00", "22:30"
FREQUENCIES = {"once", "daily", "weekly"}
MAX_TASKS_PER_PET = 12

PET_WORDS = re.compile(
    r"\b(pets?|dogs?|puppy|puppies|pup|cats?|kittens?|kitty|hamsters?|rabbits?|bunny|"
    r"birds?|parrots?|fish|guinea pigs?|ferrets?|turtles?|lizards?|snakes?|horses?|"
    r"adopt\w*|rescue\w*|vet|leash|litter|kibble|walks?|feeding)\b",
    re.IGNORECASE,
)

EMERGENCY = re.compile(
    r"\b(ate|eaten|swallowed|chewed)\b.*\b(chocolate|grapes?|raisins?|xylitol|onions?|"
    r"garlic|poison|rat bait|antifreeze|pills?|medicine|ibuprofen|tylenol)\b|"
    r"\b(seizure|seizing|not breathing|can'?t breathe|trouble breathing|collapsed?|"
    r"unconscious|bleeding (a lot|heavily|badly)|hit by a car|can'?t (pee|urinate)|poisoned)\b",
    re.IGNORECASE,
)

MEDICAL_QUESTION = re.compile(
    r"\b(dose|dosage|dosing|how (much|many) (mg|ml|pills?|tablets?)|mg|milligrams?|"
    r"benadryl|ibuprofen|tylenol|aspirin|antibiotics?|prescri\w*|medicat\w*|medicine|"
    r"diagnos\w*|symptoms?|vomit\w*|diarrh\w*)\b",
    re.IGNORECASE,
)

# A dose in a task description ("5 mg", "2 tablets", "0.5ml") is never allowed:
# doses must come from a vet, not from the planner.
DOSE_IN_TASK = re.compile(r"\b\d+(\.\d+)?\s*(mg|ml|mcg|milligrams?|tablets?|pills?|capsules?|cc)\b", re.IGNORECASE)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

EMERGENCY_MESSAGE = (
    "This sounds like it could be an emergency. PawPal+ can't help with that: "
    "please contact your veterinarian or an emergency animal hospital now. For "
    "suspected poisoning, call the ASPCA Animal Poison Control Center at (888) 426-4435."
)
MEDICAL_NOTE = (
    "Medication and dosing questions have to be answered by your vet. PawPal+ can "
    "remind you to give a medication at the time your vet set, but it will never "
    "suggest a drug or a dose."
)


@dataclass
class RequestCheck:
    status: str  # "ok", "empty", "too_long", "off_topic", "emergency"
    message: str = ""
    flags: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def check_request(text: str) -> RequestCheck:
    """Screens the owner's message before any AI call is made."""
    text = (text or "").strip()
    if not text:
        return RequestCheck("empty", "Please describe your pets and your daily schedule.")
    if len(text) > MAX_REQUEST_CHARS:
        return RequestCheck("too_long", f"Please keep the request under {MAX_REQUEST_CHARS} characters.")
    if EMERGENCY.search(text):
        return RequestCheck("emergency", EMERGENCY_MESSAGE)
    if not PET_WORDS.search(text):
        return RequestCheck(
            "off_topic",
            "PawPal+ only plans pet care. Tell me about your pets (species, age, any "
            "health conditions) and when you're busy, and I'll draft a care schedule.",
        )
    flags = ["medical_question"] if MEDICAL_QUESTION.search(text) else []
    return RequestCheck("ok", MEDICAL_NOTE if flags else "", flags)


def to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def in_window(time: str, window: Iterable[str]) -> bool:
    """True if time falls inside [start, end). Windows that cross midnight are handled."""
    start, end = (to_minutes(t) for t in window)
    t = to_minutes(time)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def validate_plan(
    request: CareRequest,
    tasks: List[ProposedTask],
    retrieved_ids: Iterable[str],
    existing_owner: Optional[Owner] = None,
) -> List[Issue]:
    """
    Checks every proposed task. Returns a list of Issues; an empty list means
    the plan passed. retrieved_ids are the chunk ids (and their doc ids) the
    planner was shown, so a task citing anything else is treated as made up.
    """
    issues: List[Issue] = []
    allowed_sources = set(retrieved_ids)
    allowed_sources |= {cid.split("#")[0] for cid in allowed_sources}
    pet_names = {p.name.lower() for p in request.pets}
    per_pet = {}

    for i, task in enumerate(tasks):
        def err(code, message):
            issues.append(Issue("error", code, message, i))

        if not task.description:
            err("empty_description", "Task has no description.")
        if task.pet.lower() not in pet_names:
            err("unknown_pet", f"Task is for '{task.pet}', who isn't one of the owner's pets.")
        if not TIME_RE.match(task.time):
            err("bad_time", f"'{task.time}' is not a valid HH:MM time.")
        else:
            if not in_window(task.time, (AWAKE_START, AWAKE_END)):
                err("outside_waking_hours", f"{task.time} is outside {AWAKE_START}-{AWAKE_END}.")
            for window in request.busy_windows:
                if in_window(task.time, window):
                    err("during_busy_hours",
                        f"{task.time} is during the owner's busy hours ({window[0]}-{window[1]}).")
        if task.frequency not in FREQUENCIES:
            err("bad_frequency", f"Frequency '{task.frequency}' must be once, daily or weekly.")
        if not task.source:
            err("missing_source", "Task doesn't cite a knowledge-base source.")
        elif task.source not in allowed_sources:
            err("unsupported_source",
                f"Source '{task.source}' was not among the retrieved guides, so the task isn't grounded.")
        if DOSE_IN_TASK.search(task.description) or DOSE_IN_TASK.search(task.reason):
            err("medication_dose", "Task includes a medication dose; doses must come from a vet.")
        per_pet[task.pet.lower()] = per_pet.get(task.pet.lower(), 0) + 1

    for pet, count in per_pet.items():
        if count > MAX_TASKS_PER_PET:
            issues.append(Issue("error", "too_many_tasks",
                                f"{count} tasks for {pet} is more than the limit of {MAX_TASKS_PER_PET}."))

    # Duplicate tasks (same pet, time and description) are always a mistake.
    seen = {}
    for i, task in enumerate(tasks):
        key = (task.pet.lower(), task.time, task.description.lower())
        if key in seen:
            issues.append(Issue("error", "duplicate_task", f"Duplicate of task {seen[key] + 1}.", i))
        else:
            seen[key] = i

    issues.extend(_conflict_warnings(tasks, existing_owner))
    return issues


def _conflict_warnings(tasks: List[ProposedTask], existing_owner: Optional[Owner]) -> List[Issue]:
    """Reuses the original PawPal+ Scheduler to flag same-time tasks (warnings, not errors)."""
    owner = Owner("check")
    pets = {}
    if existing_owner is not None:
        for pet in existing_owner.pets:
            pets[pet.name.lower()] = Pet(pet.name, pet.species,
                                         [Task(t.description, t.due_time, t.is_complete, t.frequency,
                                               due_date=t.due_date) for t in pet.tasks])
    for task in tasks:
        if TIME_RE.match(task.time):
            pet = pets.setdefault(task.pet.lower(), Pet(task.pet, ""))
            pet.add_task(Task(task.description, task.time, frequency=task.frequency))
    for pet in pets.values():
        owner.add_pet(pet)
    return [Issue("warning", "time_conflict", w) for w in Scheduler(owner).get_conflict_warnings()]


def errors_only(issues: List[Issue]) -> List[Issue]:
    return [i for i in issues if i.level == "error"]
