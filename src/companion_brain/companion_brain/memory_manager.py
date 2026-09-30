"""
memory_manager.py

Persistent memory layer for the companion, independent of ROS 2.
This is a plain Python class that claude_bridge_node and vision_node
import and use.

Two storage backends, used for different kinds of memory:

    SQLite (facts.db)
        Structured, exact-match data: key-value facts, entities (people,
        objects, locations), observations of those entities over time,
        and mutable attributes (e.g. "shirt_color" changing week to week).
        Good for precise lookups and history queries.

    ChromaDB (chroma_db/)
        Semantic vector search over raw conversation turns, plus face
        embeddings for recognizing people. Good for fuzzy recall ("what
        did we talk about regarding X") where an exact keyword match
        would miss the relevant turn.

Both persist to disk under ~/companion_ws/memory_data/, so memory
survives node restarts and reboots.

SCHEMA NOTE:
    entities/observations/attributes/relationships form an entity-based
    schema chosen so it scales to vision: a detected face becomes a
    'person' entity, each sighting becomes an observation, and things like
    clothing color become attributes with full history. That enables
    queries like "is this the same shirt as last time" without
    restructuring the database.

CHANGES IN THIS VERSION:
    - Fixed stale docstrings on store_fact and build_context_block. Both
      said facts were unused. They're live: the bridge stores every
      [FACT: key=value] tag Claude emits.
    - Fixed face matching threshold. ChromaDB's default "l2" distance is
      SQUARED Euclidean distance, but face_recognition's 0.6 threshold is
      plain Euclidean. Comparing the two directly made matching far too
      loose (effectively ~0.77), which can merge different people into one
      entity. The distance is now square-rooted before comparing.
    - Added create_unnamed_person(). Placeholder names are now built from
      the database row id (unnamed_person_<id>), so they're unique across
      restarts. This is the memory-side half of the vision identity-merge
      fix. vision_node must call it instead of its own counter.
    - Added get_entity_name(), so callers don't reach into self.conn.
    - SQLite now uses WAL mode and a longer busy timeout. vision_node and
      claude_bridge_node each open their own connection to the same file,
      and WAL lets one read while the other writes without "database is
      locked" errors.
    - Conversation turn IDs use uuid4 instead of the timestamp, so two
      turns stored in the same instant can't collide.
    - build_context_block labels where the memory came from, in plain
      accurate terms. Claude no longer has to guess how it remembers
      things, which removes the reason to confabulate about its own
      architecture.
    - Added close() for clean shutdown.
    - Added delete_fact(), for the bridge's forget_fact tool.
    - Added find_person_by_name() and merge_person(). When the user names
      an unidentified face with a name that already exists (same person,
      seen in different lighting or angle), the two entities are merged.
      The person keeps every face embedding, which makes future
      recognition MORE reliable instead of creating duplicates.
"""

import math
import os
import sqlite3
import time
import uuid

import chromadb


class MemoryManager:
    """
    Owns the SQLite connection (structured facts/entities) and the
    ChromaDB client (semantic conversation search + face embeddings) for
    one companion process. Instantiated once per node and held for the
    node's whole lifetime.
    """

    def __init__(self, data_dir=None):
        if data_dir is None:
            data_dir = os.path.expanduser('~/companion_ws/memory_data')
        os.makedirs(data_dir, exist_ok=True)

        # ---- SQLite: structured facts, entities, observations ----------
        self.db_path = os.path.join(data_dir, 'facts.db')
        # check_same_thread=False: ROS 2 callbacks may run off the main
        # thread depending on executor type, so cross-thread use is allowed.
        # timeout=10: if another process holds a write lock, wait up to
        # 10 s instead of failing immediately.
        self.conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=10.0
        )
        # WAL (write-ahead logging) lets readers and a writer work at the
        # same time. Needed because vision_node and claude_bridge_node
        # both open this file. The setting persists in the database file.
        self.conn.execute('PRAGMA journal_mode=WAL')
        self._init_tables()

        # ---- ChromaDB: semantic memory --------------------------------
        self.chroma_client = chromadb.PersistentClient(
            path=os.path.join(data_dir, 'chroma_db')
        )
        # Conversation turns, for "what did we talk about" recall.
        self.collection = self.chroma_client.get_or_create_collection(
            name='conversations'
        )
        # Face embeddings (128-d vectors from face_recognition), kept
        # separate from conversation turns. A newly seen face is compared
        # against everyone previously seen.
        self.face_collection = self.chroma_client.get_or_create_collection(
            name='face_embeddings'
        )

    def _init_tables(self):
        """
        Creates the schema if it doesn't already exist. Safe to call on
        every startup: CREATE TABLE IF NOT EXISTS is a no-op once the
        tables exist.

        facts:         exact key-value facts from conversation ([FACT] tags)
        entities:      one row per person/object/location the companion knows
        observations:  one row per sighting/interaction, linked to an entity
        attributes:    mutable properties of an entity, with full history
                       (never overwritten; new rows track change over time)
        relationships: entity-to-entity links (e.g. "table located_in kitchen")
        """
        cur = self.conn.cursor()

        cur.execute('''
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                updated_at REAL
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT,        -- 'person', 'object', 'location', etc.
                name TEXT,
                first_seen REAL,
                last_seen REAL,
                embedding_id TEXT        -- links to a face/visual embedding, if any
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER,
                observed_at REAL,
                description TEXT,
                location_entity_id INTEGER,
                FOREIGN KEY(entity_id) REFERENCES entities(id),
                FOREIGN KEY(location_entity_id) REFERENCES entities(id)
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS attributes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER,
                attribute_key TEXT,      -- e.g. 'shirt_color', 'location'
                attribute_value TEXT,
                recorded_at REAL,
                FOREIGN KEY(entity_id) REFERENCES entities(id)
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a_id INTEGER,
                relation TEXT,           -- e.g. 'located_in', 'owned_by'
                entity_b_id INTEGER,
                recorded_at REAL,
                FOREIGN KEY(entity_a_id) REFERENCES entities(id),
                FOREIGN KEY(entity_b_id) REFERENCES entities(id)
            )
        ''')

        cur.execute('CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id)')
        cur.execute(
            'CREATE INDEX IF NOT EXISTS idx_attr_entity '
            'ON attributes(entity_id, attribute_key)'
        )
        cur.execute(
            'CREATE INDEX IF NOT EXISTS idx_entity_type_name '
            'ON entities(entity_type, name)'
        )

        self.conn.commit()

    # ------------------------------------------------------------------------
    # Entity management
    # ------------------------------------------------------------------------

    def get_or_create_entity(self, entity_type, name):
        """
        Looks up an entity by type + name. If found, refreshes last_seen
        and returns its id. If not found, creates a new entity row.

        Use this for entities with a real, stable name. For a face that
        hasn't been identified yet, use create_unnamed_person() instead.
        Passing a generated placeholder name here can return a DIFFERENT
        person who happens to have the same placeholder.
        """
        cur = self.conn.cursor()
        cur.execute(
            'SELECT id FROM entities WHERE entity_type=? AND name=?',
            (entity_type, name),
        )
        row = cur.fetchone()
        now = time.time()
        if row:
            cur.execute('UPDATE entities SET last_seen=? WHERE id=?', (now, row[0]))
            self.conn.commit()
            return row[0]

        cur.execute(
            'INSERT INTO entities (entity_type, name, first_seen, last_seen) '
            'VALUES (?, ?, ?, ?)',
            (entity_type, name, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def create_unnamed_person(self):
        """
        Creates a NEW person entity for an unidentified face and returns
        (entity_id, placeholder_name).

        The placeholder is built from the row id (unnamed_person_<id>), so
        it's unique forever, including across restarts. The old approach,
        an in-memory counter in vision_node, reset to 1 on every launch and
        collided with existing entities, merging different people.
        """
        now = time.time()
        cur = self.conn.cursor()
        cur.execute(
            'INSERT INTO entities (entity_type, name, first_seen, last_seen) '
            'VALUES (?, ?, ?, ?)',
            ('person', None, now, now),
        )
        entity_id = cur.lastrowid
        placeholder_name = f'unnamed_person_{entity_id}'
        cur.execute('UPDATE entities SET name=? WHERE id=?', (placeholder_name, entity_id))
        self.conn.commit()
        return entity_id, placeholder_name

    def get_entity_name(self, entity_id):
        """Returns an entity's name, or None if the id doesn't exist."""
        cur = self.conn.cursor()
        cur.execute('SELECT name FROM entities WHERE id=?', (entity_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def touch_entity(self, entity_id):
        """Refreshes an entity's last_seen time, e.g. on a face match."""
        cur = self.conn.cursor()
        cur.execute('UPDATE entities SET last_seen=? WHERE id=?', (time.time(), entity_id))
        self.conn.commit()

    def find_person_by_name(self, name):
        """Returns the id of a person with this name (case-insensitive), or None."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM entities WHERE entity_type='person' "
            'AND LOWER(name)=LOWER(?) ORDER BY id LIMIT 1',
            (name,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def merge_person(self, from_id, into_id):
        """
        Folds one person entity into another: face embeddings,
        observations, attributes, and relationships all move to into_id,
        then from_id is deleted. Used when an unidentified face turns out
        to be someone already known.
        """
        # Re-point face embeddings in ChromaDB.
        faces = self.face_collection.get(where={'entity_id': from_id})
        if faces['ids']:
            self.face_collection.update(
                ids=faces['ids'],
                metadatas=[{'entity_id': into_id}] * len(faces['ids']),
            )

        # Re-point everything in SQLite, in one transaction.
        cur = self.conn.cursor()
        cur.execute('UPDATE observations SET entity_id=? WHERE entity_id=?', (into_id, from_id))
        cur.execute('UPDATE attributes SET entity_id=? WHERE entity_id=?', (into_id, from_id))
        cur.execute('UPDATE relationships SET entity_a_id=? WHERE entity_a_id=?', (into_id, from_id))
        cur.execute('UPDATE relationships SET entity_b_id=? WHERE entity_b_id=?', (into_id, from_id))
        cur.execute(
            'UPDATE entities SET first_seen=MIN(first_seen, '
            '(SELECT first_seen FROM entities WHERE id=?)), last_seen=? WHERE id=?',
            (from_id, time.time(), into_id),
        )
        cur.execute('DELETE FROM entities WHERE id=?', (from_id,))
        self.conn.commit()

    def rename_entity(self, entity_id, new_name):
        """
        Updates an entity's name. Used when a previously unnamed entity
        (e.g. 'unnamed_person_7') gets identified by the user ("that's Paul").
        """
        cur = self.conn.cursor()
        cur.execute('UPDATE entities SET name=? WHERE id=?', (new_name, entity_id))
        self.conn.commit()

    # ------------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------------

    def log_observation(self, entity_id, description, location_entity_id=None):
        """
        Records a single sighting/interaction with an entity.
        location_entity_id lets an observation reference a 'location'
        entity, so "where was this seen" is queryable later.
        """
        cur = self.conn.cursor()
        cur.execute(
            'INSERT INTO observations '
            '(entity_id, observed_at, description, location_entity_id) '
            'VALUES (?, ?, ?, ?)',
            (entity_id, time.time(), description, location_entity_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------------
    # Attributes (mutable, tracked over time)
    # ------------------------------------------------------------------------

    def set_attribute(self, entity_id, key, value):
        """
        Records a NEW attribute value. Does not overwrite prior values.
        Keeping full history is what makes "is this a different shirt than
        last time" answerable later.
        """
        cur = self.conn.cursor()
        cur.execute(
            'INSERT INTO attributes '
            '(entity_id, attribute_key, attribute_value, recorded_at) '
            'VALUES (?, ?, ?, ?)',
            (entity_id, key, value, time.time()),
        )
        self.conn.commit()

    def get_attribute_history(self, entity_id, key):
        """Returns all recorded values for one attribute, newest first."""
        cur = self.conn.cursor()
        cur.execute(
            'SELECT attribute_value, recorded_at FROM attributes '
            'WHERE entity_id=? AND attribute_key=? ORDER BY recorded_at DESC',
            (entity_id, key),
        )
        return cur.fetchall()

    def get_latest_attribute(self, entity_id, key):
        """Convenience wrapper: just the most recent value, or None."""
        history = self.get_attribute_history(entity_id, key)
        return history[0][0] if history else None

    # ------------------------------------------------------------------------
    # Key-value facts
    # ------------------------------------------------------------------------

    def store_fact(self, key, value):
        """
        Stores or updates one exact key-value fact. Called by
        claude_bridge_node.extract_and_store_facts() whenever Claude's
        reply contains a [FACT: key=value] tag. This is the precise,
        exact-lookup counterpart to store_turn()'s fuzzy semantic recall.
        Storing an existing key overwrites its value (upsert).
        """
        cur = self.conn.cursor()
        cur.execute('''
            INSERT INTO facts (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
        ''', (key, value, time.time()))
        self.conn.commit()

    def delete_fact(self, key):
        """Deletes one fact. Returns True if it existed."""
        cur = self.conn.cursor()
        cur.execute('DELETE FROM facts WHERE key=?', (key,))
        self.conn.commit()
        return cur.rowcount > 0

    def get_all_facts(self):
        """Returns every stored key-value fact as a dict."""
        cur = self.conn.cursor()
        cur.execute('SELECT key, value FROM facts ORDER BY key')
        return dict(cur.fetchall())

    # ------------------------------------------------------------------------
    # ChromaDB: semantic conversation memory
    # ------------------------------------------------------------------------

    def store_turn(self, user_text, assistant_text):
        """
        Embeds and stores one conversation exchange (user message +
        companion reply) for later semantic recall. Called after every
        successful Claude response in claude_bridge_node.
        """
        combined = f'User: {user_text}\nCompanion: {assistant_text}'
        self.collection.add(
            documents=[combined],
            ids=[str(uuid.uuid4())],
            metadatas=[{'timestamp': time.time()}],
        )

    def retrieve_similar_turns(self, query_text, n_results=3):
        """
        Semantic search over stored conversation turns. Returns the
        n_results most similar past exchanges to query_text.
        Returns an empty list on any failure (e.g. empty collection)
        rather than raising, so handle_input() stays resilient.
        """
        try:
            count = self.collection.count()
            if count == 0:
                return []
            results = self.collection.query(
                query_texts=[query_text],
                n_results=min(n_results, count),
            )
            return results['documents'][0] if results['documents'] else []
        except Exception:
            return []

    def build_context_block(self, query_text):
        """
        Assembles everything relevant to the current input into one text
        block: known facts (from store_fact) + semantically similar past
        conversation turns (from store_turn). The block gets appended to
        the system prompt for a single Claude call. It does not persist or
        mutate stored data.

        The header states plainly where this came from, so Claude can
        answer "how do you remember that?" truthfully instead of inventing
        an explanation of its own architecture.
        """
        facts = self.get_all_facts()
        similar = self.retrieve_similar_turns(query_text)

        if not facts and not similar:
            return ''

        block = (
            'MEMORY (retrieved from your long-term storage, which persists '
            'across restarts):\n'
        )

        if facts:
            block += '\nKnown facts:\n'
            for k, v in facts.items():
                block += f'- {k}: {v}\n'

        if similar:
            block += '\nRelevant past conversation:\n'
            for turn in similar:
                block += f'{turn}\n\n'

        return block

    # ------------------------------------------------------------------------
    # ChromaDB: face embeddings
    # ------------------------------------------------------------------------

    def store_face_embedding(self, entity_id, embedding):
        """
        Stores a 128-d face encoding (from face_recognition) linked to an
        entity_id. ChromaDB requires embeddings as a plain list of floats,
        so the numpy array from face_recognition is converted.
        """
        self.face_collection.add(
            embeddings=[embedding.tolist()],
            ids=[f'face_{entity_id}_{uuid.uuid4().hex}'],
            metadatas=[{'entity_id': entity_id}],
        )

    def find_matching_face(self, embedding, threshold=0.6):
        """
        Searches stored face embeddings for the closest match. Returns the
        matching entity_id if the distance is under threshold (lower =
        more similar), otherwise None.

        threshold=0.6 is face_recognition's standard "same person" value,
        measured as plain Euclidean distance. ChromaDB's default "l2"
        space returns SQUARED Euclidean distance, so the result is
        square-rooted before comparing. Without that, the effective
        threshold was about 0.77, loose enough to match different people.
        """
        try:
            count = self.face_collection.count()
            if count == 0:
                return None

            results = self.face_collection.query(
                query_embeddings=[embedding.tolist()],
                n_results=1,
            )

            if not results['distances'] or not results['distances'][0]:
                return None

            closest_distance = math.sqrt(results['distances'][0][0])
            if closest_distance <= threshold:
                return results['metadatas'][0][0]['entity_id']

            return None
        except Exception:
            return None

    # ------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------

    def close(self):
        """Closes the SQLite connection. Call from a node's destroy_node()."""
        try:
            self.conn.close()
        except Exception:
            pass
