"""
FastAPI backend for the UCSD study/project partner matcher.

Run locally:
    pip install fastapi uvicorn
    uvicorn app:app --reload

⚠️ EMAIL SENDING: send_verification_email() below is a STUB — it prints the
code to the server console instead of actually emailing it. My sandbox can't
send real email. Before this is usable by real people, wire it up to an
actual email provider (see the TODO in that function) — otherwise nobody
can complete signup except you, watching your own server logs.
"""

import random
import secrets
import sqlite3
import string
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator

DB_PATH = Path(__file__).parent / "study_match.db"

# Only these domains can sign up — adjust if grad/extension students use a
# different subdomain (e.g. checking with UCSD's actual list is worth doing).
ALLOWED_EMAIL_DOMAINS = ["ucsd.edu"]

app = FastAPI(title="UCSD Study/Project Partner Matcher")
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


def send_verification_email(email: str, code: str):
    """
    TODO: replace with a real email send — e.g. via SMTP, SendGrid, Postmark,
    or AWS SES. For now this just logs to the console so you can test the
    flow yourself locally.
    """
    print(f"\n[DEV MODE] Verification code for {email}: {code}\n")


def generate_code() -> str:
    return "".join(random.choices(string.digits, k=6))


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

class SignupRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def must_be_allowed_domain(cls, v: str) -> str:
        domain = v.split("@")[-1].lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            raise ValueError(f"Email must end in one of: {', '.join(ALLOWED_EMAIL_DOMAINS)}")
        return v


class VerifyRequest(BaseModel):
    email: EmailStr
    code: str
    display_name: Optional[str] = None


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


class MessageCreate(BaseModel):
    posting_id: int
    body: str


# ---------- Auth endpoints ----------

@app.post("/api/signup")
def signup(req: SignupRequest):
    """Start signup: create (or reuse) a user row and send a verification code."""
    conn = get_db()
    code = generate_code()

    existing = conn.execute("SELECT id FROM users WHERE email = ?", (req.email,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET verification_code = ?, verification_sent_at = datetime('now') WHERE email = ?",
            (code, req.email),
        )
    else:
        conn.execute(
            "INSERT INTO users (email, verification_code, verification_sent_at) VALUES (?, ?, datetime('now'))",
            (req.email, code),
        )
    conn.commit()
    conn.close()

    send_verification_email(req.email, code)
    return {"message": "Verification code sent. Check your email."}


@app.post("/api/verify")
def verify(req: VerifyRequest):
    """Confirm the code and issue a session token."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (req.email,)).fetchone()

    if user is None:
        conn.close()
        raise HTTPException(status_code=404, detail="No signup found for this email")
    if user["verification_code"] != req.code:
        conn.close()
        raise HTTPException(status_code=400, detail="Incorrect code")

    token = secrets.token_urlsafe(32)
    conn.execute(
        """UPDATE users SET is_verified = 1, verification_code = NULL,
           session_token = ?, display_name = COALESCE(?, display_name)
           WHERE id = ?""",
        (token, req.display_name, user["id"]),
    )
    conn.commit()
    conn.close()
    return {"session_token": token}


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
    """
    params = [subject.strip().upper(), catalog_number.strip().upper(), user["id"]]

    if preference:
        # 'both' postings show up regardless of which specific preference is requested
        query += " AND (p.preference = ? OR p.preference = 'both')"
        params.append(preference)

    query += " ORDER BY p.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/postings/{posting_id}")
def deactivate_posting(posting_id: int, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    result = conn.execute(
        "UPDATE postings SET is_active = 0 WHERE id = ? AND user_id = ?",
        (posting_id, user["id"]),
    )
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Posting not found or not yours")
    return {"message": "Posting removed"}


# ---------- Messaging endpoints ----------

@app.post("/api/messages")
def send_message(req: MessageCreate, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()

    posting = conn.execute("SELECT * FROM postings WHERE id = ?", (req.posting_id,)).fetchone()
    if posting is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Posting not found")
    if posting["user_id"] == user["id"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Can't message your own posting")

    conn.execute(
        "INSERT INTO messages (posting_id, from_user_id, to_user_id, body) VALUES (?, ?, ?, ?)",
        (req.posting_id, user["id"], posting["user_id"], req.body),
    )
    conn.commit()
    conn.close()
    return {"message": "Sent"}


@app.get("/api/messages/{posting_id}")
def get_thread(posting_id: int, authorization: Optional[str] = Header(None)):
    """All messages between the current user and the posting owner for this posting."""
    user = get_current_user(authorization)
    conn = get_db()
    rows = conn.execute(
        """SELECT m.*, u.display_name AS from_name
           FROM messages m
           JOIN users u ON u.id = m.from_user_id
           WHERE m.posting_id = ?
             AND (m.from_user_id = ? OR m.to_user_id = ?)
           ORDER BY m.created_at ASC""",
        (posting_id, user["id"], user["id"]),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/inbox")
def get_inbox(authorization: Optional[str] = Header(None)):
    """Distinct conversations (by posting) the current user is part of, most recent first."""
    user = get_current_user(authorization)
    conn = get_db()
    rows = conn.execute(
        """
        SELECT p.id AS posting_id, c.subject, c.catalog_number,
               MAX(m.created_at) AS last_message_at,
               COUNT(*) AS message_count
        FROM messages m
        JOIN postings p ON p.id = m.posting_id
        JOIN courses c ON c.id = p.course_id
        WHERE m.from_user_id = ? OR m.to_user_id = ?
        GROUP BY p.id
        ORDER BY last_message_at DESC
        """,
        (user["id"], user["id"]),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/health")
def health_check():
    return {"status": "ok"}
