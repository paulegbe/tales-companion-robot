"""
claude_bridge_node.py

Bridges ROS 2 text input to the Claude API and back, with vision exposed
as a tool Claude can choose to use.

Flow:
    /user_input (String) --> ClaudeBridgeNode --> /companion_speech (String, per sentence)
                                              --> /companion_response (String, full reply)

What changed in this version (speed pass):
    1. STREAMING. Claude's reply is streamed token by token. As soon as a
       full sentence arrives, it's published on /companion_speech so
       tts_node can start speaking while the rest is still being written.
       /companion_response still carries the complete reply, for logs and
       anything else that wants the whole thing.
    2. TIMING LOGS. Every turn logs how long each stage took: memory
       lookup, time to first sentence, total API time, and memory storage.
    3. MODEL PARAMETER. Switch models without editing code, e.g.
       -p model:=claude-haiku-4-5
    4. MULTI-ROUND TOOLS. Claude can use the vision tool more than once per
       turn (capped), instead of the second request producing an empty
       reply.
    5. HISTORY TRIM FIX. Trimming history can no longer leave an orphaned
       tool_result or assistant message at the front, which made every
       later API call fail.
    6. CHEAPER CAMERA BUFFER. Frames are stored raw and only converted
       when Claude actually asks to look, instead of converting all 30
       frames per second.
    7. FACE RECOGNITION TOOLS. vision_node's identity results now reach
       Claude. who_is_here reports who vision_node recognizes in view;
       name_person attaches a name to a face the user identifies.
    8. MEMORY WARM-UP. The embedding model loads at startup, so the first
       message doesn't pay a ~1.5 s cold-start cost.
    9. CONCURRENT SENSOR CALLBACKS. Camera frames and face data now keep
       updating while a turn is being handled (MultiThreadedExecutor +
       callback groups). Before, the node was single-threaded: during a
       turn, no new messages were processed, so who_is_here and the
       snapshot tool saw data frozen at the moment you started talking.
   10. FACT TOOLS. remember_fact and forget_fact replace inline
       [FACT: key=value] tags as the main way to save facts. Tags were
       unreliable: Claude sometimes said "got it, saved" without emitting
       one, and the speech rule (no symbols) competes with them. A tool
       call is structured, always logged, and can't be faked. Tag parsing
       is kept as a fallback.
   11. CLEAN SHUTDOWN. Ctrl+C no longer ends in an RCLError traceback.
   12. INTERVIEW PRACTICE. "Tales, do a recruiter screen for <company>,
       <role>" makes Claude call start_interview. The bridge looks up a
       cached company brief (SQLite, 30 days) or researches the company
       with server-side web search, then Claude role-plays a phased
       interview grounded in the resume and ends with an out-of-character
       debrief that is saved. Logic lives in plain modules:
       interview_phases, interview_context, company_research,
       interview_store, resume_loader. Tools: start_interview,
       interview_advance, end_interview, save_interview_debrief,
       refresh_company_research.

Per turn:
    1. Pull relevant memory context (facts + similar past turns)
    2. In an interview: "end interview" jumps to the debrief; the turn is
       counted for the current phase
    3. Stream Claude's reply, publishing each finished sentence. The system
       prompt is rebuilt every round, so a phase change made by a tool shows
       up in the follow-up
    4. If Claude requests tools, run them, send the results back, and stream
       the follow-up (up to MAX_TOOL_ROUNDS)
    5. Parse [FACT: key=value] tags, store them, keep them out of speech
    6. Publish the full reply and store the turn in memory. Interview turns
       skip both fact tags and turn storage, so roleplay never reaches
       long-term memory

Parameters:
    model             Claude model for conversation (default claude-sonnet-5)
    max_tokens        reply cap per call (default 500)
    people_stale_s    max age of /vision_people data (default 3.0)
    resume_path       resume PDF (default ~/companion_ws/data/resume.pdf)
    research_model    model for company research ('' means use model)
    scenarios_dir     persona cards ('' means the installed share directory)
    debrief_max_tokens reply cap during the debrief phase (default 1200)
"""

import base64
import copy
import json
import os
import re
import time

from ament_index_python.packages import get_package_share_directory
import anthropic
import cv2
import rclpy
from cv_bridge import CvBridge
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import String

from companion_brain.company_research import CompanyResearcher, get_brief, normalize_company
from companion_brain.interview_context import (
    build_interview_block, compose_system_prompt, DEFAULT_PERSONA, load_scenario, PERSONAS,
)
from companion_brain.interview_phases import (
    InterviewSession, is_end_interview_command, is_interview_turn, PHASE_GUIDANCE,
)
from companion_brain.interview_store import InterviewStore
from companion_brain.memory_manager import MemoryManager
from companion_brain.resume_loader import load_resume_text


# Tool definition Claude sees on every call. Claude decides on its own,
# based on this description, whether a message warrants looking.
VISION_TOOL = {
    "name": "capture_and_view_snapshot",
    "description": (
        "Captures a single current frame from the robot's camera and "
        "returns it for viewing. Use this whenever answering the user's "
        "question requires knowing what is currently visible, for "
        "example questions about what they're wearing, holding, what "
        "object is in front of the camera, who is present, or the "
        "current state of the room. Do not use this for questions that "
        "don't require current visual information."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

WHO_IS_HERE_TOOL = {
    "name": "who_is_here",
    "description": (
        "Reports which people your face recognition currently sees in "
        "view: known people by name, and unidentified faces with an id. "
        "Use this when asked who is present, whether you recognize "
        "someone, or before naming someone. It is faster than a snapshot "
        "and is the only way to know identities; a snapshot shows what "
        "people look like but not who they are."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

NAME_PERSON_TOOL = {
    "name": "name_person",
    "description": (
        "Attaches a name to a face you see, so you recognize that person "
        "from now on. Only use this when the user explicitly tells you who "
        "someone is (e.g. 'that's me, Paul' or 'this is my friend Clinton'). "
        "Never guess a name. Call who_is_here first to get the entity_id. "
        "If someone with that name already exists, the faces are merged, "
        "which improves recognition."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "integer",
                "description": "The id from who_is_here of the face to name.",
            },
            "name": {
                "type": "string",
                "description": "The person's name, as the user gave it.",
            },
        },
        "required": ["entity_id", "name"],
    },
}

REMEMBER_FACT_TOOL = {
    "name": "remember_fact",
    "description": (
        "Saves a durable fact about the user or their life to long-term "
        "memory, so you still know it after restarts. Use it whenever the "
        "user shares something concrete worth recalling later: names, "
        "preferred name, preferences, people in their life, where they "
        "live, important details. Also use it to update a fact that "
        "changed. Never claim you've remembered something without calling "
        "this. Don't save guesses or temporary states."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Short snake_case label, e.g. preferred_name, "
                    "favorite_animal, current_city. Reuse an existing key "
                    "to update it."
                ),
            },
            "value": {"type": "string", "description": "The fact itself."},
        },
        "required": ["key", "value"],
    },
}

FORGET_FACT_TOOL = {
    "name": "forget_fact",
    "description": (
        "Deletes a saved fact. Use when the user says a stored fact is "
        "wrong or asks you to forget it. Use the exact key shown in your "
        "memory's known facts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "The key to delete."},
        },
        "required": ["key"],
    },
}

START_INTERVIEW_TOOL = {
    "name": "start_interview",
    "description": (
        "Starts a spoken mock interview practice session, for example when the "
        "user says 'do a recruiter screen for Acme, junior Python developer'. "
        "Call it right away without saying anything first: Tales announces the "
        "company lookup itself if research is needed. After it returns, begin "
        "in character with the rapport phase."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "Company name as the user said it."},
            "role": {"type": "string", "description": "The role being interviewed for."},
            "persona": {
                "type": "string",
                "enum": list(PERSONAS),
                "description": (
                    "Interviewer type. recruiter_screen unless the user asks for a "
                    "hiring manager, technical, or behavioral interview."
                ),
            },
        },
        "required": ["company", "role"],
    },
}

INTERVIEW_ADVANCE_TOOL = {
    "name": "interview_advance",
    "description": (
        "Moves the interview to the next phase. Call it when the current phase's "
        "goal is met, before starting the next phase. Phases only move forward one "
        "step at a time."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

END_INTERVIEW_TOOL = {
    "name": "end_interview",
    "description": (
        "Ends the interview early and jumps to the debrief. Use when the user "
        "asks to stop or end the interview in any wording."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

SAVE_DEBRIEF_TOOL = {
    "name": "save_interview_debrief",
    "description": (
        "Saves the debrief after you have spoken it, so future practice sessions "
        "can target weak spots. Only valid in the debrief phase. Ends the session."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "description": "Overall score from 1 to 10."},
            "summary": {
                "type": "string",
                "description": "Two or three sentences: weakest areas and what to practice.",
            },
        },
        "required": ["score", "summary"],
    },
}

REFRESH_RESEARCH_TOOL = {
    "name": "refresh_company_research",
    "description": (
        "Forces fresh web research on a company for interview practice, ignoring "
        "the cache. Use when the user says 'refresh research on <company>'. Call it "
        "without saying anything first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "Company name."},
            "role": {"type": "string", "description": "Role, if known."},
        },
        "required": ["company"],
    },
}

TOOLS = [
    VISION_TOOL, WHO_IS_HERE_TOOL, NAME_PERSON_TOOL,
    REMEMBER_FACT_TOOL, FORGET_FACT_TOOL,
    START_INTERVIEW_TOOL, INTERVIEW_ADVANCE_TOOL, END_INTERVIEW_TOOL,
    SAVE_DEBRIEF_TOOL, REFRESH_RESEARCH_TOOL,
]

# Max tool calls in one turn. Stops a runaway loop if Claude keeps asking
# to look.
MAX_TOOL_ROUNDS = 3

# Max messages kept in conversation history. An interview runs longer and
# follow-ups need earlier answers, so it gets a larger window.
MAX_HISTORY = 20
MAX_HISTORY_INTERVIEW = 40

# Spoken while research runs, so Tales is not silent for a minute.
RESEARCH_START_LINE = 'Give me a minute to look them up.'
RESEARCH_SLOW_LINE = 'Still looking, almost there.'
RESEARCH_SLOW_S = 30.0
# Research blocks the input thread, so bound it. The web search step has a
# hard 120 s deadline for all its calls combined; past that, the brief comes
# from general knowledge (not cached). No SDK retries, since they would
# multiply the wait, and the fallback call gets its own short timeout.
RESEARCH_TIMEOUT_S = 120.0
RESEARCH_DEADLINE_S = 120.0
RESEARCH_FALLBACK_TIMEOUT_S = 30.0

# User turns allowed in the debrief before the session closes on its own,
# in case save_interview_debrief never succeeds.
DEBRIEF_MAX_TURNS = 3

# [FACT: key=value] tags Claude embeds to save facts.
FACT_PATTERN = re.compile(r'\[FACT:\s*([^=\]]+)=([^\]]+)\]')

# A sentence ends at . ! or ? (optionally followed by quotes or brackets),
# then whitespace. Newlines also end a chunk.
SENTENCE_END = re.compile(r'([.!?]["\')\]]*)\s+|\n+')

# Markdown symbols that sound wrong when read aloud.
MARKDOWN = re.compile(r'[*_#`>]+|^\s*[-\u2022]\s+', re.MULTILINE)


class ClaudeBridgeNode(Node):
    """Connects /user_input to Claude and streams replies out sentence by sentence."""

    def __init__(self):
        super().__init__('claude_bridge_node')

        # ---- Parameters ----------------------------------------------------
        self.declare_parameter('model', 'claude-sonnet-5')
        self.declare_parameter('max_tokens', 500)
        # How old /vision_people data can be before it counts as stale.
        self.declare_parameter('people_stale_s', 3.0)
        self.model = self.get_parameter('model').value
        self.max_tokens = self.get_parameter('max_tokens').value
        self.people_stale_s = self.get_parameter('people_stale_s').value
        self.declare_parameter('resume_path', '~/companion_ws/data/resume.pdf')
        self.declare_parameter('research_model', '')
        self.declare_parameter('scenarios_dir', '')
        self.declare_parameter('debrief_max_tokens', 1200)
        self.debrief_max_tokens = self.get_parameter('debrief_max_tokens').value
        research_model = self.get_parameter('research_model').value or self.model
        self.scenarios_dir = (
            self.get_parameter('scenarios_dir').value
            or os.path.join(get_package_share_directory('companion_brain'), 'scenarios')
        )

        self.client = anthropic.Anthropic()
        self.conversation_history = []
        self.system_prompt = self.load_personality()
        self.memory = MemoryManager()

        # ---- Interview practice --------------------------------------------
        # Resume text is loaded once here, but only enters the system prompt
        # while an interview session is active.
        resume_path = self.get_parameter('resume_path').value
        self.resume_text, origin = load_resume_text(resume_path)
        if self.resume_text:
            self.get_logger().info(f'Resume loaded: {len(self.resume_text)} chars ({origin}).')
        else:
            self.get_logger().warn(
                f'No resume ({origin}) at {resume_path}. Interviews run without it.'
            )
        self.interview_store = InterviewStore(self.memory.db_path)
        self.researcher = CompanyResearcher(
            self.client.with_options(timeout=RESEARCH_TIMEOUT_S, max_retries=0),
            research_model,
            log=self.get_logger().info,
            deadline_s=RESEARCH_DEADLINE_S,
            fallback_timeout_s=RESEARCH_FALLBACK_TIMEOUT_S,
        )
        self.interview = None
        self.interview_scenario = None
        self.past_debriefs = []
        self.debrief_saved_this_turn = False

        # Warm up ChromaDB's embedding model now, so the first real
        # message doesn't pay the ~1.5 s model-load cost.
        self.get_logger().info('Warming up memory search...')
        self.memory.retrieve_similar_turns('warm up')

        # ---- Callback groups -----------------------------------------------
        # Two groups, run on separate threads by the MultiThreadedExecutor
        # in main(). Sensor callbacks (camera, face data) keep updating
        # while handle_input is busy with a multi-second turn. Each group
        # is mutually exclusive internally, so handle_input never runs
        # twice at once, which keeps conversation history consistent.
        self.sensor_group = MutuallyExclusiveCallbackGroup()
        self.input_group = MutuallyExclusiveCallbackGroup()

        # ---- Camera buffer -------------------------------------------------
        # Keep the latest RAW message only. Converting to an OpenCV image
        # costs CPU, so it's done once, when Claude asks to look, rather
        # than 30 times per second.
        self.bridge = CvBridge()
        self.latest_image_msg = None
        self.create_subscription(
            Image, '/image_raw', self.handle_image, 10,
            callback_group=self.sensor_group,
        )

        # ---- Face recognition feed from vision_node ------------------------
        # Latest "who is in view" snapshot, and when it arrived.
        self.latest_people = None
        self.latest_people_time = None
        self.create_subscription(
            String, '/vision_people', self.handle_people, 10,
            callback_group=self.sensor_group,
        )

        # ---- Input and outputs --------------------------------------------
        self.create_subscription(
            String, '/user_input', self.handle_input, 10,
            callback_group=self.input_group,
        )
        # Full reply, published once per turn.
        self.response_pub = self.create_publisher(String, '/companion_response', 10)
        # One message per sentence, for tts_node.
        self.speech_pub = self.create_publisher(String, '/companion_speech', 10)

        self.get_logger().info(
            f'Claude bridge node ready (model: {self.model}, '
            'tools: snapshot, who_is_here, name_person, remember_fact, forget_fact, '
            f'interview practice; research model: {research_model}).'
        )

    # ------------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------------

    def handle_image(self, msg):
        """Store the latest camera message. Conversion happens on demand."""
        self.latest_image_msg = msg

    def handle_people(self, msg):
        """Cache vision_node's latest who-is-in-view snapshot."""
        try:
            self.latest_people = json.loads(msg.data)
            self.latest_people_time = time.monotonic()
        except json.JSONDecodeError:
            self.get_logger().warn('Bad /vision_people message, ignored.')

    def load_personality(self):
        """Read companion.md as plain text for the system prompt."""
        personality_path = os.path.expanduser(
            '~/companion_ws/src/companion_brain/companion_brain/companion.md'
        )
        try:
            with open(personality_path, 'r') as f:
                return f.read()
        except FileNotFoundError:
            self.get_logger().warn('companion.md not found, using default prompt.')
            return 'You are a helpful robot companion.'

    def capture_snapshot_base64(self):
        """Convert the latest frame to base64 JPEG, or None if no frame yet."""
        if self.latest_image_msg is None:
            return None
        frame = self.bridge.imgmsg_to_cv2(self.latest_image_msg, desired_encoding='bgr8')
        success, buffer = cv2.imencode('.jpg', frame)
        if not success:
            return None
        return base64.b64encode(buffer).decode('utf-8')

    # ------------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------------

    def tool_snapshot(self):
        """capture_and_view_snapshot: current frame as an image block."""
        image_b64 = self.capture_snapshot_base64()
        if image_b64 is None:
            return [{'type': 'text', 'text': 'No camera frame is currently available.'}]
        return [{
            'type': 'image',
            'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': image_b64},
        }]

    def tool_who_is_here(self):
        """who_is_here: describe vision_node's latest identity snapshot."""
        if self.latest_people_time is None:
            return 'No face recognition data. The face recognition system is not running.'

        age = time.monotonic() - self.latest_people_time
        if age > self.people_stale_s:
            return (
                f'No recent face recognition data (last update {age:.0f}s ago). '
                'The face recognition system may have stopped.'
            )

        people = self.latest_people.get('people', [])
        hidden = self.latest_people.get('faces_not_visible', 0)

        lines = []
        for p in people:
            if p.get('known'):
                lines.append(f"{p['name']} (entity_id {p['entity_id']}, recognized)")
            else:
                lines.append(
                    f"An unidentified person (entity_id {p['entity_id']}, not yet named)"
                )
        if hidden:
            lines.append(f'{hidden} person(s) detected whose face is not visible')

        if not lines:
            return 'Nobody is in view right now.'
        return 'In view right now: ' + '; '.join(lines) + '.'

    def tool_name_person(self, entity_id, name):
        """name_person: name a face, merging if the name already exists."""
        name = name.strip()
        if not name:
            return 'No name given, nothing changed.'

        current = self.memory.get_entity_name(entity_id)
        if current is None:
            return f'No person with entity_id {entity_id}. Call who_is_here for current ids.'

        existing = self.memory.find_person_by_name(name)
        if existing is not None and existing != entity_id:
            known_name = self.memory.get_entity_name(existing)  # Keep stored spelling
            self.memory.merge_person(entity_id, existing)
            self.get_logger().info(f'Merged entity {entity_id} into {existing} ({known_name}).')
            return (
                f'Merged: that face now belongs to {known_name}, who was already '
                'known. Recognition of them should be more reliable now.'
            )

        self.memory.rename_entity(entity_id, name)
        self.get_logger().info(f'Named entity {entity_id}: {current} -> {name}')
        return f'Done. You will recognize {name} from now on.'

    def close_interview(self, why):
        """End the session and drop its prompt context."""
        self.get_logger().info(f'Interview closed: {why}')
        self.interview = None
        self.interview_scenario = None
        self.past_debriefs = []

    def interview_block(self):
        """Return the interview section of the system prompt, or None outside a session."""
        if self.interview is None:
            return None
        return build_interview_block(
            self.interview, self.interview_scenario, self.resume_text, self.past_debriefs
        )

    def research_company(self, company, role, force_refresh=False):
        """Get a company brief from cache or research, speaking while it runs."""
        key = normalize_company(company)
        result = get_brief(
            self.interview_store, self.researcher, company, role,
            force_refresh=force_refresh,
            on_research_start=lambda: self.speak(RESEARCH_START_LINE),
            slow_notice_s=RESEARCH_SLOW_S,
            on_slow=lambda: self.speak(RESEARCH_SLOW_LINE),
        )
        if result.origin == 'cache':
            self.get_logger().info(
                f"Research: cache hit for '{key}' (age {result.age_s / 86400:.1f}d)"
            )
        elif result.origin == 'web':
            self.get_logger().info(
                f"Research: cache {result.reason} for '{key}', searched web "
                f"(model {self.researcher.model}, tool {self.researcher.search_tool['type']}); "
                f'stored brief, {len(result.sources)} sources'
            )
        else:
            self.get_logger().warn(
                f"Research: cache {result.reason} for '{key}', web search failed; "
                'using general knowledge, not cached'
            )
        return result

    def research_status_line(self, result):
        """Tell Claude where the brief came from and what to say about it."""
        if result.origin == 'fallback':
            if not result.brief:
                return ('Research failed and no brief is available. Tell the user that in '
                        'one sentence, then continue from what you know.')
            return f'{result.note} Tell the user that in one sentence.'
        if result.origin == 'cache':
            return f'Brief came from the cache ({result.age_s / 86400:.0f} days old).'
        return f'Fresh web research, {len(result.sources)} sources.'

    def tool_start_interview(self, company, role, persona):
        """start_interview: research the company and open a session."""
        company, role = company.strip(), role.strip()
        persona = persona or DEFAULT_PERSONA
        if not company or not role:
            return 'Company and role are both required. Ask the user for the missing one.'
        if self.interview is not None:
            return (f'An interview for {self.interview.company} is already running. '
                    'Ask the user whether to end it first (end_interview).')
        scenario = load_scenario(persona, self.scenarios_dir)  # ValueError if unknown
        result = self.research_company(company, role)
        self.interview = InterviewSession(
            company, role, persona, brief=result.brief, brief_note=result.note,
        )
        self.interview_scenario = scenario
        self.past_debriefs = self.interview_store.recent_debriefs(3)
        self.get_logger().info(f'Interview started: {company}, {role}, {persona}')
        return (
            f'Interview session started ({persona}). {self.research_status_line(result)} '
            'Now begin in character with the rapport phase. Do not repeat the lookup line.'
        )

    def tool_interview_advance(self):
        """interview_advance: move one phase forward."""
        if self.interview is None:
            return 'No interview is running.'
        before = self.interview.phase
        if not self.interview.advance():
            return 'Already in the debrief. Give it, then call save_interview_debrief.'
        self.get_logger().info(f'Interview phase: {before} -> {self.interview.phase}')
        return f'Now in phase {self.interview.phase}: {PHASE_GUIDANCE[self.interview.phase]}'

    def tool_end_interview(self):
        """end_interview: jump straight to the debrief."""
        if self.interview is None:
            return 'No interview is running.'
        if self.interview.in_debrief:
            # Second end request: leave without saving rather than get stuck.
            self.close_interview('ended during debrief, not saved')
            return 'Interview session closed without saving a debrief. You are Tales again.'
        before = self.interview.phase
        self.interview.end()
        self.get_logger().info(f'Interview phase: {before} -> debrief (ended early)')
        return 'Interview ended. Step out of character now and give the debrief.'

    def tool_save_debrief(self, score, summary):
        """save_interview_debrief: store the debrief and close the session."""
        if self.interview is None:
            return 'No interview is running. Nothing saved.'
        if not self.interview.in_debrief:
            return 'Not in the debrief yet. Call end_interview or finish the phases first.'
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = 0
        if not 1 <= score <= 10:
            return 'Score must be a whole number from 1 to 10. Call again with a valid score.'
        s = self.interview
        self.interview_store.save_debrief(
            s.company, s.role, s.persona, score, summary.strip(), time.time()
        )
        self.get_logger().info(f'Debrief saved: score {score}')
        # The row is committed; a failed turn must not reopen this session.
        self.debrief_saved_this_turn = True
        self.close_interview('debrief saved')
        return 'Debrief saved. The interview session is over; you are Tales again.'

    def tool_refresh_research(self, company, role):
        """refresh_company_research: force new research and update a live session."""
        company = company.strip()
        if not company:
            return 'No company given.'
        result = self.research_company(company, role.strip(), force_refresh=True)
        s = self.interview
        if s is not None and normalize_company(s.company) == normalize_company(company):
            s.brief, s.brief_note = result.brief, result.note
        return f'Research refreshed for {company}. {self.research_status_line(result)}'

    def execute_tool(self, block):
        """Run one tool_use block and return its tool_result content."""
        self.get_logger().info(f'Tool: {block.name} {block.input or ""}')
        try:
            if block.name == 'capture_and_view_snapshot':
                return self.tool_snapshot()
            if block.name == 'who_is_here':
                result = self.tool_who_is_here()
                self.get_logger().info(f'who_is_here -> {result}')
                return result
            if block.name == 'remember_fact':
                key = str(block.input['key']).strip()
                value = str(block.input['value']).strip()
                if not key or not value:
                    return 'Key and value are both required. Nothing saved.'
                self.memory.store_fact(key, value)
                self.get_logger().info(f'Stored fact: {key} = {value}')
                return f'Saved: {key} = {value}'
            if block.name == 'forget_fact':
                key = str(block.input['key']).strip()
                if self.memory.delete_fact(key):
                    self.get_logger().info(f'Forgot fact: {key}')
                    return f'Deleted: {key}'
                return f'No fact with key "{key}". Nothing deleted.'
            if block.name == 'name_person':
                return self.tool_name_person(
                    int(block.input['entity_id']), str(block.input['name'])
                )
            if block.name == 'start_interview':
                return self.tool_start_interview(
                    str(block.input['company']), str(block.input['role']),
                    block.input.get('persona'),
                )
            if block.name == 'interview_advance':
                return self.tool_interview_advance()
            if block.name == 'end_interview':
                return self.tool_end_interview()
            if block.name == 'save_interview_debrief':
                return self.tool_save_debrief(
                    block.input['score'], str(block.input['summary'])
                )
            if block.name == 'refresh_company_research':
                return self.tool_refresh_research(
                    str(block.input['company']), str(block.input.get('role') or '')
                )
            return f'Unknown tool: {block.name}'
        except Exception as e:
            # Report the failure to Claude instead of crashing the turn.
            self.get_logger().error(f'Tool {block.name} failed: {e}')
            return f'Tool failed: {e}'

    # ------------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------------

    def destroy_node(self):
        """Close the database connections before shutting down."""
        self.interview_store.close()
        self.memory.close()
        super().destroy_node()

    def extract_and_store_facts(self, text):
        """Store every [FACT: key=value] tag in text, return text without them."""
        for key, value in FACT_PATTERN.findall(text):
            key, value = key.strip(), value.strip()
            self.memory.store_fact(key, value)
            self.get_logger().info(f'Stored fact: {key} = {value}')
        return FACT_PATTERN.sub('', text).strip()

    def speakable(self, text):
        """Strip fact tags and markdown so TTS reads clean sentences."""
        text = FACT_PATTERN.sub('', text)
        text = MARKDOWN.sub('', text)
        return ' '.join(text.split())  # Collapse extra whitespace

    def speak(self, sentence):
        """Publish one sentence to tts_node, if anything speakable is left."""
        clean = self.speakable(sentence)
        if clean:
            self.speech_pub.publish(String(data=clean))

    def trim_history(self):
        """
        Keep history under MAX_HISTORY without breaking its structure.

        The API requires history to start with a plain user message. A blind
        slice can cut between a tool_use and its tool_result, leaving an
        orphan at the front, and every later call fails. So after slicing,
        drop messages until the first one is a user message with plain
        text content.
        """
        limit = MAX_HISTORY_INTERVIEW if self.interview is not None else MAX_HISTORY
        if len(self.conversation_history) <= limit:
            return
        trimmed = self.conversation_history[-limit:]
        while trimmed and not (
            trimmed[0]['role'] == 'user' and isinstance(trimmed[0]['content'], str)
        ):
            trimmed.pop(0)
        self.conversation_history = trimmed

    # ------------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------------

    def stream_reply(self, system_prompt, max_tokens, turn_start, timing):
        """
        Stream one Claude response. Publish each sentence as it completes.
        Returns the final message object (for stop_reason and tool blocks).
        """
        buffer = ''
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=TOOLS,
            messages=self.conversation_history,
        ) as stream:
            for chunk in stream.text_stream:
                buffer += chunk

                # Don't split while a [FACT: ...] tag is still open, or half
                # a tag could leak into speech.
                if buffer.count('[') > buffer.count(']'):
                    continue

                # Publish every complete sentence in the buffer and keep the
                # unfinished remainder.
                while True:
                    match = SENTENCE_END.search(buffer)
                    if not match:
                        break
                    sentence = buffer[:match.end()]
                    buffer = buffer[match.end():]
                    if 'first_sentence' not in timing and self.speakable(sentence):
                        timing['first_sentence'] = time.monotonic() - turn_start
                    self.speak(sentence)

            final = stream.get_final_message()

        # Whatever is left when the stream ends is the last sentence.
        if buffer.strip():
            if 'first_sentence' not in timing and self.speakable(buffer):
                timing['first_sentence'] = time.monotonic() - turn_start
            self.speak(buffer)

        return final

    # ------------------------------------------------------------------------
    # Main callback
    # ------------------------------------------------------------------------

    def handle_input(self, msg):
        """Handle one user message: context, streamed reply, tools, memory."""
        user_text = msg.data
        self.get_logger().info(f'Received: {user_text}')

        turn_start = time.monotonic()
        timing = {}
        # Remember where this turn starts, so a failed turn can be rolled
        # back without leaving a dangling user message in history.
        history_len_before = len(self.conversation_history)
        self.debrief_saved_this_turn = False
        # Interview state is rolled back with history if the turn fails.
        interview_before = (
            self.interview, copy.copy(self.interview),
            self.interview_scenario, self.past_debriefs,
        )

        try:
            # ---- Memory context ------------------------------------------
            t = time.monotonic()
            context_block = self.memory.build_context_block(user_text)
            timing['memory_lookup'] = time.monotonic() - t

            # ---- Interview bookkeeping -----------------------------------
            # "end interview" is checked here as well as offered as a tool,
            # so the plain phrase always works even if Claude misses it.
            if self.interview is not None:
                if is_end_interview_command(user_text):
                    if self.interview.in_debrief:
                        self.close_interview('user said end interview during debrief')
                    else:
                        self.interview.end()
                        self.get_logger().info(
                            'Interview phase: -> debrief (user said end interview)'
                        )
                elif (self.interview.in_debrief
                      and self.interview.exchanges_in_phase >= DEBRIEF_MAX_TURNS):
                    self.close_interview('debrief turn limit reached, not saved')
            if self.interview is not None:
                self.interview.record_user_turn()
            # Sampled for is_interview_turn below.
            session_active = self.interview is not None

            self.conversation_history.append({'role': 'user', 'content': user_text})

            # ---- Stream, handling tool rounds ----------------------------
            t_api = time.monotonic()
            reply_parts = []
            debrief_turn = False
            for _ in range(MAX_TOOL_ROUNDS + 1):
                # Once a turn reaches the debrief, keep the larger cap for the
                # rest of it, even if save_interview_debrief closes the session.
                debrief_turn = debrief_turn or (
                    self.interview is not None and self.interview.in_debrief
                )
                max_tokens = self.debrief_max_tokens if debrief_turn else self.max_tokens
                # Rebuilt every round: a tool may have started an interview
                # or changed its phase since the last round.
                system_prompt = compose_system_prompt(
                    self.system_prompt, context_block, self.interview_block()
                )
                response = self.stream_reply(system_prompt, max_tokens, turn_start, timing)

                # Keep any text Claude said this round (e.g. "Let me look.")
                reply_parts.extend(
                    block.text for block in response.content if block.type == 'text'
                )

                if response.stop_reason != 'tool_use':
                    break

                # Claude can request several tools in one response (e.g.
                # who_is_here and a snapshot together). Every tool_use block
                # needs its own tool_result, all in ONE user message.
                results = [
                    {
                        'type': 'tool_result',
                        'tool_use_id': block.id,
                        'content': self.execute_tool(block),
                    }
                    for block in response.content
                    if block.type == 'tool_use'
                ]

                # Required shape: Claude's tool request, then our results.
                self.conversation_history.append(
                    {'role': 'assistant', 'content': response.content}
                )
                self.conversation_history.append({'role': 'user', 'content': results})
            timing['api_total'] = time.monotonic() - t_api

            # ---- Facts, history, publish ---------------------------------
            # Roleplay stays out of long-term memory (see is_interview_turn).
            # Debriefs are still saved, through save_interview_debrief.
            interview_turn = is_interview_turn(session_active, self.interview is not None)
            raw_reply = ' '.join(reply_parts)
            if interview_turn:
                reply_text = FACT_PATTERN.sub('', raw_reply).strip()
            else:
                reply_text = self.extract_and_store_facts(raw_reply)
            self.conversation_history.append({'role': 'assistant', 'content': reply_text})
            self.trim_history()

            # Publish the full reply before storing, so storage time never
            # delays anything downstream.
            self.response_pub.publish(String(data=reply_text))
            self.get_logger().info(f'Claude: {reply_text}')

            t = time.monotonic()
            if not interview_turn:
                self.memory.store_turn(user_text, reply_text)
            timing['memory_store'] = time.monotonic() - t

            self.get_logger().info(
                'Timing: '
                f"memory lookup {timing['memory_lookup']:.2f}s | "
                f"first sentence {timing.get('first_sentence', 0):.2f}s | "
                f"API total {timing['api_total']:.2f}s | "
                f"memory store {timing['memory_store']:.2f}s | "
                f'turn total {time.monotonic() - turn_start:.2f}s'
            )

        except Exception as e:
            # Roll back this turn's messages so history stays valid.
            self.conversation_history = self.conversation_history[:history_len_before]
            session, snapshot, scenario, past = interview_before
            if self.debrief_saved_this_turn:
                session, scenario, past = None, None, []
            elif session is not None:
                # Restore the same object so phase and turn count match history.
                session.__dict__.update(snapshot.__dict__)
            self.interview, self.interview_scenario, self.past_debriefs = session, scenario, past
            self.get_logger().error(f'Claude API error: {e}')


def main(args=None):
    """Standard ROS 2 node entry point."""
    rclpy.init(args=args)
    node = ClaudeBridgeNode()
    # Two threads: one for sensor callbacks, one for handle_input.
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        # Ctrl+C already shuts rclpy down via its signal handler. Calling
        # shutdown a second time raised RCLError, so only call it if needed.
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
