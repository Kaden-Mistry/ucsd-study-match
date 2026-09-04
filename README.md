# UCSD Study Match

A web app that helps UCSD students find study and project partners for the classes they're currently enrolled in.

**Live app:** [ucsdstudymatch.com](https://ucsdstudymatch.com)

## Features

- **Google Sign-In authentication** — sign in with any Google account; the ID token is verified server-side against Google's public certificates
- **Course-based matching** — post that you're looking for a study or project partner in a specific course, and browse others posted in that same course
- **Post to multiple courses at once** — add several course rows to a single submission
- **Direct messaging inbox** — a full conversation view per person, independent of which posting started it, with unread indicators on both the conversation list and the Inbox tab itself
- **Block/unblock users** — hides their postings from your search and removes them from your inbox; they're blocked from messaging you
- **Edit and delete your own postings** — inline editing, with cascading cleanup of any messages tied to a deleted posting
- **Email notifications** — recipients get notified by email when they receive a new message

## Tech Stack

- **Backend:** FastAPI + SQLite, deployed on Railway with a persistent volume for the database
- **Auth:** Firebase Authentication (Google Sign-In), verified server-side via `google-auth` — no Firebase Admin SDK or service-account secret required
- **Frontend:** Vanilla HTML, CSS, and JavaScript — no framework or build step
- **Email:** Resend

## Getting Started (local dev)

### Backend

```bash
cd backend
cp .env.example .env   # fill in FIREBASE_PROJECT_ID at minimum
pip install -r ../requirements.txt
uvicorn app:app --reload --port 8001
```

The database and its tables are created automatically on startup.

### Frontend

Firebase's sign-in flow requires an authorized `http(s)` origin, so serve the frontend rather than opening it as a `file://` path:

```bash
cd frontend
python3 -m http.server 5500
```

Then visit `http://localhost:5500`. Update `API_BASE` near the top of the `<script>` tag if your backend isn't running on `http://127.0.0.1:8001`.

## Notes on scope

- Session tokens don't currently expire
- No password reset flow — re-authentication is always via Google
- Display name comes from your Google account and isn't currently editable in the UI
