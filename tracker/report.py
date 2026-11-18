"""Renders JOBS.md, the dashboard you open on GitHub."""

LABEL = {"high": "🎯 AM / ER / credit internships — open now",
         "medium": "Related internships (wealth, other asset-manager roles) — open now",
         "low": "Lower priority (IB, tech, ops, events) — open now"}


def _cell(s):
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def _table(rows, closed=False):
    if not rows:
        return "_None right now._\n"
    head = "| Found | Firm | Role | Location | Who can apply |" + (" Closed |" if closed else "")
    sep = "|---|---|---|---|---|" + ("---|" if closed else "")
    lines = [head, sep]
    for j in rows:
        line = (f"| {j['found'][:10]} | {_cell(j['firm'])} | [{_cell(j['title'])}]({j['url']}) "
                f"| {_cell(j['location'])[:60]} | {_cell(j.get('fit', ''))} |")
        if closed:
            line += f" {j['closed'][:10]} |"
        lines.append(line)
    return "\n".join(lines) + "\n"


def render(state, sources, manual, names, now, cutoff_30d):
    """names (from filters.yaml "dashboard") can override the title and section labels."""
    label = {**LABEL, **{k: v for k, v in names.items() if k in LABEL}}
    jobs = sorted(state["jobs"].values(), key=lambda j: j["found"], reverse=True)
    opened = {p: [j for j in jobs if j["open"] and j["priority"] == p] for p in label}
    closed = [j for j in jobs if not j["open"] and j["priority"] != "low" and (j.get("closed") or "") >= cutoff_30d]
    health = state["sources"]
    failing = [s["name"] for s in sources if health.get(s["name"], {}).get("failures", 0) > 0]

    out = [f"# {names.get('title', 'Internship tracker')}\n",
           f"_Updated {now[:16].replace('T', ' ')} UTC · watching {len(sources)} sources"
           + (f" · ⚠️ {len(failing)} failing: {', '.join(failing)}" if failing else " · all sources OK") + "_\n"]

    out += [f"## {label['high']} ({len(opened['high'])})\n", _table(opened["high"])]
    out += [f"## {label['medium']} ({len(opened['medium'])})\n", _table(opened["medium"])]
    out += [f"<details><summary><b>{label['low']} ({len(opened['low'])})</b></summary>\n\n", _table(opened["low"]), "\n</details>\n"]
    out += [f"<details><summary><b>Closed in the last 30 days ({len(closed)})</b></summary>\n\n",
            _table(closed, closed=True), "\n</details>\n"]

    out += ["## Sources\n", "| Firm | System | Status | Matches open |", "|---|---|---|---|"]
    for s in sources:
        h = health.get(s["name"], {})
        if "failures" not in h:
            status = "not checked yet"
        elif h["failures"]:
            status = f"⚠️ failing ({h['failures']} runs): {_cell(h.get('error', ''))[:80]}"
        else:
            status = "✅"
        n = sum(1 for j in jobs if j["open"] and j["source"] == s["name"])
        out.append(f"| {_cell(s['name'])} | {s['type']} | {status} | {n} |")

    out += ["\n## Check by hand (no public feed)\n",
            "These block automated checks. Join their campus mailing lists so they email you when applications open.\n"]
    for m in manual:
        out.append(f"- [{m['name']}]({m['url']})" + (f" — {m['note']}" if m.get("note") else ""))
    return "\n".join(out) + "\n"
