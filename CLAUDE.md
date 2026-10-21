# Tales: project rules for Claude Code

## What this is
Tales is a companion robot: ROS 2 Humble, Python 3.10, running on a Jetson Orin Nano
(JetPack 6.2.1, Ubuntu 22.04). This repo is the ROS 2 workspace `companion_ws`. The code
lives in the ament_python package at `src/companion_brain/` (nodes in
`src/companion_brain/companion_brain/`).
The build plan and current status live in `plan.md`. Read it before planning any task.

Owner and tech lead: Akama. He approves plans and merges PRs. Nothing merges without him.

## Where code runs
- Agents usually work on Akama's Mac, which does NOT have ROS installed.
- ROS integration and hardware tests run only on the Jetson. Akama runs those.
- Therefore: keep logic in plain Python modules with no ROS imports, and keep ROS nodes as
  thin wrappers around them. Pure logic must be testable with plain `pytest` on the Mac.

## Hard constraints
- System numpy must stay at 1.26.4 (numpy 2.x breaks cv_bridge on Humble). Never add a
  dependency that requires numpy 2.x to the ROS package. Engines that need it (e.g. Kokoro)
  live in their own venv behind a worker process.
- Pin ALSA devices by card NAME (`plughw:CARD=Array,DEV=0`), never by index.
- Build, THEN source: `colcon build --packages-select companion_brain && source install/setup.bash`
- Every node ends with `if rclpy.ok(): rclpy.shutdown()` in its finally block.
- Never commit secrets (API keys, tokens, SSH keys). `ANTHROPIC_API_KEY` comes from the environment.
- Never commit `voices/` (model files, venvs), `build/`, `install/`, `log/`, or audio files.
  Check `git status` before every commit.

## Code style
- Module docstring at the top of every node: what it does, data flow, parameters.
- Comments explain WHY, not what.
- Prefer clarity and separation of concerns over cleverness.
- In docs, comments, and PR text: no em dashes or double dashes.

## Tests
- Put tests in `src/companion_brain/test/` as `test_<module>.py`. Use pytest.
- Tests must not import rclpy or anything ROS. If a test needs ROS, mark it
  `@pytest.mark.ros` so it is skipped on the Mac.
- One-time setup on the Mac, from the repo root (Python 3.10 matches the Jetson):
  `brew install python@3.10 && python3.10 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Run from the repo root: `.venv/bin/python -m pytest src/companion_brain/test -m "not ros" -q`
- Without ROS, the ament linter tests (flake8, pep257, copyright) are reported as skipped.
  They run under `colcon test` on the Jetson.
- The Jetson runs apt pytest 6.2.5 with anthropic's dependencies (including anyio) installed.
  Write tests that work on pytest 6.2.5 too. `setup.cfg` disables the anyio plugin
  (`-p no:anyio`), which crashes pytest 6.2.5.
- On the Jetson, run the ament linter tests with `colcon test --packages-select companion_brain`.
  To run them with plain pytest, run from the package directory, because ament flake8 and
  pep257 scan the current directory: `cd src/companion_brain && python3 -m pytest test`.
- ament flake8 and pep257 cannot run on the Mac. Until CI covers them, follow their rules by
  hand in every new or changed file: max line length 99, single quotes, google import order,
  docstring summaries in the imperative ending in a period, and multi-line docstrings with
  the summary on the second line (D213): `"""` on its own line, then the summary. Separately,
  project style requires a docstring on every module and public function.

## Git
- Never commit to `main`. Branch per issue: `issue-<number>-<short-slug>`.
- Small, focused commits. Commit messages in the imperative ("Add presence debounce").
- PR description: what changed, why, how it was tested, and what Akama must verify on the
  Jetson.

## The dev team pipeline
When Akama says "run the pipeline on issue #N" (or gives a task):

1. **Fetch** the issue: `gh issue view N`.
2. **Plan:** delegate to the `planner` agent. Show Akama the plan and STOP. Wait for his
   approval or changes. Do not write code before approval.
3. **Branch:** `git checkout -b issue-N-<slug>` from an up-to-date `main`.
4. **Code:** implement the approved plan yourself (main session).
5. **Test:** delegate to the `tester` agent. If tests fail because of the code, fix the code
   and re-run the tester. Max 3 loops, then stop and report to Akama.
6. **Review:** delegate to the `reviewer` agent. Fix every "must fix" item, then re-run the
   reviewer. Max 2 loops, then stop and report.
7. **PR:** push the branch and open a PR with `gh pr create`, following the PR rules above.
8. **Log:** append one line to `devteam_log.md`:
   `YYYY-MM-DD | issue #N | planner ok | tester loops: X | reviewer loops: Y | PR #M | notes`
9. Stop. Akama reviews and merges.

If anything is ambiguous at any step, ask Akama one question rather than guessing.
