"""Builds the interview part of the system prompt.

Plain Python, no ROS imports. The bridge calls compose_system_prompt before
every Claude call. Outside an interview it returns the normal prompt, with
no resume text in it. During an interview it appends a block with the
persona card, company brief, resume, past weak spots, current phase and the
pacing rules.

The block puts stable content first (persona, brief, resume) and per-turn
content last (phase, exchange count), so it stays friendly to prompt
caching if the bridge adopts it later.
"""

import os

from companion_brain.interview_phases import PHASE_GUIDANCE

PERSONAS = ('recruiter_screen', 'hiring_manager', 'technical', 'behavioral')
DEFAULT_PERSONA = 'recruiter_screen'

PACING_RULES = (
    'Interview rules:\n'
    'Ask one question per turn.\n'
    'Give a brief, natural reaction to each answer before moving on.\n'
    "Ask at least one follow-up grounded in the candidate's actual answer.\n"
    'Speak 2 to 3 sentences per turn.\n'
    'Stay in character until the debrief.\n'
    'Ask resume-based questions the way a real interviewer would.\n'
    'When a phase is done, call interview_advance before starting the next one. '
    'Never skip a phase.'
)

DEBRIEF_INSTRUCTIONS = (
    'Debrief: step out of character and speak as Tales, the coach. Cover, in plain '
    'spoken sentences: the strengths you saw; the weakest answers, each with a '
    'better version; filler words you noticed (transcripts may drop some, so say '
    'that if relevant); a score from 1 to 10; and one thing to practice next. Up to '
    'about 8 spoken sentences is fine here. Then call save_interview_debrief with '
    'the score and a short summary of the weak spots.'
)


def load_scenario(persona, scenarios_dir):
    """Return the scenario card text for persona. Raise ValueError if unknown."""
    if persona not in PERSONAS:
        raise ValueError('Unknown persona %r. Choose one of: %s' % (persona, ', '.join(PERSONAS)))
    with open(os.path.join(scenarios_dir, persona + '.md'), 'r', encoding='utf-8') as f:
        return f.read().strip()


def build_interview_block(session, scenario_text, resume_text, past_debriefs=()):
    """Return the interview section of the system prompt for this turn."""
    parts = [
        'INTERVIEW PRACTICE SESSION. You are role-playing an interviewer. '
        'This replaces your usual reply length rule for the rest of the session.',
        'Persona card:\n' + scenario_text,
        'Company: %s\nRole: %s' % (session.company, session.role),
    ]
    if session.brief:
        brief = 'Company research brief:\n' + session.brief
        if session.brief_note:
            brief += '\n(' + session.brief_note + ')'
        parts.append(brief)
    if resume_text:
        parts.append("Candidate's resume:\n" + resume_text)
    else:
        parts.append('No resume is loaded. Ask about their background instead.')
    if past_debriefs:
        lines = [
            '- %s, %s: score %s. %s' % (d['company'], d['persona'], d['score'], d['summary'])
            for d in past_debriefs
        ]
        parts.append(
            'Weak spots from past practice sessions (probe these where natural):\n'
            + '\n'.join(lines)
        )
    parts.append(
        'Current phase: %s (%d candidate turns so far in this phase).\nPhase goal: %s'
        % (session.phase, session.exchanges_in_phase, PHASE_GUIDANCE[session.phase])
    )
    parts.append(DEBRIEF_INSTRUCTIONS if session.in_debrief else PACING_RULES)
    return '\n\n'.join(parts)


def compose_system_prompt(base, memory_block=None, interview_block=None):
    """Join the base prompt, memory context and (only in a session) the interview block."""
    return '\n\n'.join(p for p in (base, memory_block, interview_block) if p)
