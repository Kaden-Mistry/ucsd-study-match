# UCSD Study & Project Partner Match

Post that you're in a course looking for a study or project partner; other
verified UCSD students in that course can find you and message you.

No scraped data, no login-wall data source — everything here is
user-submitted, which sidesteps the CAPE/SET data restrictions entirely.

## What's tested

I ran the full flow end-to-end against a live local server (not just read
through the code) before handing this to you:

- ✅ Signup rejects non-`.edu` emails (422)
- ✅ Signup + verification code flow works
- ✅ Session token issued and required for protected routes (401 without it)
- ✅ Creating a posting works
- ✅ Browsing postings for a course shows other students, correctly excludes
  your own postings
- ✅ Preference filter (study/project/both) works correctly
- ✅ Messaging works; self-messaging your own posting is blocked (400)
- ✅ Inbox aggregates conversations correctly

The frontend is written and self-reviewed but not clicked through in an
actual browser session — worth doing a manual pass once it's running.

## ⚠️ Before this works for real people: wire up real email sending

Right now, `send_verification_email()` in `backend/app.py` just **prints the
code to the server console** — it doesn't send an actual email. That's fine
for testing it yourself, but nobody else can sign up until you connect it to
a real provider. Options, roughly easiest to hardest:

- **Resend** (resend.com) — simple API, generous free tier, good for a
  student project
- **SendGrid** — similar, well-documented
- **AWS SES** — cheapest at scale, more setup

Wherever you land, you'll `pip install` their SDK and replace the body of
`send_verification_email()` with an actual send call — the function
signature and where it's called don't need to change.

## Setup

```bash
pip install -r requirements.txt
```

### 1. Initialize the database

```bash
cd backend
python3 -c "import sqlite3; conn = sqlite3.connect('study_match.db'); conn.executescript(open('schema.sql').read()); conn.commit()"
```

### 2. Run the backend

```bash
uvicorn app:app --reload --port 8001
```

### 3. Open the frontend

Just open `frontend/index.html` in a browser. It's hardcoded to call the API
at `http://127.0.0.1:8001` — change `API_BASE` near the top of the
`<script>` tag if you deploy the backend elsewhere.

### 4. Try it yourself

1. Sign up with your `@ucsd.edu` email
2. Since real email isn't wired up yet, check your terminal — the 6-digit
   code prints there (`[DEV MODE] Verification code for you@ucsd.edu: 123456`)
3. Enter that code in the browser to finish verifying
4. Post that you're looking for a partner in some course
5. Open a second browser (or incognito window), sign up as a second test
   email, and confirm you can see and message the first posting

## Notes on scope / what's not built yet

- **No password reset / re-login flow beyond re-verifying** — session
  tokens don't expire, which is fine for a first version but worth adding
  expiry to eventually
- **Display name is optional and self-reported** — there's no real identity
  verification beyond "owns this .edu inbox," which matches how most
  campus tools like this work (Fizz, campus marketplaces, etc.)
- **No block/report feature** — worth adding before wider release, since
  this involves students messaging strangers
- **No editing existing postings from the UI** — the API supports
  re-posting (it'll update your existing posting for that course), but
  there's no "edit" button, only "post again"
