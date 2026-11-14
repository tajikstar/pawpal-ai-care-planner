"""
PawPal+ AI care planner, command-line version.

    python planner.py "I adopted a 4-month-old puppy named Biscuit. I work 9 to 5."
    python planner.py --planner claude "..."   # or gemini; auto picks whichever key is set
    python planner.py --planner rule "..."     # offline baseline, no API key needed
    python planner.py --yes "..."              # approve every task that passed the checks

Without --yes you approve or reject each proposed task (human in the loop).
Approved tasks go into the original PawPal+ Owner/Pet/Task model and the
Scheduler prints the final schedule.
"""

import argparse
import sys

from dotenv import load_dotenv

from pawpal_ai.agent import CarePlannerAgent, apply_plan
from pawpal_ai.planners import PLANNER_KINDS, make_planner
from pawpal_ai.retriever import KnowledgeBase
from pawpal_system import Owner, Scheduler


def print_result(result) -> None:
    print(f"\nStatus: {result.status}   Confidence: {result.confidence:.2f}   Revisions: {result.revisions}")
    print(result.message)
    for note in result.notes:
        print(f"Note: {note}")
    if result.tasks:
        print("\nProposed tasks (all passed the guardrail checks):")
        print(f"  {'#':<3}{'Pet':<10}{'Time':<7}{'Repeats':<9}{'Task':<52}Source")
        for i, t in enumerate(result.tasks, 1):
            print(f"  {i:<3}{t.pet:<10}{t.time:<7}{t.frequency:<9}{t.description[:50]:<52}{t.source}")
    if result.rejected:
        print("\nLeft out (still failed checks):")
        for r in result.rejected:
            print(f"  - {r['task']['pet']} {r['task']['time']} {r['task']['description']}: {'; '.join(r['issues'])}")
    if result.warnings:
        print("\nWarnings for you to review:")
        for w in result.warnings:
            print(f"  - {w.message}")
    if result.trace_path:
        print(f"\nTrace saved to {result.trace_path}")


def approve(tasks, auto_yes: bool):
    if auto_yes:
        return list(tasks)
    approved = []
    print("\nApprove each task: y = add, n = skip, q = stop.")
    for t in tasks:
        answer = input(f"  {t.pet} {t.time} {t.description}? [y/n/q] ").strip().lower()
        if answer == "q":
            break
        if answer in ("y", "yes", ""):
            approved.append(t)
    return approved


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="PawPal+ AI care planner")
    parser.add_argument("request", nargs="*", help="describe your pets and schedule")
    parser.add_argument("--planner", choices=PLANNER_KINDS, default="auto")
    parser.add_argument("--yes", action="store_true", help="approve all checked tasks without asking")
    parser.add_argument("--owner", default="Jordan")
    args = parser.parse_args(argv)

    text = " ".join(args.request) or input("Describe your pets and your schedule: ")
    kb = KnowledgeBase.load()
    try:
        planner = make_planner(args.planner, kb)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 2
    print(f"Planner: {planner.name}")

    result = CarePlannerAgent(planner, kb).run(text)
    print_result(result)
    if not result.tasks:
        return 0 if result.status in ("refused", "emergency", "unsupported_species", "no_pets") else 1

    approved = approve(result.tasks, args.yes)
    owner = Owner(args.owner)
    added = apply_plan(owner, result.request, approved)
    print(f"\nAdded {added} approved task(s) to {owner.name}'s PawPal+ schedule:")
    scheduler = Scheduler(owner)
    for t in scheduler.sort_by_time():
        print(f"  {t.due_time}  {t.pet_name:<10}{t.description} ({t.frequency})")
    for w in scheduler.get_conflict_warnings():
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
