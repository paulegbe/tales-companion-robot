"""
memory_manager.py

Persistent memory layer for the companion, independent of ROS 2 --
this is a plain Python class that claude_bridge_node imports and uses.

Two storage backends, used for different kinds of memory:

    SQLite (facts.db)
        Structured, exact-match data: entities (people, objects,
        locations), observations of those entities over time, and
        mutable attributes (e.g. "shirt_color" changing week to week).
        Good for precise lookups and history queries.

    ChromaDB (chroma_db/)
        Semantic vector search over raw conversation turns. Good for
        fuzzy recall -- "what did we talk about regarding X" -- where
        an exact keyword match would miss the relevant turn.

Both persist to disk under ~/companion_ws/memory_data/, so memory
survives node restarts and reboots.

SCHEMA NOTE:
    entities/observations/attributes/relationships form an
    entity-based schema chosen specifically so it scales to vision
    later: a detected face becomes a 'person' entity, each sighting
    becomes an observation, and things like clothing color become
    attributes with full history -- enabling queries like "is this
    the same shirt as last time" without restructuring the database.
"""

import sqlite3
import os
import time
import chromadb


class MemoryManager:
    """
    Owns both the SQLite connection (structured facts/entities) and
    the ChromaDB client (semantic conversation search) for one companion
    instance. Instantiated once, held by claude_bridge_node for its
    whole lifetime.
    """

    def __init__(self, data_dir=None):
        if data_dir is None:
            data_dir = os.path.expanduser('~/companion_ws/memory_data')
        os.makedirs(data_dir, exist_ok=True)

        # --- SQLite: structured facts, entities, observations ---
        self.db_path = os.path.join(data_dir, 'facts.db')
        # check_same_thread=False: ROS 2 callbacks may run off the main
        # thread depending on executor type, so we allow cross-thread use.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_facts_table()

        # --- ChromaDB: semantic search over conversation history ---
        self.chroma_client = chromadb.PersistentClient(
            path=os.path.join(data_dir, 'chroma_db')
        )
        self.collection = self.chroma_client.get_or_create_collection(
            name='conversations'
        )
                # Second ChromaDB collection, separate from conversation turns --
        # stores face embeddings (128-d vectors from face_recognition) so
        # a newly seen face can be compared against everyone previously seen.
        self.face_collection = self.chroma_client.get_or_create_collection(
            name='face_embeddings'
        )

    def _init_facts_table(self):
        """
        Creates the entity-based schema if it doesn't already exist.
        Safe to call every startup -- CREATE TABLE IF NOT EXISTS is a no-op
        once the tables exist.

        entities:      one row per person/object/location the companion knows about
        observations:  one row per sighting/interaction, linked to an entity
        attributes:    mutable properties of an entity, with full history
                       (never overwritten -- new rows track change over time)
        relationships: entity-to-entity links (e.g. "table located_in kitchen")
        """
        cur = self.conn.cursor()

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
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                updated_at REAL
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
        cur.execute('CREATE INDEX IF NOT EXISTS idx_attr_entity ON attributes(entity_id, attribute_key)')

        self.conn.commit()

    # ---------- Entity management ----------

    def get_or_create_entity(self, entity_type, name):
        """
        Looks up an entity by type + name. If found, refreshes last_seen
        and returns its id. If not found, creates a new entity row.

        This is the single entry point vision code will call every time
        it recognizes (or fails to recognize) something in frame.
        """
        cur = self.conn.cursor()
        cur.execute('SELECT id FROM entities WHERE entity_type=? AND name=?', (entity_type, name))
        row = cur.fetchone()
        if row:
            cur.execute('UPDATE entities SET last_seen=? WHERE id=?', (time.time(), row[0]))
            self.conn.commit()
            return row[0]

        cur.execute(
            'INSERT INTO entities (entity_type, name, first_seen, last_seen) VALUES (?, ?, ?, ?)',
            (entity_type, name, time.time(), time.time())
        )
        self.conn.commit()
        return cur.lastrowid

    def rename_entity(self, entity_id, new_name):
        """
        Updates an entity's name -- used when a previously-unnamed
        entity (e.g. 'unnamed_person_1') gets identified by the user
        ("that's Paul").
        """
        cur = self.conn.cursor()
        cur.execute('UPDATE entities SET name=? WHERE id=?', (new_name, entity_id))
        self.conn.commit()

    # ---------- Observations ----------

    def log_observation(self, entity_id, description, location_entity_id=None):
        """
        Records a single sighting/interaction with an entity.
        location_entity_id lets an observation reference a 'location'
        entity, so "where was this seen" is queryable later.
        """
        cur = self.conn.cursor()
        cur.execute(
            'INSERT INTO observations (entity_id, observed_at, description, location_entity_id) VALUES (?, ?, ?, ?)',
            (entity_id, time.time(), description, location_entity_id)
        )
        self.conn.commit()

    # ---------- Attributes (mutable, tracked over time) ----------

    def set_attribute(self, entity_id, key, value):
        """
        Records a NEW attribute value -- does not overwrite prior values.
        This is intentional: keeping full history is what makes
        "is this a different shirt than last time" answerable later.
        """
        cur = self.conn.cursor()
        cur.execute(
            'INSERT INTO attributes (entity_id, attribute_key, attribute_value, recorded_at) VALUES (?, ?, ?, ?)',
            (entity_id, key, value, time.time())
        )
        self.conn.commit()

    def get_attribute_history(self, entity_id, key):
        """Returns all recorded values for one attribute, newest first."""
        cur = self.conn.cursor()
        cur.execute(
            'SELECT attribute_value, recorded_at FROM attributes WHERE entity_id=? AND attribute_key=? ORDER BY recorded_at DESC',
            (entity_id, key)
        )
        return cur.fetchall()

    def get_latest_attribute(self, entity_id, key):
        """Convenience wrapper -- just the most recent value, or None."""
        history = self.get_attribute_history(entity_id, key)
        return history[0][0] if history else None

    # ---------- Simple key-value facts (legacy/general use) ----------

    def store_fact(self, key, value):
        """
        NOTE: currently unused by claude_bridge_node -- nothing calls
        this yet. Facts the companion learns in conversation right now
        go into ChromaDB via store_turn() instead, which is less precise
        for exact values (names, numbers) than a direct key lookup would be.
        Wiring this up (having Claude flag things worth storing as
        explicit facts) is a planned next step, not yet built.
        """
        cur = self.conn.cursor()
        cur.execute('''
            INSERT INTO facts (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        ''', (key, value, time.time()))
        self.conn.commit()

    def get_all_facts(self):
        """Returns every stored key-value fact as a dict."""
        cur = self.conn.cursor()
        cur.execute('SELECT key, value FROM facts')
        return dict(cur.fetchall())

    # ---------- ChromaDB: semantic conversation memory ----------

    def store_turn(self, user_text, assistant_text):
        """
        Embeds and stores one conversation exchange (user message +
        companion reply) for later semantic recall. Called after every
        successful Claude response in claude_bridge_node.
        """
        turn_id = str(time.time())
        combined = f'User: {user_text}\nCompanion: {assistant_text}'
        self.collection.add(
            documents=[combined],
            ids=[turn_id],
            metadatas=[{'timestamp': time.time()}]
        )

    def retrieve_similar_turns(self, query_text, n_results=3):
        """
        Semantic search over stored conversation turns. Returns the
        n_results most similar past exchanges to query_text.
        Returns an empty list on any failure (e.g. empty collection)
        rather than raising -- keeps handle_input() resilient.
        """
        try:
            count = self.collection.count()
            if count == 0:
                return []
            results = self.collection.query(
                query_texts=[query_text],
                n_results=min(n_results, count)
            )
            return results['documents'][0] if results['documents'] else []
        except Exception:
            return []

    def build_context_block(self, query_text):
        """
        Assembles everything relevant to the current input into one
        text block: known facts (currently always empty, see store_fact
        note above) + semantically similar past conversation turns.
        This block gets appended to the system prompt for a single
        Claude call -- it does not persist or mutate stored data.
        """
        facts = self.get_all_facts()
        similar = self.retrieve_similar_turns(query_text)

        block = ''
        if facts:
            block += 'Known facts:\n'
            for k, v in facts.items():
                block += f'- {k}: {v}\n'

        if similar:
            block += '\nRelevant past conversation:\n'
            for turn in similar:
                block += f'{turn}\n\n'

        return block
    def store_face_embedding(self, entity_id, embedding):
        """
        Stores a 128-d face encoding (from face_recognition) linked to
        an entity_id. ChromaDB requires embeddings as a plain list of
        floats, so the numpy array from face_recognition is converted.
        """
        self.face_collection.add(
            embeddings=[embedding.tolist()],
            ids=[f'face_{entity_id}_{time.time()}'],
            metadatas=[{'entity_id': entity_id}]
        )

    def find_matching_face(self, embedding, threshold=0.6):
        """
        Searches stored face embeddings for the closest match to the
        given embedding. Returns the matching entity_id if the distance
        is under threshold (lower = more similar), otherwise None.

        threshold=0.6 is the standard face_recognition library default
        for "same person" -- lower values are stricter matches.
        """
        try:
            count = self.face_collection.count()
            if count == 0:
                return None

            results = self.face_collection.query(
                query_embeddings=[embedding.tolist()],
                n_results=1
            )

            if not results['distances'] or not results['distances'][0]:
                return None

            closest_distance = results['distances'][0][0]
            if closest_distance <= threshold:
                return results['metadatas'][0][0]['entity_id']

            return None
        except Exception:
            return None
    