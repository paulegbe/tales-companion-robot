"""SQLite storage for interview practice: company research briefs and debriefs.

Plain Python, no ROS imports. Uses the same facts.db as MemoryManager, but
with its own connection, because memory_manager imports chromadb at module
level and that would make this unusable in Mac tests.

Tables:
    company_briefs:     one row per research run (company_key, company_display,
                        role, brief, sources as JSON, created_at). The newest
                        row for a company_key is the cached brief.
    interview_debriefs: one row per finished interview (company, role,
                        persona, created_at, score 1 to 10, summary).

WAL mode matches MemoryManager, so vision_node and the bridge can keep using
facts.db at the same time. A lock guards the connection because the bridge
runs callbacks on more than one thread.
"""

import json
import sqlite3
import threading


class InterviewStore:
    """Reads and writes interview tables in an SQLite database file."""

    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        """Create the tables if missing. Safe to run on every startup."""
        with self.lock, self.conn:
            self.conn.execute(
                'CREATE TABLE IF NOT EXISTS company_briefs ('
                ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
                ' company_key TEXT NOT NULL,'
                ' company_display TEXT NOT NULL,'
                ' role TEXT,'
                ' brief TEXT NOT NULL,'
                ' sources TEXT NOT NULL,'
                ' created_at REAL NOT NULL)'
            )
            self.conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_company_briefs_key '
                'ON company_briefs (company_key, created_at)'
            )
            self.conn.execute(
                'CREATE TABLE IF NOT EXISTS interview_debriefs ('
                ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
                ' company TEXT NOT NULL,'
                ' role TEXT,'
                ' persona TEXT NOT NULL,'
                ' created_at REAL NOT NULL,'
                ' score INTEGER NOT NULL,'
                ' summary TEXT NOT NULL)'
            )

    def latest_brief(self, company_key):
        """Return the newest brief for company_key as a dict, or None."""
        with self.lock:
            row = self.conn.execute(
                'SELECT company_key, company_display, role, brief, sources, created_at '
                'FROM company_briefs WHERE company_key = ? '
                'ORDER BY created_at DESC, id DESC LIMIT 1',
                (company_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            'company_key': row[0],
            'company_display': row[1],
            'role': row[2],
            'brief': row[3],
            'sources': json.loads(row[4]),
            'created_at': row[5],
        }

    def save_brief(self, company_key, company_display, role, brief, sources, created_at):
        """Store a new brief. Older rows are kept as history."""
        with self.lock, self.conn:
            self.conn.execute(
                'INSERT INTO company_briefs '
                '(company_key, company_display, role, brief, sources, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (company_key, company_display, role, brief, json.dumps(sources), created_at),
            )

    def save_debrief(self, company, role, persona, score, summary, created_at):
        """Store one debrief. Raise ValueError if score is not an integer 1 to 10."""
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 10:
            raise ValueError('score must be an integer from 1 to 10, got %r' % (score,))
        with self.lock, self.conn:
            self.conn.execute(
                'INSERT INTO interview_debriefs '
                '(company, role, persona, created_at, score, summary) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (company, role, persona, created_at, score, summary),
            )

    def recent_debriefs(self, n=3):
        """Return the n most recent debriefs, newest first, as dicts."""
        with self.lock:
            rows = self.conn.execute(
                'SELECT company, role, persona, created_at, score, summary '
                'FROM interview_debriefs ORDER BY created_at DESC, id DESC LIMIT ?',
                (n,),
            ).fetchall()
        keys = ('company', 'role', 'persona', 'created_at', 'score', 'summary')
        return [dict(zip(keys, row)) for row in rows]

    def close(self):
        """Close the database connection."""
        with self.lock:
            self.conn.close()
