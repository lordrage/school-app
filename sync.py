#!/usr/bin/env python3
"""Auto-sync: refresh assignments in school-data.json from Canvas.

Runs inside GitHub Actions on a schedule (see .github/workflows/sync.yml).
Only touches `assignments`, `generated`, and stale `oneThing` — weekly plans,
shifts, courses, and coaching lines stay whatever Claude last wrote.

Env vars: CANVAS_BASE (e.g. https://canvas.dccc.edu), CANVAS_TOKEN (secret).
"""
import json, os, re, urllib.request, datetime, pathlib
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
BASE = os.environ["CANVAS_BASE"].rstrip("/")
TOKEN = os.environ["CANVAS_TOKEN"]
IGNORE_COURSES = {47763}  # PSY 140 — Fall 2025 leftover that still shows "active"
ALLOWED_TYPES = {"assignment", "quiz", "discussion_topic", "sub_assignment"}
COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,4})\s?-?(\d{3})\b")
ONE_THING_MAX_AGE_H = 36  # stale coaching is worse than none


def api(path):
    out, url = [], BASE + path
    while url:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN})
        with urllib.request.urlopen(req, timeout=30) as r:
            page = json.load(r)
            out.extend(page if isinstance(page, list) else [page])
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
            url = m.group(1) if m else None
    return out


def code_of(context_name):
    m = COURSE_CODE_RE.search(context_name or "")
    return f"{m.group(1)} {m.group(2)}" if m else (context_name or "?")


def to_et(iso):
    d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d.astimezone(ET).isoformat(timespec="minutes")


def fetch_assignments():
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=21)
    items = api(f"/api/v1/planner/items?start_date={start:%Y-%m-%dT%H:%M:%SZ}&per_page=50")
    seen, out = set(), []
    for it in items:
        if it.get("course_id") in IGNORE_COURSES:
            continue
        if it.get("plannable_type") not in ALLOWED_TYPES:
            continue
        p = it.get("plannable") or {}
        due = p.get("due_at") or it.get("plannable_date")
        if not due:
            continue
        pid = f"{it.get('plannable_type')}-{p.get('id')}"
        if pid in seen:
            continue
        seen.add(pid)
        sub = it.get("submissions") or {}
        if not isinstance(sub, dict):
            sub = {}
        status = ("graded" if sub.get("graded")
                  else "submitted" if sub.get("submitted")
                  else "not_started")
        title = p.get("title") or p.get("name") or "?"
        pts = p.get("points_possible")
        url = it.get("html_url") or p.get("html_url") or ""
        if url.startswith("/"):
            url = BASE + url
        big = bool((pts or 0) >= 40 or re.search(r"exam|midterm|final|test", title, re.I))
        out.append({"id": pid, "title": title, "course": code_of(it.get("context_name")),
                    "due": to_et(due), "points": pts, "status": status, "url": url, "big": big})
    out.sort(key=lambda a: a["due"])
    return out


def main():
    f = ROOT / "school-data.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    data["assignments"] = fetch_assignments()
    now = datetime.datetime.now(ET)
    data["generated"] = now.isoformat(timespec="minutes")

    ots = data.get("oneThingTs")
    if data.get("oneThing"):
        try:
            age_h = (now - datetime.datetime.fromisoformat(ots)).total_seconds() / 3600
        except (TypeError, ValueError):
            age_h = ONE_THING_MAX_AGE_H + 1
        if age_h > ONE_THING_MAX_AGE_H:
            data["oneThing"] = ""

    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ix = ROOT / "index.html"
    html = ix.read_text(encoding="utf-8")
    html = re.sub(r'(<script type="application/json" id="data-island">).*?(</script>)',
                  lambda m: m.group(1) + json.dumps(data, ensure_ascii=False) + m.group(2),
                  html, count=1, flags=re.S)
    ix.write_text(html, encoding="utf-8")
    print(f"assignments: {len(data['assignments'])} | generated: {data['generated']}")


if __name__ == "__main__":
    main()
