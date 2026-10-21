"""Tests for the interview phase machine and the end-interview command matcher."""

from companion_brain.interview_phases import (
    DEBRIEF,
    InterviewSession,
    is_end_interview_command,
    is_interview_turn,
    PHASE_GUIDANCE,
    PHASES,
)
import pytest

EXPECTED_ORDER = (
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


def _session():
    return InterviewSession(company='Acme', role='Junior Dev', persona='recruiter_screen')


def test_phases_match_issue_order():
    """Check PHASES lists the phases in the order the issue specifies."""
    assert PHASES == EXPECTED_ORDER
    assert DEBRIEF == 'debrief'


def test_session_starts_at_rapport():
    """Start a new session at rapport with zero exchanges."""
    session = _session()
    assert session.phase == 'rapport'
    assert session.exchanges_in_phase == 0
    assert not session.in_debrief


def test_advance_visits_phases_in_exact_order():
    """Advance one phase per call and stop with False at the debrief."""
    session = _session()
    visited = [session.phase]
    while session.advance():
        visited.append(session.phase)
    assert tuple(visited) == EXPECTED_ORDER
    assert session.in_debrief
    assert session.advance() is False
    assert session.phase == 'debrief'


def test_advance_moves_exactly_one_step():
    """Never skip a phase: each advance moves the index by exactly one."""
    session = _session()
    for i in range(len(PHASES) - 1):
        assert session.phase_index == i
        assert session.advance() is True
        assert session.phase_index == i + 1
        assert session.phase == PHASES[i + 1]


@pytest.mark.parametrize('start', range(len(PHASES) - 1))
def test_end_from_every_phase_lands_on_debrief(start):
    """Jump to the debrief from any phase and never move back."""
    session = _session()
    for _ in range(start):
        session.advance()
    assert session.phase == PHASES[start]
    session.record_user_turn()
    assert session.end() is True
    assert session.phase == 'debrief'
    assert session.in_debrief
    assert session.exchanges_in_phase == 0
    assert session.end() is False
    assert session.advance() is False
    assert session.phase == 'debrief'


def test_end_at_debrief_returns_false():
    """Return False from end() when the session is already at the debrief."""
    session = _session()
    session.end()
    assert session.end() is False
    assert session.phase == 'debrief'


def test_exchange_counter_increments_and_resets():
    """Count user turns per phase and reset the count on advance."""
    session = _session()
    session.record_user_turn()
    session.record_user_turn()
    assert session.exchanges_in_phase == 2
    session.advance()
    assert session.exchanges_in_phase == 0
    session.record_user_turn()
    assert session.exchanges_in_phase == 1


@pytest.mark.parametrize('text', [
    'end interview',
    'End the interview.',
    "ok let's end interview now",
    'END INTERVIEW',
    'please end the interview',
    'Tales, end the interview please',
    'END INTERVIEW!',
    'could we end this interview',
    '  okay, lets end the interview now.  ',
    'hey tales, can we end interview?',
    "i'd like to end the interview",
    'end interview, tales',
])
def test_end_command_positives(text):
    """Match utterances that are only the end command plus filler."""
    assert is_end_interview_command(text) is True


@pytest.mark.parametrize('text', [
    'extend interview',
    'how did the interview end',
    'interview end',
    'end interviews',
    'the weekend interview',
    'before we end the interview, I have a question',
    "don't end the interview",
    'can you end the interview and then tell me',
    'I think we should end the interview',
    'end the interview later',
    'end the meeting',
    'end',
    '   ',
    '',
    None,
])
def test_end_command_negatives(text):
    """Reject utterances that mention ending the interview inside a longer sentence."""
    assert is_end_interview_command(text) is False


def test_every_phase_has_guidance():
    """Give every phase a non-empty guidance line and no extra keys."""
    assert set(PHASE_GUIDANCE) == set(PHASES)
    for phase in PHASES:
        assert PHASE_GUIDANCE[phase].strip()


# (active_at_start, active_after_bookkeeping, active_at_end, expected)
TURN_CASES = [
    pytest.param(False, False, False, False, id='no_session'),
    pytest.param(False, False, True, True, id='start_interview_turn'),
    pytest.param(True, True, True, True, id='normal_interview_turn'),
    pytest.param(True, True, False, True, id='debrief_saved_this_turn'),
    pytest.param(True, True, False, True, id='end_interview_tool_during_debrief'),
    pytest.param(True, False, False, False, id='auto_close_at_debrief_turn_limit'),
    pytest.param(True, False, False, False, id='end_interview_said_during_debrief'),
    pytest.param(True, False, True, True, id='closed_in_bookkeeping_then_reopened'),
    pytest.param(False, True, False, True, id='unreachable_open_after_bookkeeping_only'),
    pytest.param(False, True, True, True, id='unreachable_opened_before_bookkeeping'),
]


@pytest.mark.parametrize('at_start,after_bookkeeping,at_end,expected', TURN_CASES)
def test_is_interview_turn(at_start, after_bookkeeping, at_end, expected):
    """Keep a turn out of long-term memory only when the session was live after bookkeeping."""
    assert is_interview_turn(at_start, after_bookkeeping, at_end) is expected


def test_is_interview_turn_table_covers_all_combinations():
    """Cover all 8 combinations of the three flags in the scenario table."""
    combos = {tuple(p.values[:3]) for p in TURN_CASES}
    assert len(combos) == 8


def test_start_alone_never_counts():
    """Return False whenever only active_at_start is set, and ignore it otherwise."""
    assert is_interview_turn(True, False, False) is False
    for after in (False, True):
        for end in (False, True):
            assert is_interview_turn(True, after, end) == is_interview_turn(False, after, end)
