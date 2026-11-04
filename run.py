"""Internship tracker: checks every firm in firms.yaml, alerts on new AM/ER
internships, and rewrites JOBS.md.

  python run.py                 normal run (what GitHub Actions does)
  python run.py --dry-run       fetch + print what would happen; writes and sends nothing
  python run.py --only gold     only check sources whose name contains "gold"
  python run.py --test-push     send a test notification to your phone and exit
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from tracker.match import Matcher
from tracker.notify import push, sheet_append
from tracker.report import render
from tracker.sources import FETCHERS, SourceError, describe

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "state.json"
DASHBOARD = ROOT / "JOBS.md"
FAIL_ALERT_AFTER = 3          # consecutive failed runs before you get a "source broken" push
MAX_SINGLE_PUSHES = 8         # more new matches than this in one run -> one combined push
RANK = {"low": 0, "medium": 1, "high": 2}
HEARTBEAT_EVERY = timedelta(days=7)


def load_yaml(name):
    with open(ROOT / name) as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"initialized": False, "last_heartbeat": None, "jobs": {}, "sources": {},
            "week": {"high": 0, "medium": 0, "low": 0}}


def dashboard_url():
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"https://github.com/{repo}/blob/{branch}/JOBS.md" if repo else None


def fetch(src, terms, matcher, known):
    """Fetch one source, classify each posting, and check your class year can apply.

    Returns (src, [(job, priority, fit)], error). A posting's description is only
    downloaded when its title can't settle the class-year question and it isn't
    already in `known` (results from earlier runs).
    """
    try:
        found = FETCHERS[src["type"]](src, terms)
    except SourceError as e:
        return src, None, str(e)
    except Exception as e:  # a bug or unexpected format; report it as a source failure
        return src, None, f"{type(e).__name__}: {e}"[:200]
    out = []
    for job in found:
        prio = matcher.classify(job, src.get("kind", "bank"))
        fit = None
        if prio is not None:
            verdict = matcher.fit(job)
            if verdict:
                fit = {"ok": verdict[0], "why": verdict[1], "cache": False}
            elif job.key in known:
                fit = {**known[job.key], "cache": True}
            else:
                try:
                    ok, why = matcher.fit(job, describe(src, job))
                    fit = {"ok": ok, "why": why, "cache": True}
                except Exception:  # couldn't read the description; decide from the title, retry next run
                    ok, why = matcher.fit(job, "")
                    fit = {"ok": ok, "why": why, "cache": False}
        out.append((job, prio, fit))
    return src, out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--test-push", action="store_true")
    ap.add_argument("--sync-sheet", action="store_true", help="send every open AM/ER + related posting to the sheet")
    args = ap.parse_args()

    if args.test_push:
        push("Internship tracker test", "If you can read this, phone alerts work.", priority=4,
             url=dashboard_url(), tags=["white_check_mark"])
        return

    filters = load_yaml("filters.yaml")
    firms = load_yaml("firms.yaml")
    sources = firms["sources"]
    matcher = Matcher(filters)
    terms = list(dict.fromkeys(filters["search_terms"] + sorted(matcher.years)))

    state = load_state()
    before = json.dumps(state, sort_keys=True)
    known = state.setdefault("fit", {})  # job key -> {"ok", "why", "source"}: class-year checks already done
    if state.get("fit_grad") != matcher.grad:  # graduation year changed: re-check everything
        known.clear()
        state["fit_grad"] = matcher.grad
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    names = {s["name"] for s in sources}
    # Forget postings from sources you removed from firms.yaml
    state["jobs"] = {k: j for k, j in state["jobs"].items() if j["source"] in names}
    state["sources"] = {k: v for k, v in state["sources"].items() if k in names}

    todo = [s for s in sources if not args.only or args.only.lower() in s["name"].lower()]
    new_by_prio = {"high": [], "medium": [], "low": []}
    newly_seeded = []          # (source name, jobs) for sources checked for the first time
    failing_alerts, recoveries = [], []

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda s: fetch(s, terms, matcher, dict(known)), todo))

    for src, found, err in results:
        name = src["name"]
        h = state["sources"].setdefault(name, {"failures": 0, "alerted": False, "seeded": False})
        if err:
            h["failures"] = h.get("failures", 0) + 1
            h["error"] = err
            print(f"FAIL {name}: {err}")
            if h["failures"] >= FAIL_ALERT_AFTER and not h.get("alerted"):
                failing_alerts.append((name, err))
                h["alerted"] = True
            continue

        if h.get("alerted"):
            recoveries.append(name)
        h.update(failures=0, alerted=False, error="")
        current, fetched, seeded_now = set(), set(), []
        for job, prio, fit in found:
            fetched.add(job.key)
            if prio is None:
                continue
            if fit["cache"]:
                known[job.key] = {"ok": fit["ok"], "why": fit["why"], "source": name}
            if not fit["ok"]:
                continue
            current.add(job.key)
            rec = state["jobs"].get(job.key)
            if rec is None:
                rec = {"firm": job.firm, "source": name, "found": now, "closed": None}
                state["jobs"][job.key] = rec
                (seeded_now if not h["seeded"] else new_by_prio[prio]).append(rec)
            elif rec["open"] and RANK[prio] > RANK.get(rec.get("priority"), 0):
                # An already-open posting now matches your filters more strongly (e.g. you added keywords)
                new_by_prio[prio].append(rec)
            rec.update(title=job.title, url=job.url, location=job.location, posted=job.posted,
                       priority=prio, fit=fit["why"], open=True, closed=None)
        for key in [k for k, r in state["jobs"].items() if r["source"] == name and k not in current]:
            if key in fetched:
                del state["jobs"][key]  # still posted, but no longer matches your filters or class year
            elif state["jobs"][key]["open"]:
                state["jobs"][key].update(open=False, closed=now)
        for key in [k for k, v in known.items() if v["source"] == name and k not in fetched]:
            del known[key]  # posting is gone; forget its class-year check
        if not h["seeded"]:
            h["seeded"] = True
            newly_seeded.append((name, seeded_now))
        print(f"ok   {name}: {len(found)} fetched, {len(current)} relevant")

    # Drop postings that closed more than 60 days ago
    cutoff = (now_dt - timedelta(days=60)).isoformat()
    state["jobs"] = {k: j for k, j in state["jobs"].items() if j["open"] or (j["closed"] or now) >= cutoff}

    dash = dashboard_url()
    if args.sync_sheet:
        to_sheet = sorted((j for j in state["jobs"].values() if j["open"] and j["priority"] != "low"),
                          key=lambda j: j["found"])
    else:
        to_sheet = (new_by_prio["high"] + new_by_prio["medium"]
                    + [r for _, rs in newly_seeded for r in rs if r["priority"] != "low"])
    sheet_rows = [{"found": r["found"][:10], "priority": r["priority"], "firm": r["firm"], "title": r["title"],
                   "location": r["location"], "url": r["url"]} for r in to_sheet]

    # --- notifications ---------------------------------------------------------
    if not state["initialized"]:
        open_jobs = [j for j in state["jobs"].values() if j["open"]]
        hi = [j for j in open_jobs if j["priority"] == "high"]
        md = [j for j in open_jobs if j["priority"] == "medium"]
        top = "\n".join(f"• {j['firm']}: {j['title']}" for j in hi[:10])
        push("Internship tracker is live",
             f"{len(hi)} AM/ER/credit internships and {len(md)} related internships are open right now."
             + (f"\n{top}" if top else "") + "\nOpen the dashboard for the full list.",
             priority=4, url=dash, tags=["rocket"], dry_run=args.dry_run)
        state["initialized"] = True
        state["last_heartbeat"] = now
    else:
        if len(newly_seeded) > 3:
            seeded = [r for _, rs in newly_seeded for r in rs]
            hi = [r for r in seeded if r["priority"] == "high"]
            push(f"Now watching {len(newly_seeded)} more firms",
                 ", ".join(n for n, _ in newly_seeded)
                 + f"\n{len(hi)} AM/ER/credit and {len(seeded) - len(hi)} other internships open there now."
                 + "".join(f"\n• {r['firm']}: {r['title']}" for r in hi[:10]),
                 priority=3, url=dash, tags=["eyes"], dry_run=args.dry_run)
        else:
            for name, rs in newly_seeded:
                hi = [r for r in rs if r["priority"] == "high"]
                push(f"Now watching {name}",
                     f"{len(hi)} AM/ER/credit and {len(rs) - len(hi)} other internships currently open there.",
                     priority=3, url=dash, tags=["eyes"], dry_run=args.dry_run)

        hi = new_by_prio["high"]
        if len(hi) <= MAX_SINGLE_PUSHES:
            for r in hi:
                push(f"{r['firm']}: new AM/ER/credit internship", f"{r['title']}\n{r['location']}",
                     priority=5, url=r["url"], tags=["rotating_light"], dry_run=args.dry_run)
        else:
            push(f"{len(hi)} new AM/ER/credit internships",
                 "\n".join(f"• {r['firm']}: {r['title']}" for r in hi[:25]),
                 priority=5, url=dash, tags=["rotating_light"], dry_run=args.dry_run)
        md = new_by_prio["medium"]
        if md:
            push(f"{len(md)} new related internship(s)",
                 "\n".join(f"• {r['firm']}: {r['title']}" for r in md[:25]),
                 priority=3, url=md[0]["url"] if len(md) == 1 else dash, tags=["briefcase"], dry_run=args.dry_run)
        for p in new_by_prio:
            state["week"][p] += len(new_by_prio[p])

    if len(failing_alerts) > 5:
        push(f"{len(failing_alerts)} sources failing",
             "Many sources failed at once, likely a network problem on the runner. "
             + ", ".join(n for n, _ in failing_alerts), priority=3, url=dash, tags=["warning"], dry_run=args.dry_run)
    else:
        for name, err in failing_alerts:
            push(f"Tracker can't check {name}",
                 f"Failed {FAIL_ALERT_AFTER} runs in a row: {err}\nCheck that firm by hand until it recovers.",
                 priority=3, url=dash, tags=["warning"], dry_run=args.dry_run)
    for name in recoveries:
        push(f"{name} is back", "The tracker can check this firm again.", priority=2, tags=["white_check_mark"],
             dry_run=args.dry_run)

    last_hb = state.get("last_heartbeat")
    if last_hb and now_dt - datetime.fromisoformat(last_hb) >= HEARTBEAT_EVERY:
        failing = [n for n, h in state["sources"].items() if h.get("failures")]
        open_hi = sum(1 for j in state["jobs"].values() if j["open"] and j["priority"] == "high")
        w = state["week"]
        push("Weekly tracker check-in",
             f"Watching {len(sources)} sources. This week: {w['high']} new AM/ER/credit, {w['medium']} related, "
             f"{w['low']} low-priority. {open_hi} AM/ER/credit internships open now."
             + (f"\nFailing: {', '.join(failing)}" if failing else "\nAll sources OK."),
             priority=2, url=dash, tags=["calendar"], dry_run=args.dry_run)
        state["last_heartbeat"] = now
        state["week"] = {"high": 0, "medium": 0, "low": 0}

    sheet_append(sheet_rows, dry_run=args.dry_run)

    # --- save ------------------------------------------------------------------
    if args.dry_run:
        print("\n[dry run] nothing written")
        return
    after = json.dumps(state, sort_keys=True)
    if after != before or not DASHBOARD.exists():
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        DASHBOARD.write_text(render(state, sources, firms.get("manual", []), now,
                                    (now_dt - timedelta(days=30)).isoformat()))
        print("state and dashboard updated")
    else:
        print("no changes")


if __name__ == "__main__":
    sys.exit(main())
