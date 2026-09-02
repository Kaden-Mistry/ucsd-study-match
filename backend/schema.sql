-- UCSD Study/Project Partner Matcher — Database Schema
PRAGMA foreign_keys = ON;

-- Verified UCSD students (via Firebase Google Sign-In, restricted to an
-- allowed .edu domain — no scraped data)
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT NOT NULL UNIQUE,       -- must end in an allowed .edu domain
    display_name        TEXT,                        -- optional, e.g. first name only for privacy
    firebase_uid        TEXT,                        -- Firebase Auth uid this email is bound to
    is_verified         INTEGER NOT NULL DEFAULT 0,  -- 0/1
    session_token       TEXT UNIQUE,                 -- simple bearer token, reissued each login
    created_at          TEXT DEFAULT (datetime('now'))
);

-- The unique index on users(firebase_uid) is created in migrate_db() in
-- app.py, not here: on a pre-Firebase database this CREATE TABLE is a
-- no-op (the table already exists without the column), so an index
-- statement here would fail until migrate_db() has added the column.

CREATE TABLE IF NOT EXISTS courses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT NOT NULL,     -- e.g. "CSE"
    catalog_number  TEXT NOT NULL,     -- e.g. "100"
    UNIQUE(subject, catalog_number)
);

-- One row per "I'm looking for a partner" post
CREATE TABLE IF NOT EXISTS postings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    course_id       INTEGER NOT NULL REFERENCES courses(id),
    preference      TEXT NOT NULL CHECK (preference IN ('study', 'project', 'both')),
    note            TEXT,              -- free text, e.g. "want to review before midterm 2"
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, course_id)         -- one active posting per user per course
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id      INTEGER REFERENCES postings(id),  -- NULL for a direct reply not tied to a posting
    from_user_id    INTEGER NOT NULL REFERENCES users(id),
    to_user_id      INTEGER NOT NULL REFERENCES users(id),
    body            TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_postings_course ON postings(course_id, is_active);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(posting_id, from_user_id, to_user_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- One row per "blocker has blocked blocked" relationship. One-directional:
-- only affects the blocker's own view (search results, inbox) and the
-- blocked user's ability to message the blocker — see app.py.
CREATE TABLE IF NOT EXISTS blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    blocker_id      INTEGER NOT NULL REFERENCES users(id),
    blocked_id      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(blocker_id, blocked_id)
);

-- Tracks, per user per conversation partner, the highest message id that
-- user had seen as of their last visit to that thread — used to compute
-- the unread indicator in GET /api/conversations. A message id (not a
-- timestamp) because SQLite's datetime('now') only has second resolution,
-- and a read followed by a reply within the same second needs to compare
-- exactly, not approximately.
CREATE TABLE IF NOT EXISTS conversation_reads (
    user_id                INTEGER NOT NULL REFERENCES users(id),
    other_user_id          INTEGER NOT NULL REFERENCES users(id),
    last_read_message_id   INTEGER NOT NULL,
    PRIMARY KEY (user_id, other_user_id)
);
