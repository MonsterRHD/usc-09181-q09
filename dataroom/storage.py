"""SQLite persistence.

One database file holds the whole data room.  WAL mode plus a single
writer connection guarded by a lock gives us serialisable writes while
read requests stay concurrent.  All state lives here, which is what
makes "the program runs again and review items / access records are
still queryable" hold: nothing is kept only in memory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    team          TEXT NOT NULL CHECK (team IN ('finance','legal','tax','admin')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','departed')),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL,
    timezone    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(id),
    title              TEXT NOT NULL,
    country            TEXT NOT NULL,
    clearance_required INTEGER NOT NULL CHECK (clearance_required BETWEEN 1 AND 3),
    status             TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','scope_changed','closed')),
    deadline_at        TEXT NOT NULL,           -- UTC instant
    deadline_tz        TEXT NOT NULL,
    created_by         TEXT NOT NULL REFERENCES users(id),
    created_at         TEXT NOT NULL,
    closed_at          TEXT
);

CREATE TABLE IF NOT EXISTS request_scope_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES requests(id),
    kind        TEXT NOT NULL CHECK (kind IN ('scope_change','deadline_extension','closed')),
    detail      TEXT NOT NULL,                   -- JSON payload
    actor_id    TEXT NOT NULL REFERENCES users(id),
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id                  TEXT PRIMARY KEY,
    request_id          TEXT NOT NULL REFERENCES requests(id),
    filename            TEXT NOT NULL,
    classification      TEXT NOT NULL CHECK (classification IN ('public','confidential','secret')),
    clearance_level     INTEGER NOT NULL CHECK (clearance_level BETWEEN 1 AND 3),
    current_version     INTEGER NOT NULL DEFAULT 0,
    supplier_withdrawn  INTEGER NOT NULL DEFAULT 0,
    withdrawn_at        TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(id),
    version      INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    uploader_id  TEXT NOT NULL REFERENCES users(id),
    summary      TEXT NOT NULL DEFAULT '',
    uploaded_at  TEXT NOT NULL,
    UNIQUE (document_id, version)
);

-- Reviews are attached to an *explicit version*: a later upload of the
-- same document never replaces evidence already cited.
CREATE TABLE IF NOT EXISTS reviews (
    id                TEXT PRIMARY KEY,
    document_id       TEXT NOT NULL REFERENCES documents(id),
    version_id        TEXT NOT NULL REFERENCES document_versions(id),
    reviewer_id       TEXT NOT NULL REFERENCES users(id),
    conclusion        TEXT NOT NULL CHECK (conclusion IN ('pending','approved','flagged','superseded')),
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    decided_at        TEXT
);

CREATE TABLE IF NOT EXISTS review_conclusions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id   TEXT NOT NULL REFERENCES reviews(id),
    conclusion  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    at          TEXT NOT NULL
);

-- Evidence citations freeze (document, version, content hash) so the
-- impact chain survives uploads, withdrawals and scope changes.
CREATE TABLE IF NOT EXISTS evidence_citations (
    id              TEXT PRIMARY KEY,
    review_id       TEXT NOT NULL REFERENCES reviews(id),
    document_id     TEXT NOT NULL REFERENCES documents(id),
    version_id      TEXT NOT NULL REFERENCES document_versions(id),
    sha256          TEXT NOT NULL,
    quoted_filename TEXT NOT NULL,
    quoted_version  INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

-- Per-project grants: role + maximum clearance this user may open.
CREATE TABLE IF NOT EXISTS permissions (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(id),
    user_id          TEXT NOT NULL REFERENCES users(id),
    role             TEXT NOT NULL DEFAULT 'viewer'
                       CHECK (role IN ('admin','reviewer','viewer')),
    max_clearance    INTEGER NOT NULL DEFAULT 1 CHECK (max_clearance BETWEEN 1 AND 3),
    revoked_at       TEXT,
    updated_at       TEXT NOT NULL,
    updated_by       TEXT REFERENCES users(id),
    UNIQUE (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS permission_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    action      TEXT NOT NULL,                  -- grant / update / revoke
    before_json TEXT,
    after_json  TEXT,
    actor_id    TEXT NOT NULL,
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_restrictions (
    id            TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents(id),
    status        TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','lifted')),
    reason        TEXT NOT NULL DEFAULT '',
    imposed_at    TEXT NOT NULL,
    lifted_at     TEXT
);

CREATE TABLE IF NOT EXISTS impact_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,                  -- document / request
    subject_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
                 -- withdraw, upload_version, scope_change, deadline_extension,
                 -- restriction_lift, citation_preserved
    detail       TEXT NOT NULL,                  -- JSON
    actor_id     TEXT,
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
    id             TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES documents(id),
    version_id     TEXT NOT NULL REFERENCES document_versions(id),
    purpose        TEXT NOT NULL,
    granted_by     TEXT NOT NULL REFERENCES users(id),
    watermark_policy TEXT NOT NULL DEFAULT 'stamp',
    issued_at      TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    revoked_at     TEXT,
    revoke_reason  TEXT NOT NULL DEFAULT '',
    max_accesses   INTEGER,                     -- NULL = unlimited
    access_count   INTEGER NOT NULL DEFAULT 0
);

-- Every open / denial is recorded, including expired and revoked tries,
-- so "who tried to see the payroll file" remains answerable.
CREATE TABLE IF NOT EXISTS access_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    actor_id      TEXT,                         -- NULL for unauthenticated link hits
    link_id       TEXT,
    document_id   TEXT,
    version_id    TEXT,
    project_id    TEXT,
    purpose       TEXT NOT NULL DEFAULT '',
    decision      TEXT NOT NULL
                  CHECK (decision IN ('allowed','watermarked','denied_clearance',
                                      'denied_membership','denied_departed',
                                      'denied_withdrawn','denied_restriction',
                                      'expired','revoked','deprecated_version')),
    reason        TEXT NOT NULL DEFAULT '',
    watermark     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS document_blobs (
    sha256      TEXT PRIMARY KEY,
    content     BLOB NOT NULL,
    media_type  TEXT NOT NULL DEFAULT 'text/plain'
);

CREATE TABLE IF NOT EXISTS import_keys (
    batch_key   TEXT NOT NULL,
    item_ref    TEXT NOT NULL,
    request_id  TEXT,
    document_id TEXT,
    at          TEXT NOT NULL,
    PRIMARY KEY (batch_key, item_ref)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    user_id     TEXT,                          -- NULL = project-wide
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    dedup_key   TEXT NOT NULL,
    UNIQUE (dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_versions_doc ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_reviews_doc   ON reviews(document_id);
CREATE INDEX IF NOT EXISTS idx_access_doc    ON access_records(document_id);
CREATE INDEX IF NOT EXISTS idx_access_time   ON access_records(at);
CREATE INDEX IF NOT EXISTS idx_impact_subj   ON impact_events(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_links_doc     ON share_links(document_id);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        parent = os.path.dirname(path)
        if parent and path != ":memory:":
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )

    @property
    def lock(self):
        return self._lock

    def conn(self):
        return self._conn

    # -- small helpers ---------------------------------------------------
    # Every statement takes the re-entrant lock: worker threads share one
    # connection (WAL), and a fetch must not be interleaved by another
    # thread's execute.  Transactions keep holding it via _tx().
    def get(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def execute(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args)

    def close(self):
        with self._lock:
            self._conn.close()

    @staticmethod
    def row_to_dict(row: sqlite3.Row | None):
        return dict(row) if row is not None else None
