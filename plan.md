# Tales: Build Plan

Last updated: 2026-09-30 (evening: life system + dev team plan)
Repo: https://github.com/paulegbe/tales-companion-robot

Start every session here. Update Status and Next Session before stopping, then commit.

---

## End goal

Tales is an embodied companion that notices, remembers, decides, and acts on its own
initiative, within bounds Akama sets.

Core loop: **perceive → remember → decide → act → learn.** Today Tales runs this loop only
when spoken to. The goal is running it continuously, on its own initiative.

### Autonomy ladder

| Level | Tales can... | Status |
|---|---|---|
| 1. Reactive | Respond when spoken to | **Done** |
| 2. Aware | Notice events and choose to act (greetings) | **Next** |
| 3. Self-directed | Set small goals ("learn this new person's name") | Planned |
| 4. Curious | Seek information it lacks (ask, look around) | Needs pan/tilt head |
| 5. Embodied | Explore and map its space | Needs chassis |
| 6. Reflective | Review its own day, write conclusions to memory | Software, can slot in anytime |

Each level reuses the one below. Don't design motion logic before the chassis exists.

**End-state demo:** Akama walks in after a day out. Tales notices, greets him by name, mentions
something from yesterday, notices a stranger with him, introduces itself, learns their name,
and remembers them tomorrow.

**Bounds (decide early):** things Tales may never do on its own, e.g. spend money, contact
people, move near stairs.

---

## Life system: Tales as chief of staff

Tales is one voice and one memory that routes work to **departments**, each staffed by
specialist agents. Akama is the CEO: approves and decides.

```
                  Akama (approve, decide)
                            │
               Tales (chief of staff: voice, memory, routing, supervision)
                            │
   ┌────────────┬───────────┼────────────┬──────────────┐
 Dev Team    Job Desk    Trade Desk  Creative Studio  Shared services
                                                      memory, phone push,
                                                      scheduler, approvals
```

**Rules for every department**
- Agents draft, Akama approves, then they act. No agent submits an application, sends a
  message, places a trade, or merges code without approval.
- Autonomy grows per department as trust is earned (same idea as the autonomy ladder).
- Measured by real output (merged PRs, applications sent, trades journaled, shoots booked),
  never by agents built.
- A department doesn't start until the previous one is used daily for a week.

**Shared foundation: the consistency engine.** Ritual (check-in) + checklist (gate) +
journal (log) + score (consistency %) + nudges. Built once, generic. Trade Desk and Job Desk
are configurations of it.

**Where agents run:** Dev Team agents run in Claude Code (laptop or cloud). Life agents run on
the Jetson as ROS nodes using the same tool-loop pattern as the bridge, publishing to
`/proactive_events`. A new agent is a new node; nothing else changes.

### Department order
1. **Dev Team** (now). Accelerates building everything after it.
2. **Consistency engine + Job Desk.** Income first.
3. **Trade Desk** (second configuration of the engine). Full design: `docs/trade_desk_design.md`.
4. **Creative Studio.**

Rung 2 (presence/greetings) and the wake word become the Dev Team's first real work items.

### Department summaries

**Dev Team:** planner, coder (main session), tester, reviewer, later release and docs agents.
Akama is tech lead. GitHub is the shared workspace. Tales supervises and reports. See
"Next session".

**Job Desk**
| Agent | Job | Autonomy |
|---|---|---|
| Scout | Daily scan for matching roles (robotics, Python, Southeast or remote) | Autonomous, read-only |
| Tailor | Drafts resume/cover letter variants. No fabricated skills, honest gaps | Drafts only |
| Tracker | Pipeline state, follow-up timing | Nudges |
| Interview coach | Spoken mock interviews (behavioral, Python, ROS) with feedback | Interactive |
| Networker | Follow-up reminders for warm contacts | Nudges |
Ritual: morning job desk ("3 new matches, target 2 applications"). Scored on submissions.

**Trade Desk** (accountability layer, not a trading bot)
- v1: morning check-in, alert checklist, decision journal, consistency score, nudges. Voice only.
- v1.5: phone notifier, price watcher (code, not an agent), alert context agent that
  pre-answers the objective checklist items (news, extended, zone valid).
- v2: pre-market prep agent, review agent, optional TradingView webhook hub.
- Guardrails: never recommends, predicts, or executes. No broker credentials on the Jetson.
  Driving = log skipped, end interaction.

**Creative Studio:** client pipeline, shoot prep checklist, content scheduler (Muse, studio),
research/moodboard, story partner for film and capsule concepts.

---

## Status

**Phase 1 (stationary brain):** done. Conversation, memory, vision, face recognition.
**Phase 2 (voice):** done. Natural turn-taking (smart endpointing) and Kokoro GPU voice.
**Phase 3 (autonomy + life system):** Dev Team first, then departments in order above.

**Stack:** Jetson Orin Nano Super 8GB, JetPack 6.2.1 (Ubuntu 22.04, CUDA 12.6), ROS 2 Humble,
Python 3.10. Claude API (`claude-sonnet-5`), YOLOv8 + face_recognition, SQLite + ChromaDB,
faster-whisper STT, Kokoro TTS (Piper fallback).
**Workspace:** `~/companion_ws`, package `companion_brain`.

### Nodes
| Node | Role |
|---|---|
| `claude_bridge_node` | Streams Claude replies sentence by sentence. Tools: snapshot, who_is_here, name_person, remember_fact, forget_fact. MultiThreadedExecutor |
| `stt_node` | faster-whisper `base.en` + webrtcvad + energy gate + smart endpointing |
| `tts_node` | Kokoro on GPU via `kokoro_worker.py`, Piper fallback, pipelined synth/playback |
| `vision_node` | YOLOv8 + face recognition, publishes `/vision_people` |
| `usb_cam` | Camera driver, `/dev/video0` |

---

## Done this session (2026-09-29 to 30)

### Smart endpointing (`stt_node.py`)
- Problem: a fixed 0.8 s silence timer cut Akama off mid-thought; Claude answered fragments.
- Fix: 0.6 s pause triggers a completeness check on the transcript so far. Ends in a
  conjunction, filler, preposition, comma, or lacks final punctuation → keep listening.
  3 s silence always ends the turn. Max utterance raised 15 s → 60 s.
- Check transcript is reused as the final transcript for utterances up to 8 s (no second
  Whisper pass).
- Energy-gate hysteresis: `min_rms` to start a turn, `min_rms * 0.5` to continue it.
- Params: `soft_silence_ms` 600, `hard_silence_ms` 3000, `check_tail_s` 8,
  `continue_rms_ratio` 0.5. Old `silence_ms` removed.
- Known limit: a pause after a complete sentence ends the turn. Follow-up if it bites:
  fragment merging in the bridge.

### Kokoro voice (`tts_node.py` + `kokoro_worker.py`)
- Kokoro needs numpy ≥ 2.0; ROS Humble's cv_bridge needs numpy 1.26.4. Solved with a
  separate venv and a worker process (JSON lines over stdin/stdout).
- Benchmarks (fp16 model, Orin Nano):

  | Setup | First sentence | RTF |
  |---|---|---|
  | CPU int8 | 4.3 to 5.1 s | 2.3 (int8 is slow on ARM, don't use) |
  | CPU fp16 | 1.6 to 1.9 s | 0.73 to 0.87 |
  | GPU fp16, uncapped | 0.51 to 0.62 s | 0.23, NvMap out-of-memory errors |
  | **GPU fp16, 1024 MB cap** | **0.46 to 0.52 s** | **0.20, no errors** |

- Failure handling: bad sentence skipped; dead worker restarted once; then falls back to Piper.
- Params: `engine` (kokoro/piper), `kokoro_voice` (default `af_heart`), `kokoro_speed`,
  `kokoro_provider`, `kokoro_gpu_mem_mb` (1024).
- Verified working in live conversation.

### Deferred
- Custom Piper voice trained on Akama's recordings moved to a later project. Recording tool
  and prompts are in `voice_dataset/`.

---

## Next session: start here

### 0. Prerequisites (blocking for Dev Team)
- [ ] **GitHub SSH key.** GitHub is the Dev Team's shared workspace, so push must work.
  ```bash
  ssh-keygen -t ed25519 -C "tales-jetson"
  cat ~/.ssh/id_ed25519.pub   # add at github.com > Settings > SSH keys
  cd ~/companion_ws/src/companion_brain
  git remote set-url origin git@github.com:paulegbe/tales-companion-robot.git
  git push
  ```
- [ ] Same for the laptop if agents run there.
- [ ] Install Claude Code on the laptop (docs: https://code.claude.com/docs).
- [ ] Check storage: `df -h /` and `lsblk`. If `mmcblk0` (microSD), plan an NVMe SSD.

### 1. Dev Team (about 4 weekends)

**How it works**
- Built with **Claude Code subagents**: markdown files in `.claude/agents/`, each with its own
  role prompt, tool permissions, and model. Configuration, not a framework.
- Subagents can't spawn subagents, so the **main Claude Code session is the orchestrator and
  coder**. A custom command runs the pipeline.
- `CLAUDE.md` in the repo: project rules (ROS Humble, numpy pin, build-then-source, no em
  dashes in docs, test before PR).

**Team v1 (start small)**
| Agent | Role | Tools |
|---|---|---|
| Main session | Orchestrator + coder | Full |
| `planner` | Issue → plan with acceptance criteria and test list | Read-only |
| `tester` | Writes and runs tests, reports failures | Read, write tests, run pytest |
| `reviewer` | Reviews diff: bugs, security, maintainability, project rules | Read-only |
Later: `release` (CI, deploy), `docs` (plan.md, READMEs).

**Pipeline**
```
GitHub issue → planner → AKAMA approves plan → coder on a branch → tester
  → reviewer → fixes loop back → PR → AKAMA reviews and merges → plan.md updated
```

**Testing reality for ROS:** pure-Python logic (flow engines, scoring, parsers) is tested with
pytest anywhere. ROS integration and hardware tests run on the Jetson. Design code so most
logic is plain Python behind thin ROS nodes.

**Tales supervision ("Tales, how's the dev team doing?")**
- `devteam_monitor_node` on the Jetson.
- Source 1 (v1): GitHub API. Open issues, PRs, review state, CI results. Works wherever agents run.
- Source 2 (v2): Claude Code hooks write lifecycle events (agent started/finished, tests run)
  as JSON lines; the monitor ingests them. Finer-grained "what is each agent doing now."
- Stores events in SQLite, exposes a `dev_team_status` tool to the bridge.
- Proactive: daily spoken standup, alert when CI breaks or a PR waits on review.

**Efficiency metrics (per task)**
| Metric | Definition |
|---|---|
| Cycle time | Issue opened → PR merged |
| First-pass test rate | Tests green on first tester run |
| Review rounds | Reviewer → coder loops before PR |
| Human touches | Times Akama had to intervene beyond approve/merge |
| Rework rate | Merged PRs needing a follow-up fix within 7 days |
| Cost | Tokens/dollars per task (from session logs/hooks, v2) |

**Timeline**
| When | Deliverable | Done when |
|---|---|---|
| Evening before | Prerequisites (section 0), `CLAUDE.md`, pytest in the package | `git push` works; `pytest` runs |
| Weekend 1 | `planner`, `tester`, `reviewer` + pipeline command. First real issue end to end | One PR merged through the full pipeline |
| Weekend 2 | GitHub Actions CI (pytest on push), branch protection. 2 to 3 more issues. Tune prompts from failures | CI green; 3 PRs merged |
| Weekend 3 | `devteam_monitor_node` (GitHub source) + `dev_team_status` tool + daily standup | Asking Tales returns accurate status |
| Weekend 4 | Hooks telemetry, efficiency metrics, weekly report, delegating from phone | Weekly report with real metrics |
Usable from weekend 1. Prompt tuning never fully ends; that's normal.

**First issues for the team** (small, real, low risk)
1. Add `cv_bridge` to `package.xml`.
2. Install `companion.md` via `data_files`, load with `get_package_share_directory`.
3. Move `store_turn` to a background thread.
4. Then bigger: rung 2 `presence_node`, then the wake word.

### 2. Rung 2 and wake word (built by the Dev Team)

**Rung 2: presence and greetings.** Code notices, Claude decides.
- [ ] `presence_node`: subscribes `/vision_people`, publishes `/presence_events` (`arrived`,
      `returned`, `first_today`, `stranger_arrived`, `left`). Last-seen from observations table.
- [ ] Debounce: visible ~2 s = arrived, gone ~60 s = left.
- [ ] Bridge proactive turn via `/proactive_events`; `stay_silent` tool; no interrupting;
      per-person cooldown.
- [ ] Strangers: greet, ask name, `name_person`.

**Wake word: "Hey Tales."**
- [ ] openWakeWord, custom model from synthetic speech. Gate in `stt_node` before VAD.
- [ ] Conversation window: open ~20 s after Tales's last reply. Proactive greetings open it too.
- [ ] Publish `/listening_state`. Tune threshold (TV false triggers vs misses). `wake_word:=false` param.

### 3. Backlog (not blocking)
(`cv_bridge`, `companion.md` path, and `store_turn` moved to Dev Team first issues.)
- [ ] **Single ChromaDB owner.** Bridge and vision each open a `PersistentClient` on the same
      folder; `presence_node` adds a reader. Fix: one `memory_node` with ROS services.
      Strong portfolio piece.
- [ ] Facts grow unbounded; every fact goes into every prompt. Retrieve relevant facts only.
- [ ] Rung 6 reflection: nightly job reviews the day, writes conclusions to memory.
- [ ] Fragment merging in the bridge, if endpointing still cuts off after complete sentences.
- [ ] `companion.md` speech style: contractions, short sentences, no asides (helps any voice).
- [ ] Custom voice (later project), see `voice_dataset/`.
- [ ] Hardware/battery phase. Brownout risk: Orin draws ~25 W in MAXN SUPER; undersized
      power causes random reboots that look like software bugs.

---

## Reference

### Hardware
| Part | Role | Status |
|---|---|---|
| Jetson Orin Nano Super 8GB | Compute | Running |
| Arducam day/night USB camera | Vision | Working, `/dev/video0` |
| reSpeaker XVF3800 4-Mic Array | Mic in + speaker out | Working. Echo cancellation lets TTS play through it without self-transcription |
| ROADOM screen | Face display | Audio verified but unused. Face UI not built |
| Battery | Power | On hand, not wired |

### ALSA card map (always use names, never indices)
| Name | Device | Use |
|---|---|---|
| `Array` | reSpeaker XVF3800 | In and out, `plughw:CARD=Array,DEV=0` |
| `Camera` | Arducam mic | Unused |
| `HDA` | Jetson HDMI | Unused (was `DEV=3` to screen) |
| `APE` | Onboard audio engine | Unused |

- PulseAudio masked: `systemctl --user mask pulseaudio.socket pulseaudio.service`
  (undo with `unmask`). Desktop apps have no audio; ROS nodes get exclusive device access.
- `/etc/asound.conf` is back to original. Always pass `-D` explicitly.
- Alias: `alias play='aplay -D plughw:CARD=Array,DEV=0'`

### ROS topics
| Topic | Type | Publisher | Subscriber |
|---|---|---|---|
| `/user_input` | String | `stt_node` | `claude_bridge_node` |
| `/companion_speech` | String (per sentence) | `claude_bridge_node` | `tts_node` |
| `/companion_response` | String (full reply) | `claude_bridge_node` | logging |
| `/tts_speaking` | Bool | `tts_node` | `stt_node` |
| `/image_raw` | Image | `usb_cam` | `claude_bridge_node`, `vision_node` |
| `/vision_people` | String (JSON) | `vision_node` | `claude_bridge_node` |
| `/vision_annotated` | Image | `vision_node` | `rqt_image_view` |
| `/presence_events` | String (JSON) | `presence_node` (planned) | `claude_bridge_node` |
| `/listening_state` | String | `stt_node` (planned) | face UI (future) |
| `/proactive_events` | ProactiveEvent | skills, presence (planned) | `claude_bridge_node` |
| `/trade_desk/alerts` | TradeAlert | bridge, v2 hub (planned) | `trade_desk_node` |

### Kokoro setup
```
~/companion_ws/voices/kokoro/
  kokoro-v1.0.fp16.onnx     # use this
  kokoro-v1.0.int8.onnx     # slow on ARM, unused
  voices-v1.0.bin
  .venv-gpu/                # Python 3.10, numpy 2.x, onnxruntime-gpu 1.24 (Jetson AI Lab)
  .venv/                    # Python 3.12, CPU only, benchmark leftovers
  kokoro_bench.py
```
Rebuild `.venv-gpu`:
```bash
cd ~/companion_ws/voices/kokoro
python3 -m uv venv -p 3.10 .venv-gpu
python3 -m uv pip install --python .venv-gpu/bin/python kokoro-onnx soundfile
python3 -m uv pip uninstall --python .venv-gpu/bin/python onnxruntime
python3 -m uv pip install --python .venv-gpu/bin/python \
  --extra-index-url https://pypi.jetson-ai-lab.io/jp6/cu126/+simple/ onnxruntime-gpu
```
Never let system numpy leave 1.26.4 (breaks cv_bridge).

### Run commands
```bash
cd ~/companion_ws && colcon build --packages-select companion_brain && source install/setup.bash   # build, THEN source
ros2 run companion_brain tts_node --ros-args -p kokoro_voice:=af_heart
ros2 run companion_brain stt_node --ros-args -p vad_aggressiveness:=3 -p min_rms:=<measured>
ros2 run rqt_image_view rqt_image_view   # topics /image_raw or /vision_annotated
```

### Key decisions
- Claude model: `claude-sonnet-5`. Haiku was faster but ignored speech rules and confabulated.
- Pin ALSA devices by card name.
- JSON in `std_msgs/String` for now; a proper interface package later.
- Face distance: ChromaDB `l2` is squared, so `sqrt` before comparing to 0.6.
- Engine dependencies live in their own venvs so they can't break ROS.
- Run Cursor on the laptop via Remote-SSH, not on the Jetson (caused 901 MB swap).

---

## Session rules
- One problem per chat. Long chats hit the session limit and lose context.
- Before stopping: update Status and Next Session, then commit.
- Start new chats by pointing Claude at this file and the repo.
