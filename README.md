# tales-companion-robot

# Companion Brain

The reasoning core of a personal AI companion robot, built as a portfolio project to learn ROS 2, embedded AI systems, and applied robotics on real hardware.

Currently a brain with no body: it thinks, remembers, and sees, but doesn't yet speak or move. Voice, a physical face, and mobility are planned for later phases.

## What it does

- **Converses via Claude**: text in, reasoned response out, using Anthropic's API
- **Remembers permanently, not just per-session**: facts and conversation history persist across full process restarts, backed by SQLite (structured facts/entities) and ChromaDB (semantic recall)
- **Sees on its own terms**: has access to a live camera feed and a vision tool it can invoke mid-conversation. It decides for itself when a question requires looking, rather than reflexively checking on every message
- **Recognizes people and objects independently of conversation**: a separate vision pipeline runs YOLO object detection and face recognition continuously, identifying and remembering faces it's seen before, even before they're named

## Architecture
             ┌──────────────────┐
/user_input │ │ /companion_response
──────────────▶│ claude_bridge_ │──────────────────────▶
(text/voice) │ node │ (text/speech)
│ │
│ Claude API │
│ vision tool │
│ fact extract │
└────────┬─────────┘
│
▼
┌──────────────────┐
│ memory_manager │
│ │
│ SQLite: │
│ entities │
│ observations │
│ attributes │
│ facts │
│ │
│ ChromaDB: │
│ conversations │
│ face_embeddings │
└──────────────────┘
▲
│
┌────────┴─────────┐
/image_raw │ │ /vision_detections
─────────────▶│ vision_node │─────────────────────▶
│ │
│ YOLOv8n │
│ face_recognition │
└──────────────────┘


Both `claude_bridge_node` and `vision_node` read and write to the same persistent memory store on disk, so entity data (people, facts, observations) is shared across the whole system regardless of which node captured it.

## Stack

- **Compute:** NVIDIA Jetson Orin Nano Super (8GB)
- **OS:** JetPack 6.2.1 (Ubuntu 22.04)
- **Middleware:** ROS 2 Humble
- **Reasoning:** Claude API (Anthropic), with native tool-use for vision
- **Object/person detection:** YOLOv8n (Ultralytics)
- **Face recognition:** `face_recognition` (dlib-based)
- **Structured memory:** SQLite, entity-based schema (entities, observations, attributes, relationships)
- **Semantic memory:** ChromaDB, local vector store for conversation recall and face embedding matching

## Design decisions worth noting

- **Personality lives in a text file (`companion.md`), not code.** Tone, behavior rules, and character can be changed by editing a paragraph, not touching Python.
- **Vision is a tool Claude chooses to use, not a keyword trigger.** The model decides mid-conversation whether a question needs current visual information and calls a `capture_and_view_snapshot` tool when it does, closer to how a person decides to look, rather than checking on every mention of "see."
- **The memory schema is entity-based, not flat key-value,** specifically so it scales: attributes are stored with full history (not overwritten), which is what will eventually make queries like "is this the same shirt as last time" or "has this object moved rooms" answerable without restructuring the database later.

## Status

**Phase 1 (current):** stationary. Conversation, memory, and vision working end-to-end on a desk setup, tested with a USB webcam ahead of the dedicated camera module arriving.

**Phase 2 (next):** voice input/output (microphone array + speaker), touchscreen face/display.

**Phase 3 (later):** mobility. Mecanum chassis, motor control, LiDAR-based navigation and spatial memory.

---

Built by [Paul Egbe](https://github.com/paulegbe), learning ROS 2 and robotics engineering hands-on.
