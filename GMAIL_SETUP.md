# Gmail API Setup Guide

The email agent needs permission to read your Gmail and create drafts.
This is a one-time setup that takes about 10 minutes.

---

## Step 1 — Enable Gmail API

1. Go to https://console.cloud.google.com
2. Create a new project (e.g. "MECA Agent")
3. Go to **APIs & Services → Library**
4. Search for **Gmail API** → click **Enable**

---

## Step 2 — Create OAuth credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Name: `MECA Agent`
5. Click **Create**
6. Click **Download JSON**
7. Rename the file to `gmail_credentials.json`
8. Move it to your project's `config/` folder:
   ```
   C:\Users\MuzafferArpacik\PycharmProjects\AiAgentFetchBookings\config\gmail_credentials.json
   ```

---

## Step 3 — Configure OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
2. User type: **External**
3. App name: `MECA Agent`
4. Support email: your Gmail address
5. Add scope: `https://www.googleapis.com/auth/gmail.modify`
6. Add your Gmail address as a **test user**
7. Save

---

## Step 4 — Install dependencies

```
pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

---

## Step 5 — Authorize (one time only)

Run the email agent once manually:
```
python email_agent.py
```

A browser window will open asking you to log in to Google and grant
permission. After you approve, a `config/gmail_token.json` file is
saved. This token is reused automatically from then on — you never
need to log in again unless you revoke access.

---

## Step 6 — Add to Task Scheduler

Add a third scheduled task to run every 30 minutes:

- Program: `python`
- Arguments: `email_agent.py`
- Start in: `C:\Users\MuzafferArpacik\PycharmProjects\AiAgentFetchBookings`
- Trigger: **Daily**, repeat every **30 minutes**

---

## How it works after setup

```
Every 30 minutes:
  Check Gmail for unread emails (excluding MECA portal notifications)
      ↓
  For each new email → Claude reads it and decides:
      Missing info?  → Draft asking for reg number, km, problem description
      Enough info?   → Draft with price estimate and booking suggestion
      ↓
  Draft saved in Gmail (nothing sent automatically)
      ↓
  SMS to you: "Nytt kundmail från [name] — utkast klart i Gmail"
      ↓
  You open Gmail, review draft, click Send
```

---

## Files created
- `config/gmail_credentials.json` — your OAuth app credentials (keep secret)
- `config/gmail_token.json` — auto-created after first login (keep secret)
- `config/seen_email_ids.txt` — tracks processed emails
- `logs/email_agent.log` — run history
