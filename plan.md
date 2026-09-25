# Tales: Build Plan

Last updated: 2026-09-23
Repo: https://github.com/paulegbe/tales-companion-robot

Start every session here. Update the Status and Next Session sections before stopping.

---

## Status

**Phase 1 (stationary brain):** conversation, memory, and vision working end to end.
**Phase 2 (voice + face):** started. Audio output hardware verified. No audio code yet.

**Stack on device:** Jetson Orin Nano Super 8GB, JetPack 6.2.1 (Ubuntu 22.04), ROS 2 Humble.
**Workspace:** `~/companion_ws`, package `companion_brain`.
**Side setup:** ROS 2 Jazzy on an Ubuntu 24.04 VM (learning/testing).

---

## Done today (2026-09-23)

- [x] HDMI audio output verified through the ROADOM screen speaker.
- [x] Working output device: `plughw:CARD=HDA,DEV=3`.
- [x] Decided: always pass the audio device explicitly. Plain `aplay` (ALSA default) does not reach HDMI, and fixing it is not worth the time.
- [x] Full repo review. Bugs logged below.

---

## Next session: start here

### 1. Finish audio hardware checks
- [ ] Both channels:
  `speaker-test -D plughw:CARD=HDA,DEV=3 -c2 -t wav -l1`
- [ ] Reboot persistence: `sudo reboot`, then replay `test.wav` with the explicit device.
- [ ] Mic loop (speak during the 5 seconds):
  ```bash
  arecord -D plughw:CARD=Array,DEV=0 -f S16_LE -r 16000 -c 2 -d 5 mic_test.wav
  aplay -D plughw:CARD=HDA,DEV=3 mic_test.wav
  ```
- [ ] Add the shell alias:
  ```bash
  echo "alias play='aplay -D plughw:CARD=HDA,DEV=3'" >> ~/.bashrc
  source ~/.bashrc
  ```
- [ ] Revert `/etc/asound.conf` to the original (`pcm "hw:APE,0"`, `card APE`) so it doesn't mislead later.

### 2. Fix bugs (before adding audio nodes)

**Priority 1: history trim breaks API calls** (`claude_bridge_node.py`)
- `conversation_history[-20:]` can cut mid tool-use exchange, leaving an orphan `tool_result` or assistant message first. Every later call then fails with a 400.
- Fix: after trimming, drop leading messages until the first is a `user` message with plain string content.

**Priority 2: vision merges different people after restart** (`vision_node.py`)
- `unnamed_person_counter` resets to 0 on launch. A new face becomes `unnamed_person_1`, `get_or_create_entity` returns the existing entity, and a stranger's embedding is attached to someone already stored.
- Fix: name placeholders from the entity id after insert, or seed the counter from the DB at startup.

**Priority 3: personality contradicts hardware** (`companion.md`)
- Says it will "gain a camera soon." It already has a camera and a vision tool. It can refuse to look or deny what it saw.
- Fix: update the opening line to the current hardware. Update again when voice lands.

**Smaller**
- [ ] Stale docstrings: `store_fact` ("nothing calls this yet") and `build_context_block` ("facts always empty"). Both are wrong now.
- [ ] Hardcoded personality path (`~/companion_ws/src/...`). Install `companion.md` via `data_files` in `setup.py` and load it with `get_package_share_directory`.
- [ ] Single tool round only. A second `tool_use` publishes an empty reply. Loop, capped at 2 to 3 rounds.
- [ ] Add `cv_bridge` to `package.xml`.

### 3. Build audio nodes
- [ ] `tts_node`: subscribes to `/companion_response`, synthesizes a WAV, plays with `aplay -D <audio_device>`.
  - ROS param `audio_device`, default `plughw:CARD=HDA,DEV=3`.
  - Decide TTS engine (local vs cloud) before starting.
- [ ] `stt_node`: replaces `text_input_node`, publishes to `/user_input`.
  - ROS param `mic_device`, default `plughw:CARD=Array,DEV=0`.
  - Decide STT engine and wake/push-to-talk approach.
- [ ] Register both in `setup.py` entry points.
- [ ] Order: TTS first (simpler, most visible payoff), then STT.

---

## Reference

### ALSA card map
Always use card **names**. Indices shift because USB devices enumerate first.

| Name | Device | Use |
|---|---|---|
| `Camera` | Arducam USB camera mic | Not used for audio |
| `Array` | reSpeaker XVF3800 4-Mic Array | Speech input, `DEV=0` |
| `HDA` | Jetson onboard HDMI/DP audio | Output, `DEV=3` goes to ROADOM screen |
| `APE` | Jetson onboard audio engine | Not used |

The ROADOM screen is the downstream HDMI sink, not its own ALSA card.

### ROS topics
| Topic | Type | Publisher | Subscriber |
|---|---|---|---|
| `/user_input` | String | `text_input_node` (later `stt_node`) | `claude_bridge_node` |
| `/companion_response` | String | `claude_bridge_node` | (later `tts_node`) |
| `/image_raw` | Image | `usb_cam` | `claude_bridge_node`, `vision_node` |
| `/vision_detections` | String | `vision_node` | logging |
| `/vision_annotated` | Image | `vision_node` | `rqt_image_view` |

---

## Session rules
- Keep sessions narrow: one problem per chat.
- Before stopping, update Status and Next Session here and commit.
- Start new chats by pointing Claude at this file and the repo.
