"""Tests for interview_context: scenario cards and system prompt assembly."""

from pathlib import Path

from companion_brain.interview_context import (
    build_interview_block,
    compose_system_prompt,
    DEBRIEF_INSTRUCTIONS,
    DEFAULT_PERSONA,
    load_scenario,
    PACING_RULES,
    PERSONAS,
)
from companion_brain.interview_phases import (
    InterviewSession,
    PHASE_GUIDANCE,
    PHASES,
)
import pytest

PKG_DIR = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = str(PKG_DIR / 'scenarios')
RESUME = 'Jane Doe. Built a ROS robot. Python, C++.'
BASE = 'You are Tales.'
MEMORY = 'Memory: user likes tea.'


def _session(**kw):
    args = dict(company='Acme', role='Junior Dev', persona='recruiter_screen')
    args.update(kw)
    return InterviewSession(**args)


def test_personas_and_default():
    """List the four personas from the issue with recruiter_screen as default."""
    assert set(PERSONAS) == {'recruiter_screen', 'hiring_manager', 'technical', 'behavioral'}
    assert DEFAULT_PERSONA == 'recruiter_screen'


@pytest.mark.parametrize('persona', PERSONAS)
def test_load_scenario_for_every_persona(persona):
    """Load a non-empty, stripped card for each persona from the repo."""
    card = load_scenario(persona, SCENARIOS_DIR)
    assert card
    assert card == card.strip()


def test_scenario_files_have_no_dashes():
    """Keep scenario cards free of em dashes and double dashes."""
    for persona in PERSONAS:
        card = load_scenario(persona, SCENARIOS_DIR)
        assert chr(0x2014) not in card
        assert '-' * 2 not in card


@pytest.mark.parametrize('persona', ['pirate', '', 'Technical', '../companion', None])
def test_unknown_persona_raises(persona):
    """Raise ValueError for anything that is not a known persona."""
    with pytest.raises(ValueError):
        load_scenario(persona, SCENARIOS_DIR)


def test_no_interview_block_excludes_resume():
    """Leave resume and interview text out of the prompt outside a session."""
    prompt = compose_system_prompt(BASE, MEMORY, None)
    assert prompt == BASE + '\n\n' + MEMORY
    assert RESUME not in prompt
    assert 'INTERVIEW' not in prompt


def test_block_contains_everything_needed():
    """Include resume, persona card, company, phase, guidance and pacing rules."""
    card = load_scenario('technical', SCENARIOS_DIR)
    session = _session(persona='technical')
    session.record_user_turn()
    block = build_interview_block(session, card, RESUME)
    prompt = compose_system_prompt(BASE, MEMORY, block)
    assert prompt.startswith(BASE)
    assert MEMORY in prompt
    assert RESUME in prompt
    assert card in prompt
    assert 'Acme' in prompt
    assert 'Junior Dev' in prompt
    assert 'rapport' in prompt
    assert '1 candidate turns' in prompt
    assert PHASE_GUIDANCE['rapport'] in prompt
    assert PACING_RULES in prompt
    assert DEBRIEF_INSTRUCTIONS not in prompt


@pytest.mark.parametrize('phase_index', range(len(PHASES) - 1))
def test_each_non_debrief_phase_has_pacing(phase_index):
    """Show the current phase and its guidance with pacing rules in every phase."""
    session = _session(phase_index=phase_index)
    block = build_interview_block(session, 'card', RESUME)
    assert 'Current phase: %s' % PHASES[phase_index] in block
    assert PHASE_GUIDANCE[PHASES[phase_index]] in block
    assert PACING_RULES in block
    assert DEBRIEF_INSTRUCTIONS not in block


def test_debrief_block_uses_debrief_instructions():
    """Swap pacing rules for debrief instructions in the debrief phase."""
    session = _session()
    session.end()
    block = build_interview_block(session, 'card', RESUME)
    assert DEBRIEF_INSTRUCTIONS in block
    assert PACING_RULES not in block
    assert 'Current phase: debrief' in block
    assert PHASE_GUIDANCE['debrief'] in block


def test_past_debriefs_appear():
    """List past debriefs so the interviewer can probe weak spots."""
    past = [
        {'company': 'Globex', 'persona': 'technical', 'score': 6,
         'summary': 'Weak on system design.', 'role': 'dev', 'created_at': 1.0},
        {'company': 'Initech', 'persona': 'behavioral', 'score': 4,
         'summary': 'Vague STAR answers.', 'role': 'dev', 'created_at': 2.0},
    ]
    block = build_interview_block(_session(), 'card', RESUME, past)
    assert 'Globex, technical: score 6. Weak on system design.' in block
    assert 'Initech, behavioral: score 4. Vague STAR answers.' in block
    assert 'Weak spots' in block


def test_no_past_debriefs_omits_section():
    """Omit the weak spots section when there are no past debriefs."""
    block = build_interview_block(_session(), 'card', RESUME, [])
    assert 'Weak spots' not in block


def test_brief_and_note_appear():
    """Include the company brief and its fallback note."""
    session = _session(brief='Acme builds robots.', brief_note='From general knowledge.')
    block = build_interview_block(session, 'card', RESUME)
    assert 'Acme builds robots.' in block
    assert '(From general knowledge.)' in block


def test_brief_without_note():
    """Include the brief without an empty note marker."""
    session = _session(brief='Acme builds robots.')
    block = build_interview_block(session, 'card', RESUME)
    assert 'Acme builds robots.' in block
    assert '()' not in block


def test_no_brief_omits_section():
    """Omit the research section when there is no brief."""
    block = build_interview_block(_session(), 'card', RESUME)
    assert 'Company research brief' not in block


@pytest.mark.parametrize('resume', [None, ''])
def test_no_resume_fallback_sentence(resume):
    """Tell the interviewer to ask about background when no resume is loaded."""
    block = build_interview_block(_session(), 'card', resume)
    assert 'No resume is loaded' in block
    assert "Candidate's resume" not in block


@pytest.mark.parametrize('memory,interview,expected', [
    (None, None, BASE),
    ('', None, BASE),
    (None, '', BASE),
    (None, 'BLOCK', BASE + '\n\nBLOCK'),
    (MEMORY, 'BLOCK', BASE + '\n\n' + MEMORY + '\n\nBLOCK'),
])
def test_compose_skips_empty_parts(memory, interview, expected):
    """Skip None and empty parts when joining the prompt."""
    assert compose_system_prompt(BASE, memory, interview) == expected


def test_compose_all_empty():
    """Return an empty string when every part is empty."""
    assert compose_system_prompt('', None, None) == ''
