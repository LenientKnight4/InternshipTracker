"""Phone push (ntfy) and Google Sheet webhook. Both are optional: if the
environment variable isn't set, the message is printed instead."""
import os

import requests


def push(title, message, priority=3, url=None, tags=None, dry_run=False):
    """Send a phone notification via ntfy. priority: 1 (min) .. 5 (max)."""
    topic = os.environ.get("NTFY_TOPIC")
    if dry_run or not topic:
        print(f"[push p{priority}] {title} :: {message[:300]}")
        return
    body = {"topic": topic, "title": title[:250], "message": message[:3500], "priority": priority}
    if url:
        body["click"] = url
    if tags:
        body["tags"] = tags
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    try:
        requests.post(server, json=body, headers=headers, timeout=20).raise_for_status()
    except requests.RequestException as e:
        print(f"ntfy push failed: {e}")


def sheet_append(rows, dry_run=False):
    """POST new postings to the Apps Script web app that appends them to your Google Sheet."""
    hook = os.environ.get("SHEET_WEBHOOK_URL")
    if dry_run or not hook or not rows:
        if rows and not hook:
            print(f"[sheet] SHEET_WEBHOOK_URL not set; skipping {len(rows)} row(s)")
        return
    try:
        r = requests.post(hook, json={"token": os.environ.get("SHEET_TOKEN", ""), "rows": rows}, timeout=60)
        r.raise_for_status()
        print(f"[sheet] {r.text[:200]}")
    except requests.RequestException as e:
        print(f"sheet webhook failed: {e}")
