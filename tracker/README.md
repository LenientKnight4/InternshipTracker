# Internship tracker (asset management / equity research / credit)

Checks the careers sites of 70 banks, asset managers, credit funds and hedge funds every
30 minutes, and buzzes your phone the moment a new AM / ER / public or private credit
internship is posted. It runs free
on GitHub Actions, so it keeps working while your laptop is closed over break.

- **Phone alert** for every new AM / ER internship. Tapping the alert opens the application.
- **`JOBS.md` dashboard** listing everything open now, plus what closed recently.
- **Google Sheet** (optional) that fills itself in, with a Status column for tracking applications.
- **Health checks**: if a firm's site breaks or blocks the tracker, you get told, so silence always means "nothing new".
- **Weekly check-in** push so you know it's alive.

## Setup (about 15 minutes)

### 1. Phone alerts (ntfy)
1. Install the **ntfy** app from the App Store or Google Play ([setup guide](https://docs.ntfy.sh/subscribe/phone/)).
2. Tap **+** and subscribe to a topic with a long random name, e.g. `aminterns-7f3k9q2xw`.
   Anyone who knows the name can read it, so make it unguessable.

### 2. Put this folder on GitHub
Create an empty repository at <https://github.com/new>. **Public is recommended**: Actions minutes
are unlimited for public repos, and the repo only contains public job postings (your secrets stay hidden).
Private works too, but change the schedule in `.github/workflows/tracker.yml` to hourly to stay
inside the free 2,000 minutes/month.

From this folder:

```bash
git init -b main
```

```bash
git add . && git commit -m "Internship tracker"
```

```bash
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
```

```bash
git push -u origin main
```

### 3. Add your ntfy topic as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name `NTFY_TOPIC`, value: your topic name from step 1.

### 4. Turn it on
1. Repo → **Actions** tab → enable workflows if asked.
2. Click **internship-tracker → Run workflow**, check **"Just send a test notification"**, run. Your phone should buzz.
3. Run it again with nothing checked. You'll get a **"Tracker is live"** push listing what's open right now,
   and `JOBS.md` appears in the repo. From then on it runs every 30 minutes on its own.

### 5. (Optional) Google Sheet
Follow the steps at the top of [`sheets/apps_script.gs`](sheets/apps_script.gs), then add the
`SHEET_WEBHOOK_URL` and `SHEET_TOKEN` secrets. Run the workflow once with **"Also copy every open
posting into the Google Sheet"** checked to backfill it. After that, new postings are added automatically.

## Handshake

Handshake requires your school login and has no public job feed, so the tracker doesn't log in as you.
Use Handshake's own alerts instead:

1. In Handshake, search for each of: `asset management`, `equity research`, `investment research`,
   `investment management`, `summer analyst`. Filter **Job type → Internship**.
2. Click **Save search** on each and turn notifications on. Install the Handshake app for push alerts.

**Optional: route Handshake alerts into the same ntfy feed.**
1. Gmail → Settings → **Forwarding and POP/IMAP** → Add a forwarding address: `ntfy-YOUR-TOPIC@ntfy.sh`.
2. Gmail emails a confirmation code to that address, and it arrives as a notification in the ntfy app.
   Enter the code in Gmail.
3. Create a Gmail filter: **From** `joinhandshake.com` → **Forward it to** `ntfy-YOUR-TOPIC@ntfy.sh`.

ntfy shows the email's subject line as the notification title. If the confirmation code doesn't come
through, just rely on Handshake's app notifications.

## What gets alerted

| Priority | What | Alert |
|---|---|---|
| AM / ER / credit | Asset management, equity / investment research, portfolio, fixed income, public & private credit (direct lending, high yield, distressed, special situations, leveraged finance…), investment analyst roles. At credit specialists (Ares, Blue Owl, Oaktree, Sixth Street, OHA, …) any non-tech/ops internship. | Instant push (max priority). Tapping it opens the job. |
| Related | Wealth management roles anywhere; other business internships at asset managers | One grouped push per run |
| Low | IB, tech, ops, risk, HR, recruiting events at watched firms | Dashboard only |
| Ignored | Not an internship, MBA / PhD / Masters level, wrong year, outside the US | Nothing |

Other pushes:
- **"Tracker can't check X"**: a firm's site failed 3 runs in a row (about 90 minutes). Check that firm by hand until you get **"X is back"**.
- **Weekly check-in**: counts for the week plus any failing sources.
- GitHub also emails you if the whole workflow crashes.

## Customizing

- **Which summers**: `target_years` in `filters.yaml` (default 2027 and 2028).
- **Keywords**: all lists in `filters.yaml`. Postings land in whichever bucket their title matches.
- **Add a firm**: click "Apply" on any job at the firm, look at the URL, and add one line to `firms.yaml`.
  The comments at the top of that file show the pattern for each hiring system.
- **`manual:` list** in `firms.yaml`: firms that block bots. They're shown at the bottom of the dashboard
  as a reminder to join their campus mailing lists.

Test changes locally before pushing:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python run.py --dry-run
```

`--dry-run` fetches everything and prints what it would alert on, without sending or saving anything.
`--only "goldman"` checks just one firm.

## Limits

- It watches the firms' own careers sites. Firms that block automated checks (UBS, Citadel, …) are on
  the manual list instead.
- It searches each site for "internship", "summer", "2027" and "2028". A posting with none of those words
  in it could be missed. The low-priority list on the dashboard shows the near-misses.
- GitHub's scheduler can run 5–20 minutes late at busy times.
- If a site changes its format, that firm starts failing and you get a push. Fixing it usually means
  updating one line in `firms.yaml`.
