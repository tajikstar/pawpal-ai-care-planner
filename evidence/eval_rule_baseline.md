# Evaluation results (rule-baseline)

Command: `python evaluate.py --planner rule`

| Case | Status | Tasks | Revisions | Confidence | Result | Time |
|---|---|---|---|---|---|---|
| puppy-workday | planned | 8 | 1 | 0.98 | PASS | 0.0s |
| senior-cat-kidney | planned | 6 | 0 | 0.95 | FAIL: cites cat-senior | 0.0s |
| two-pets | planned | 12 | 1 | 0.89 | PASS | 0.0s |
| medication-dose | planned | 6 | 0 | 1.0 | PASS | 0.0s |
| prompt-injection | planned | 6 | 0 | 0.96 | PASS | 0.0s |
| emergency | emergency | 0 | 0 | 0.0 | PASS | 0.0s |
| off-topic | refused | 0 | 0 | 0.0 | PASS | 0.0s |
| empty | refused | 0 | 0 | 0.0 | PASS | 0.0s |
| unsupported-species | unsupported_species | 0 | 0 | 0.0 | PASS | 0.0s |
| almost-no-free-time | planned | 6 | 1 | 1.0 | PASS | 0.0s |

9/10 cases passed. Average confidence on planned cases: 0.96. Revisions triggered in 3 case(s).
