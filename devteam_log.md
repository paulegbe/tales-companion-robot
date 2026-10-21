# Dev team log

One line per pipeline run. Format:
`YYYY-MM-DD | issue #N | planner ok | tester loops: X | reviewer loops: Y | PR #M | notes`

2026-09-30 | issue #1 | planner ok | tester loops: 1 | reviewer loops: 1 | PR #5 | reviewer approved, 3 nice-to-haves not applied; filed #2 (conftest, high), #3 (requirements.txt), #4 (plan.md push path)
2026-10-21 | issue #2 | planner ok | tester loops: 1 | reviewer loops: 2 | PR #8 | reviewer loop 1: pytest.CollectReport not public on pytest 6.2, added fallback; verified on 6.2.5 and 8.3.5; filed #7 (.gitignore dedupe)
2026-10-21 | issue #12 | planner ok | tester loops: 1 | reviewer loops: 2 (+1 review of HDMI default change) | PR #14 | loop 1 must-fix: stale reSpeaker docstring; Akama made HDMI the default output; filed #13 (persistent playback stream, low priority); conftest matches #11
