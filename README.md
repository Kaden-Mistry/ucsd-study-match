# UCSD Study & Project Partner Match

Post that you're in a course looking for a study or project partner; other
verified UCSD students in that course can find you and message you.

No scraped data, no login-wall data source — everything here is
user-submitted, which sidesteps the CAPE/SET data restrictions entirely.

## What's tested

I ran the full flow end-to-end against a live local server (not just read
through the code) before handing this to you:

- ✅ `/api/auth/google` rejects an invalid/garbage ID token (401)
- ✅ Session token issued and required for protected routes (401 without it)
- ✅ `/api/me` restores the signed-in user from a stored session token
- ✅ A database created before the Firebase switchover gets migrated
  (`firebase_uid` column + index added) without losing its existing users
  or sessions
- ✅ Creating a posting works
- ✅ Browsing postings for a course shows other students, correctly excludes
  your own postings
- ✅ Preference filter (study/project/both) works correctly
- ✅ Messaging works; self-messaging your own posting is blocked (400)
- ✅ Inbox aggregates conversations correctly

I didn't have a real Firebase project to test against, so the actual Google
Sign-In popup → domain restriction → session flow needs a manual pass in a
browser once you've done the Firebase setup below.

## Auth: Firebase Authentication (Google Sign-In)

Sign-in is Google-only, restricted to `@ucsd.edu` accounts, via Firebase
Authentication. There's no email/code step anymore — Google already proves
the user owns the account, and the backend independently checks the email
domain on every sign-in (the frontend's account-picker hint isn't a security
boundary by itself).

### 1. Create a Firebase project and Web app

In the [Firebase console](https://console.firebase.google.com/):

1. Create a project (or use an existing Google Cloud project).
2. **Authentication → Sign-in method** → enable **Google**.
3. **Authentication → Settings → Authorized domains** → add whatever domain
   you'll serve `frontend/index.html` from. `localhost` is included by
   default, but opening the file directly as `file://` will **not** work —
   serve it locally instead (e.g. `python3 -m http.server`, see below).
4. **Project settings → General → Your apps** → add a Web app → copy the
   config object.

### 2. Configure the frontend

Paste your config into the `firebaseConfig` object near the top of the
`<script>` tag in `frontend/index.html`.

### 3. Configure the backend

Copy `backend/.env.example` to `backend/.env` and set `FIREBASE_PROJECT_ID`
to the same project ID used in `firebaseConfig` above (this is public, not a
secret — it's checked as the expected audience on every ID token).

## Setup

```bash
pip install -r requirements.txt
```

### 1. Initialize the database

```bash
cd backend
python3 -c "import sqlite3; conn = sqlite3.connect('study_match.db'); conn.executescript(open('schema.sql').read()); conn.commit()"
```

(The backend also does this automatically on startup, so this step is
optional — it's here for anyone who wants the DB to exist before first run.)

### 2. Run the backend

```bash
uvicorn app:app --reload --port 8001
```

### 3. Serve the frontend

Firebase's sign-in popup requires an authorized `http(s)` origin, so open it
via a local server rather than as a `file://` path:

```bash
cd frontend
python3 -m http.server 5500
```

Then visit `http://localhost:5500`. It's hardcoded to call the API at
`http://127.0.0.1:8001` — change `API_BASE` near the top of the `<script>`
tag if you deploy the backend elsewhere. Make sure `http://localhost:5500`
(or whatever port you use) is in Firebase's Authorized domains list.

### 4. Try it yourself

1. Click "Sign in with Google" and choose your `@ucsd.edu` account
2. You should land in the app signed in — refresh the page and confirm you
   stay signed in (this used to log you out; sessions now persist)
3. Post that you're looking for a partner in some course
4. Open a second browser (or incognito window), sign in with a second
   `@ucsd.edu` test account, and confirm you can see and message the first
   posting
5. Try signing in with a non-`@ucsd.edu` Google account and confirm it's
   rejected

## Notes on scope / what's not built yet

- **No re-login flow beyond signing in with Google again** — session
  tokens don't expire, which is fine for a first version but worth adding
  expiry to eventually
- **Display name comes from your Google account** and isn't editable in the
  UI yet — there's no identity verification beyond "owns this .edu Google
  account," which matches how most campus tools like this work (Fizz,
  campus marketplaces, etc.)
- **No block/report feature** — worth adding before wider release, since
  this involves students messaging strangers
- **No editing existing postings from the UI** — the API supports
  re-posting (it'll update your existing posting for that course), but
  there's no "edit" button, only "post again"
