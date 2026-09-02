"""
FastAPI backend for the UCSD study/project partner matcher.

Run locally:
    pip install fastapi uvicorn
    uvicorn app:app --reload

Auth: Firebase Authentication (Google Sign-In) on the frontend, verified
here by checking the Firebase ID token's signature and claims directly
against Google's public certs (via google-auth's verify_firebase_token) —
no firebase-admin SDK or service-account secret needed, since we only ever
verify tokens, never mint or manage them. See FIREBASE_PROJECT_ID below.
"""

import html
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

import resend
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, field_validator

# Loads variables from a local .env file (not committed to git) into the
# environment, so FIREBASE_PROJECT_ID below can find it.
load_dotenv()

DB_PATH = Path(__file__).parent / "study_match.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def migrate_db(conn: sqlite3.Connection):
    """
    Brings a database created before the Firebase Auth switchover up to
    date. CREATE TABLE IF NOT EXISTS in schema.sql only handles brand-new
    databases — an existing users table (e.g. the one already deployed on
    Railway) needs its new column added explicitly.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "firebase_uid" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN firebase_uid TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_firebase_uid
           ON users(firebase_uid) WHERE firebase_uid IS NOT NULL"""
    )

    # posting_id used to be NOT NULL (every message had to be about a
    # posting). Direct inbox replies aren't tied to a posting, so it needs
    # to allow NULL. SQLite can't ALTER a column's constraint in place, so
    # an existing table with the old constraint gets rebuilt — this
    # preserves all existing rows.
    msg_cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(messages)")}
    if msg_cols and msg_cols["posting_id"]["notnull"] == 1:
        conn.executescript(
            """
            ALTER TABLE messages RENAME TO messages_old;
            CREATE TABLE messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id      INTEGER REFERENCES postings(id),
                from_user_id    INTEGER NOT NULL REFERENCES users(id),
                to_user_id      INTEGER NOT NULL REFERENCES users(id),
                body            TEXT NOT NULL,
                created_at      TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO messages (id, posting_id, from_user_id, to_user_id, body, created_at)
                SELECT id, posting_id, from_user_id, to_user_id, body, created_at FROM messages_old;
            DROP TABLE messages_old;
            CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(posting_id, from_user_id, to_user_id);
            """
        )
    conn.commit()


def init_db_if_needed():
    """
    Creates the database and its tables if they don't exist yet, then runs
    any pending migrations. Runs automatically on startup so a fresh deploy
    (Railway, or anyone else's machine) never hits 'no such table' — this
    was previously a manual step that got skipped on the first Railway
    deploy.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    migrate_db(conn)
    conn.close()

# The Firebase project ID is not a secret — it's part of every ID token's
# audience/issuer claims — but it must match the project the frontend's
# firebaseConfig points at, or every sign-in will fail verification below.
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID")
_google_auth_request = google_auth_requests.Request()

# Guards the one-time POST /api/admin/reset-data cleanup endpoint. See
# .env.example for what this protects and why.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")

# Powers the "you have a new message" email notification. Optional — if
# unset, send_new_message_email() below just no-ops, same as the old
# verification-email flow did before Firebase Auth replaced it.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


def send_new_message_email(to_email: str, sender_name: str):
    """
    Best-effort notification email — never raises, so a Resend outage or a
    missing API key can't block a message from actually sending.
    """
    if not RESEND_API_KEY:
        return
    try:
        resend.Emails.send({
            "from": "UCSD Study Match <noreply@ucsdstudymatch.com>",
            "to": to_email,
            "subject": "New message on Study & Project Match",
            "html": f"<p>You have a new message on Study & Project Match from {html.escape(sender_name)}.</p>",
        })
    except Exception:
        pass

app = FastAPI(title="UCSD Study/Project Partner Matcher")


@app.on_event("startup")
def on_startup():
    init_db_if_needed()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before deploying
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_user(authorization: Optional[str] = Header(None)) -> sqlite3.Row:
    """Bearer-token auth. Pass 'Authorization: Bearer <token>' on protected requests."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE session_token = ? AND is_verified = 1", (token,)
    ).fetchone()
    conn.close()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


# ---------- Request/response models ----------

class GoogleAuthRequest(BaseModel):
    id_token: str


class PostingCreate(BaseModel):
    subject: str
    catalog_number: str
    preference: str  # 'study' | 'project' | 'both'
    note: Optional[str] = None

    @field_validator("preference")
    @classmethod
    def valid_preference(cls, v: str) -> str:
        if v not in ("study", "project", "both"):
            raise ValueError("preference must be 'study', 'project', or 'both'")
        return v


class PostingUpdate(BaseModel):
    preference: str  # 'study' | 'project' | 'both'
    note: Optional[str] = None

    @field_validator("preference")
    @classmethod
    def valid_preference(cls, v: str) -> str:
        if v not in ("study", "project", "both"):
            raise ValueError("preference must be 'study', 'project', or 'both'")
        return v


class MessageCreate(BaseModel):
    posting_id: int
    body: str


class DirectMessageCreate(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message can't be empty")
        return v


# ---------- Auth endpoints ----------

@app.post("/api/auth/google")
def auth_google(req: GoogleAuthRequest):
    """
    Exchange a Firebase (Google Sign-In) ID token for our own session token.

    The ID token's signature and claims are checked directly against
    Google's public certs — this proves the user owns the Google account
    and that its email is verified. Any verified Google account is accepted;
    there's no domain restriction.
    """
    if not FIREBASE_PROJECT_ID:
        raise HTTPException(status_code=500, detail="Server is missing FIREBASE_PROJECT_ID configuration")

    try:
        claims = google_id_token.verify_firebase_token(
            req.id_token, _google_auth_request, audience=FIREBASE_PROJECT_ID
        )
    except ValueError:
        claims = None
    if claims is None:
        raise HTTPException(status_code=401, detail="Invalid or expired Google sign-in token")

    if not claims.get("email_verified"):
        raise HTTPException(status_code=403, detail="Google account email is not verified")

    email = (claims.get("email") or "").lower()
    firebase_uid = claims.get("sub")
    google_name = claims.get("name")
    token = secrets.token_urlsafe(32)

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        conn.execute(
            """INSERT INTO users (email, display_name, firebase_uid, is_verified, session_token)
               VALUES (?, ?, ?, 1, ?)""",
            (email, google_name, firebase_uid, token),
        )
    else:
        conn.execute(
            """UPDATE users SET display_name = COALESCE(display_name, ?), firebase_uid = ?,
               is_verified = 1, session_token = ? WHERE id = ?""",
            (google_name, firebase_uid, token, user["id"]),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    return {
        "id": row["id"],
        "session_token": token,
        "display_name": row["display_name"] or email.split("@")[0],
    }


@app.get("/api/me")
def get_me(authorization: Optional[str] = Header(None)):
    """Lets the frontend restore 'signed in as X' after a page refresh without re-authenticating."""
    user = get_current_user(authorization)
    return {"id": user["id"], "email": user["email"], "display_name": user["display_name"]}


# ---------- Course/posting endpoints ----------

def get_or_create_course(conn: sqlite3.Connection, subject: str, catalog_number: str) -> int:
    subject = subject.strip().upper()
    catalog_number = catalog_number.strip().upper()
    row = conn.execute(
        "SELECT id FROM courses WHERE subject = ? AND catalog_number = ?",
        (subject, catalog_number),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO courses (subject, catalog_number) VALUES (?, ?)",
        (subject, catalog_number),
    )
    return cur.lastrowid


@app.post("/api/postings")
def create_posting(req: PostingCreate, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    course_id = get_or_create_course(conn, req.subject, req.catalog_number)

    try:
        conn.execute(
            """INSERT INTO postings (user_id, course_id, preference, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, course_id) DO UPDATE SET
                 preference=excluded.preference, note=excluded.note,
                 is_active=1, created_at=datetime('now')""",
            (user["id"], course_id, req.preference, req.note),
        )
        conn.commit()
    finally:
        conn.close()
    return {"message": "Posting created"}


@app.get("/api/postings")
def list_postings(
    subject: str = Query(...),
    catalog_number: str = Query(...),
    preference: Optional[str] = Query(None, description="Filter by 'study', 'project', or 'both'"),
    authorization: Optional[str] = Header(None),
):
    """List everyone else's active postings for a given course — the core matching view."""
    user = get_current_user(authorization)
    conn = get_db()

    query = """
        SELECT p.id AS posting_id, p.preference, p.note, p.created_at,
               u.id AS user_id, u.display_name
        FROM postings p
        JOIN users u ON u.id = p.user_id
        JOIN courses c ON c.id = p.course_id
        WHERE c.subject = ? AND c.catalog_number = ?
          AND p.is_active = 1
          AND u.id != ?
          AND NOT EXISTS (
              SELECT 1 FROM blocks b WHERE b.blocker_id = ? AND b.blocked_id = u.id
          )
    """
    params = [subject.strip().upper(), catalog_number.strip().upper(), user["id"], user["id"]]

    if preference:
        # 'both' postings show up regardless of which specific preference is requested
        query += " AND (p.preference = ? OR p.preference = 'both')"
        params.append(preference)

    query += " ORDER BY p.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/postings/mine")
def list_my_postings(authorization: Optional[str] = Header(None)):
    """The current user's own active postings, most recent first."""
    user = get_current_user(authorization)
    conn = get_db()
    rows = conn.execute(
        """
        SELECT p.id AS posting_id, c.subject, c.catalog_number, p.preference, p.note, p.created_at
        FROM postings p
        JOIN courses c ON c.id = p.course_id
        WHERE p.user_id = ? AND p.is_active = 1
        ORDER BY p.created_at DESC, p.id DESC
        """,
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/postings/{posting_id}")
def update_posting(posting_id: int, req: PostingUpdate, authorization: Optional[str] = Header(None)):
    """Owner-only edit of what they're looking for and their note (subject/course are immutable)."""
    user = get_current_user(authorization)
    conn = get_db()

    posting = conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
    if posting is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Posting not found")
    if posting["user_id"] != user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Not your posting")

    conn.execute(
        "UPDATE postings SET preference = ?, note = ? WHERE id = ?",
        (req.preference, req.note, posting_id),
    )
    conn.commit()
    conn.close()
    return {"message": "Posting updated"}


@app.delete("/api/postings/{posting_id}")
def delete_posting(posting_id: int, authorization: Optional[str] = Header(None)):
    """
    Permanently deletes a posting (not a soft is_active=0 flip) along with
    any messages tied to it, so deleting a posting doesn't leave messages
    pointing at a posting_id that no longer exists.
    """
    user = get_current_user(authorization)
    conn = get_db()

    posting = conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
    if posting is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Posting not found")
    if posting["user_id"] != user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Not your posting")

    conn.execute("DELETE FROM messages WHERE posting_id = ?", (posting_id,))
    conn.execute("DELETE FROM postings WHERE id = ?", (posting_id,))
    conn.commit()
    conn.close()
    return {"message": "Posting deleted"}


# ---------- Messaging endpoints ----------

def _is_blocked(conn: sqlite3.Connection, blocker_id: int, blocked_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker_id, blocked_id)
        ).fetchone()
        is not None
    )


@app.post("/api/messages")
def send_message(req: MessageCreate, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()

    posting = conn.execute(
        """SELECT p.*, u.email AS owner_email FROM postings p
           JOIN users u ON u.id = p.user_id WHERE p.id = ?""",
        (req.posting_id,),
    ).fetchone()
    if posting is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Posting not found")
    if posting["user_id"] == user["id"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Can't message your own posting")
    if _is_blocked(conn, posting["user_id"], user["id"]):
        conn.close()
        raise HTTPException(status_code=403, detail="You can't message this user")

    conn.execute(
        "INSERT INTO messages (posting_id, from_user_id, to_user_id, body) VALUES (?, ?, ?, ?)",
        (req.posting_id, user["id"], posting["user_id"], req.body),
    )
    conn.commit()
    conn.close()

    send_new_message_email(posting["owner_email"], user["display_name"] or user["email"].split("@")[0])
    return {"message": "Sent"}


@app.get("/api/conversations")
def list_conversations(authorization: Optional[str] = Header(None)):
    """
    The current user's conversations, one row per other person they've
    exchanged messages with — regardless of which posting (if any) started
    it — most recently active first. Excludes conversations with anyone the
    current user has blocked. Each row includes whether it has messages
    from the other person sent since the current user last opened it.
    """
    user = get_current_user(authorization)
    conn = get_db()
    rows = conn.execute(
        """
        WITH convo AS (
            SELECT
                CASE WHEN from_user_id = :me THEN to_user_id ELSE from_user_id END AS other_user_id,
                id, body, created_at,
                ROW_NUMBER() OVER (
                    PARTITION BY CASE WHEN from_user_id = :me THEN to_user_id ELSE from_user_id END
                    ORDER BY created_at DESC, id DESC
                ) AS rn
            FROM messages
            WHERE from_user_id = :me OR to_user_id = :me
        )
        SELECT c.other_user_id, u.display_name AS other_display_name,
               c.body AS last_message, c.created_at AS last_message_at,
               CASE WHEN EXISTS (
                   SELECT 1 FROM messages m2
                   WHERE m2.from_user_id = c.other_user_id AND m2.to_user_id = :me
                     AND m2.id > COALESCE(cr.last_read_message_id, 0)
               ) THEN 1 ELSE 0 END AS unread
        FROM convo c
        JOIN users u ON u.id = c.other_user_id
        LEFT JOIN conversation_reads cr ON cr.user_id = :me AND cr.other_user_id = c.other_user_id
        WHERE c.rn = 1
          AND NOT EXISTS (
              SELECT 1 FROM blocks b WHERE b.blocker_id = :me AND b.blocked_id = c.other_user_id
          )
        ORDER BY c.created_at DESC, c.id DESC
        """,
        {"me": user["id"]},
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/conversations/{other_user_id}")
def get_conversation_thread(other_user_id: int, authorization: Optional[str] = Header(None)):
    """
    The full message history between the current user and another user,
    oldest first. Opening a thread marks it read (updates conversation_reads).
    """
    user = get_current_user(authorization)
    conn = get_db()

    other = conn.execute("SELECT id, display_name FROM users WHERE id = ?", (other_user_id,)).fetchone()
    if other is None:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    rows = conn.execute(
        """SELECT id, from_user_id, to_user_id, body, created_at
           FROM messages
           WHERE (from_user_id = ? AND to_user_id = ?)
              OR (from_user_id = ? AND to_user_id = ?)
           ORDER BY created_at ASC, id ASC""",
        (user["id"], other_user_id, other_user_id, user["id"]),
    ).fetchall()

    is_blocked_by_me = _is_blocked(conn, user["id"], other_user_id)

    last_message_id = rows[-1]["id"] if rows else 0
    conn.execute(
        """INSERT INTO conversation_reads (user_id, other_user_id, last_read_message_id)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, other_user_id) DO UPDATE SET
             last_read_message_id = MAX(last_read_message_id, excluded.last_read_message_id)""",
        (user["id"], other_user_id, last_message_id),
    )
    conn.commit()
    conn.close()
    return {
        "other_user_id": other["id"],
        "other_display_name": other["display_name"],
        "is_blocked_by_me": is_blocked_by_me,
        "messages": [dict(r) for r in rows],
    }


@app.post("/api/conversations/{other_user_id}/messages")
def send_direct_message(
    other_user_id: int, req: DirectMessageCreate, authorization: Optional[str] = Header(None)
):
    """Reply within a conversation, independent of any specific posting."""
    user = get_current_user(authorization)
    if other_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Can't message yourself")

    conn = get_db()
    other = conn.execute("SELECT id, email FROM users WHERE id = ?", (other_user_id,)).fetchone()
    if other is None:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if _is_blocked(conn, other_user_id, user["id"]):
        conn.close()
        raise HTTPException(status_code=403, detail="You can't message this user")

    conn.execute(
        "INSERT INTO messages (posting_id, from_user_id, to_user_id, body) VALUES (NULL, ?, ?, ?)",
        (user["id"], other_user_id, req.body),
    )
    conn.commit()
    conn.close()

    send_new_message_email(other["email"], user["display_name"] or user["email"].split("@")[0])
    return {"message": "Sent"}


# ---------- Blocking ----------

@app.post("/api/users/{user_id}/block")
def block_user(user_id: int, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Can't block yourself")

    conn = get_db()
    other = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if other is None:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    conn.execute(
        "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (user["id"], user_id)
    )
    conn.commit()
    conn.close()
    return {"message": "Blocked"}


@app.delete("/api/users/{user_id}/block")
def unblock_user(user_id: int, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    conn.execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], user_id))
    conn.commit()
    conn.close()
    return {"message": "Unblocked"}


# ---------- Admin ----------

@app.post("/api/admin/reset-data")
def admin_reset_data(x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret")):
    """
    One-time cleanup: wipes all postings and messages, leaving users
    untouched. Meant for clearing out test data before real users show up —
    remove this endpoint (and unset ADMIN_SECRET) once it's no longer needed.
    """
    if not ADMIN_SECRET:
        raise HTTPException(status_code=500, detail="Server is missing ADMIN_SECRET configuration")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Secret header")

    conn = get_db()
    messages_deleted = conn.execute("DELETE FROM messages").rowcount
    postings_deleted = conn.execute("DELETE FROM postings").rowcount
    conn.commit()
    conn.close()
    return {"postings_deleted": postings_deleted, "messages_deleted": messages_deleted}


@app.get("/api/health")
def health_check():
    return {"status": "ok"}
