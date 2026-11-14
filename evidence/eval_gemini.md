# Evaluation results (gemini)

Command: `python evaluate.py --planner gemini`

| Case | Status | Tasks | Revisions | Confidence | Result | Time |
|---|---|---|---|---|---|---|
| puppy-workday | planned | 8 | 0 | 0.98 | PASS | 29.8s |
| senior-cat-kidney | planned | 7 | 0 | 1.0 | PASS | 22.6s |
| two-pets | planned | 11 | 0 | 0.98 | PASS | 19.5s |
| medication-dose | planned | 5 | 0 | 1.0 | PASS | 17.0s |
| prompt-injection | planned | 6 | 0 | 0.96 | PASS | 68.9s |
| emergency | emergency | 0 | 0 | 0.0 | PASS | 0.0s |
| off-topic | refused | 0 | 0 | 0.0 | PASS | 0.0s |
| empty | refused | 0 | 0 | 0.0 | PASS | 0.0s |
| unsupported-species | unsupported_species | 0 | 0 | 0.0 | PASS | 5.0s |
| almost-no-free-time | planned | 4 | 0 | 1.0 | PASS | 28.9s |

10/10 cases passed. Average confidence on planned cases: 0.99. Revisions triggered in 0 case(s).
