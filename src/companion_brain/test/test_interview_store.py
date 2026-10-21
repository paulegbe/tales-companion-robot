"""Tests for InterviewStore: SQLite tables for company briefs and debriefs."""

from pathlib import Path
import sqlite3
import sys

import pytest

PKG_DIR = str(Path(__file__).resolve().parents[1])
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

from companion_brain.interview_store import InterviewStore  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    """Return a path for a fresh database file."""
    return str(tmp_path / 'facts.db')


@pytest.fixture
def store(db_path):
    """Open a store and close it after the test."""
    s = InterviewStore(db_path)
    yield s
    s.close()


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_tables_created(store, db_path):
    """Create both interview tables on open."""
    tables = _tables(db_path)
    assert 'company_briefs' in tables
    assert 'interview_debriefs' in tables


def test_reopen_is_idempotent_and_keeps_data(db_path):
    """Reopen an existing database without error and without losing rows."""
    s1 = InterviewStore(db_path)
    s1.save_brief('acme', 'Acme', 'dev', 'brief one', [], 100.0)
    s1.close()
    s2 = InterviewStore(db_path)
    try:
        assert s2.latest_brief('acme')['brief'] == 'brief one'
    finally:
        s2.close()


def test_coexists_with_existing_table(db_path):
    """Open a database that already has unrelated tables (the shared facts.db)."""
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE facts (id INTEGER PRIMARY KEY, fact TEXT)')
    conn.execute("INSERT INTO facts (fact) VALUES ('likes tea')")
    conn.commit()
    conn.close()
    s = InterviewStore(db_path)
    s.close()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT fact FROM facts').fetchone()[0] == 'likes tea'
    finally:
        conn.close()


def test_journal_mode_is_wal(store, db_path):
    """Put the database in WAL mode."""
    mode = store.conn.execute('PRAGMA journal_mode').fetchone()[0]
    assert mode.lower() == 'wal'
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    finally:
        conn.close()


def test_latest_brief_returns_newest(store):
    """Return the row with the greatest created_at, regardless of insert order."""
    store.save_brief('acme', 'Acme', 'dev', 'middle', [], 200.0)
    store.save_brief('acme', 'Acme', 'dev', 'newest', [], 300.0)
    store.save_brief('acme', 'Acme', 'dev', 'oldest', [], 100.0)
    got = store.latest_brief('acme')
    assert got['brief'] == 'newest'
    assert got['created_at'] == 300.0
    assert got['company_key'] == 'acme'
    assert got['company_display'] == 'Acme'
    assert got['role'] == 'dev'


def test_latest_brief_tie_returns_latest_id(store):
    """Break a created_at tie by returning the most recently inserted row."""
    store.save_brief('acme', 'Acme', 'dev', 'first', [], 100.0)
    store.save_brief('acme', 'Acme', 'dev', 'second', [], 100.0)
    assert store.latest_brief('acme')['brief'] == 'second'


def test_latest_brief_is_per_company(store):
    """Keep briefs for different companies separate."""
    store.save_brief('acme', 'Acme', 'dev', 'acme brief', [], 100.0)
    store.save_brief('globex', 'Globex', 'dev', 'globex brief', [], 200.0)
    assert store.latest_brief('acme')['brief'] == 'acme brief'


def test_sources_round_trip(store):
    """Store sources as JSON and read back the same list of dicts."""
    sources = [
        {'url': 'https://a.example', 'title': 'A'},
        {'url': 'https://b.example', 'title': 'B with "quotes" and unicode é'},
    ]
    store.save_brief('acme', 'Acme', None, 'b', sources, 1.0)
    got = store.latest_brief('acme')
    assert got['sources'] == sources
    assert got['role'] is None


def test_unknown_key_returns_none(store):
    """Return None for a company with no brief."""
    assert store.latest_brief('nobody') is None
    assert store.latest_brief('') is None


def test_debrief_order_and_limit(store):
    """Return debriefs newest first and honor the limit."""
    for i, ts in enumerate([10.0, 30.0, 20.0, 40.0]):
        store.save_debrief('Co%d' % i, 'dev', 'technical', i + 1, 'sum %d' % i, ts)
    recent = store.recent_debriefs()
    assert [d['created_at'] for d in recent] == [40.0, 30.0, 20.0]
    assert recent[0] == {
        'company': 'Co3', 'role': 'dev', 'persona': 'technical',
        'created_at': 40.0, 'score': 4, 'summary': 'sum 3',
    }
    assert len(store.recent_debriefs(n=2)) == 2
    assert len(store.recent_debriefs(n=10)) == 4


def test_recent_debriefs_empty(store):
    """Return an empty list when there are no debriefs."""
    assert store.recent_debriefs() == []


@pytest.mark.parametrize('score', [1, 10])
def test_score_boundaries_accepted(store, score):
    """Accept the boundary scores 1 and 10."""
    store.save_debrief('Acme', 'dev', 'behavioral', score, 'ok', 1.0)
    assert store.recent_debriefs()[0]['score'] == score


@pytest.mark.parametrize('score', [0, 11, -1, 5.5, True, False, '7', None])
def test_bad_scores_rejected(store, score):
    """Raise ValueError for scores that are not integers from 1 to 10."""
    with pytest.raises(ValueError):
        store.save_debrief('Acme', 'dev', 'behavioral', score, 'bad', 1.0)
    assert store.recent_debriefs() == []
