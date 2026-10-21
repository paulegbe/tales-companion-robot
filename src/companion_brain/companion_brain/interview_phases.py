"""Phase tracking for interview roleplay.

Plain Python, no ROS imports, so it is testable on the Mac.

An interview moves through PHASES strictly in order. Claude moves it forward
one step at a time with the interview_advance tool; there is deliberately no
way to jump ahead, so the interview cannot skip straight to the technical
questions. The one exception is end(), which jumps to the debrief from any
phase when the user says "end interview".
"""

from dataclasses import dataclass
import re

PHASES = (
    'rapport',
    'setup',
    'tell_me_about_yourself',
    'motivation',
    'behavioral',
    'technical',
    'candidate_questions',
    'close',
    'debrief',
)

DEBRIEF = 'debrief'

# One line of guidance per phase, injected into the system prompt each turn.
PHASE_GUIDANCE = {
    'rapport': '2 to 3 exchanges of light small talk before getting into the interview.',
    'setup': 'Introduce yourself and your role, name the position, and give a short agenda.',
    'tell_me_about_yourself': 'Ask the candidate to tell you about themselves. Follow up once.',
    'motivation': 'Ask why this company and why this role. Probe how well they know the company.',
    'behavioral': (
        'Ask 2 behavioral questions, one at a time, each with a follow-up grounded in '
        'what they actually said. Use their resume where it fits.'
    ),
    'technical': 'Ask technical questions at the depth your persona card describes.',
    'candidate_questions': (
        'Ask whether the candidate has questions for you, and answer in character.'
    ),
    'close': 'Thank them, explain next steps as a real interviewer would, and say goodbye.',
    'debrief': 'Step out of character and give the debrief.',
}

# The whole utterance must be the command, with optional filler around it.
# Matching anywhere would catch "before we end the interview, one question",
# and the jump to the debrief cannot be undone. Other wordings are left to
# Claude, which has the end_interview tool.
_END_COMMAND = re.compile(
    r"^\s*(?:(?:ok|okay|hey|tales|please|let's|lets|let us|can we|could we|"
    r"i want to|i'd like to)[\s,]+)*"
    r'end (?:the |this )?interview(?:[\s,]+(?:now|please|tales))*[\s.!?]*$',
    re.IGNORECASE,
)


def is_end_interview_command(text):
    """Return True when the whole utterance is a request to end the interview."""
    return bool(_END_COMMAND.match(text or ''))


@dataclass
class InterviewSession:
    """State of one interview: who, for what, and where in the phases it is."""

    company: str
    role: str
    persona: str
    brief: str = ''
    brief_note: str = ''
    phase_index: int = 0
    exchanges_in_phase: int = 0

    @property
    def phase(self):
        """Return the current phase name."""
        return PHASES[self.phase_index]

    @property
    def in_debrief(self):
        """Return True once the interview has reached the debrief."""
        return self.phase == DEBRIEF

    def advance(self):
        """Move exactly one phase forward. Return False if already at the debrief."""
        if self.in_debrief:
            return False
        self.phase_index += 1
        self.exchanges_in_phase = 0
        return True

    def end(self):
        """Jump to the debrief from any phase. Return False if already there."""
        if self.in_debrief:
            return False
        self.phase_index = PHASES.index(DEBRIEF)
        self.exchanges_in_phase = 0
        return True

    def record_user_turn(self):
        """Count one candidate turn in the current phase."""
        self.exchanges_in_phase += 1
