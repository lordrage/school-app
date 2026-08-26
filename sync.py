#!/usr/bin/env python3
"""Auto-sync: refresh assignments in school-data.json from Canvas.

Runs inside GitHub Actions on a schedule (see .github/workflows/sync.yml).
Only touches `assignments`, `generated`, and stale `oneThing` — weekly plans,
shifts, courses, and coaching lines stay whatever Claude last wrote.

Env vars: CANVAS_BASE (e.g. https://canvas.dccc.edu), CANVAS_TOKEN (secret).
"""
import json, os, re, urllib.request, datetime, pathlib
from html.parser import HTMLParser
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


# --------------------------------------------------------------------------
# Exams: pull dates from course syllabi (fuzzy prose) plus Canvas items titled
# like an exam (exact due dates). Each exam gets a 1-week "study from" date.
# Syllabus hits are marked unconfirmed — regex on prose is approximate, so the
# app flags them for a human check. Best-effort: if any of this throws, main()
# keeps the assignment sync working and leaves prior exams untouched.
# --------------------------------------------------------------------------
MONTH3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
NAMED_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?", re.I)
NUM_DATE = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?\b")
EXAM_ANCHOR = re.compile(r"\b(midterms?|finals?|exams?|examinations?|tests?)\b", re.I)
NOT_EXAM = re.compile(r"\b(quiz|practice|sample|review|prep|study\s*guide|pre-?test|post-?test)\b", re.I)
CANVAS_EXAM = re.compile(r"\b(midterm|final\s+exam(?:ination)?|exam(?:ination)?)\b", re.I)
STUDY_LEAD_DAYS = 7


class _HTMLText(HTMLParser):
    """Flatten syllabus HTML to text, keeping enough line/cell structure that an
    exam label and its date stay near each other (tables and lists included)."""
    _BLOCK = {"p", "div", "br", "li", "tr", "ul", "ol", "table",
              "section", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK:
            self.buf.append("\n")
        elif tag in ("td", "th"):
            self.buf.append(" — ")

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.buf.append("\n")

    def handle_data(self, data):
        self.buf.append(data)


def html_to_text(body):
    if not body:
        return ""
    p = _HTMLText()
    try:
        p.feed(body)
        txt = "".join(p.buf)
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", body)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n[ \t]*", "\n", txt)
    return re.sub(r"\n{2,}", "\n", txt).strip()


def _resolve_date(mo, day, yr, sem_start, sem_end):
    """Return a datetime.date inside the semester window, or None. With no year,
    pick the year that lands the date in-term (handles 'September 15')."""
    if not (1 <= mo <= 12 and 1 <= day <= 31):
        return None
    lo = sem_start - datetime.timedelta(days=10)
    hi = sem_end + datetime.timedelta(days=14)
    if yr is not None:
        yr = yr + 2000 if yr < 100 else yr
        try:
            d = datetime.date(yr, mo, day)
        except ValueError:
            return None
        return d if lo <= d <= hi else None
    best = None
    for y in (sem_start.year, sem_end.year, sem_start.year + 1, sem_start.year - 1):
        try:
            d = datetime.date(y, mo, day)
        except ValueError:
            continue
        if lo <= d <= hi and (best is None or abs((d - sem_start).days) < abs((best - sem_start).days)):
            best = d
    return best


def _find_dates(region, sem_start, sem_end):
    """Every valid (iso_date, position) in a text region."""
    hits = []
    for m in NAMED_DATE.finditer(region):
        mo = MONTH3.get(m.group(1)[:3].lower())
        if not mo:
            continue
        d = _resolve_date(mo, int(m.group(2)),
                          int(m.group(3)) if m.group(3) else None, sem_start, sem_end)
        if d:
            hits.append((d.isoformat(), m.start()))
    for m in NUM_DATE.finditer(region):
        d = _resolve_date(int(m.group(1)), int(m.group(2)),
                          int(m.group(3)) if m.group(3) else None, sem_start, sem_end)
        if d:
            hits.append((d.isoformat(), m.start()))
    return hits


def _exam_label(text, anchor):
    seg = text[anchor.start(): anchor.end() + 12]
    m = re.match(r"(midterm|final\s+exam(?:ination)?|final|exam(?:ination)?|test)\s*#?\s*(\d+)?",
                 seg, re.I)
    if not m:
        return anchor.group(0).rstrip("sS").title() or "Exam"
    word = re.sub(r"\s+", " ", m.group(1)).title().replace("Examination", "Exam")
    return (word + " " + m.group(2)).strip() if m.group(2) else word


def extract_syllabus_exams(code, text, url, sem_start, sem_end):
    found = {}
    for a in EXAM_ANCHOR.finditer(text):
        aw = a.group(1).lower()
        look = text[a.start(): a.end() + 10].lower()
        # "test" only when numbered (Test 2) — skips "test your understanding"
        if aw.startswith("test") and not re.match(r"\s*#?\s*\d", text[a.end(): a.end() + 6]):
            continue
        # "final" only when it's a real final exam — skips "final draft/project"
        if aw.startswith("final") and not (look.startswith("finals") or "final exam" in look):
            continue
        if NOT_EXAM.search(text[max(0, a.start() - 22): a.end() + 22]):
            continue
        r0 = max(0, a.start() - 45)
        region = text[r0: a.end() + 85]
        dates = _find_dates(region, sem_start, sem_end)
        if not dates:
            continue
        anchor_pos = a.start() - r0
        iso, pos = min(dates, key=lambda dp: abs(dp[1] - anchor_pos))
        if abs(pos - anchor_pos) > 75:
            continue
        label = _exam_label(text, a)
        snippet = re.sub(r"\s+", " ", text[max(0, a.start() - 12): a.end() + 60]).strip()
        prev = found.get(iso)
        if prev is None or len(label) > len(prev["title"]):
            found[iso] = {"title": label, "context": snippet[:120]}
    exams = []
    for iso, e in found.items():
        study = (datetime.date.fromisoformat(iso) - datetime.timedelta(days=STUDY_LEAD_DAYS))
        exams.append({"course": code, "title": e["title"], "date": iso, "hasTime": False,
                      "studyStart": study.isoformat(), "source": "syllabus",
                      "confirmed": False, "url": url, "context": e["context"]})
    return exams


def canvas_exams(assignments, sem_start, sem_end):
    exams = []
    for a in assignments:
        title = (a.get("title") or "").strip()
        if not CANVAS_EXAM.search(title) or NOT_EXAM.search(title):
            continue
        due = a.get("due")
        if not due:
            continue
        try:
            day = datetime.datetime.fromisoformat(due).date()
        except ValueError:
            continue
        if not (sem_start - datetime.timedelta(days=10) <= day <= sem_end + datetime.timedelta(days=21)):
            continue
        study = day - datetime.timedelta(days=STUDY_LEAD_DAYS)
        exams.append({"course": a.get("course"), "title": title, "date": due, "hasTime": True,
                      "studyStart": study.isoformat(), "source": "canvas",
                      "confirmed": True, "url": a.get("url") or "", "context": ""})
    return exams


def manual_exams(items):
    """User-stated exams from data['manualExams'] — they survive every sync and
    override fuzzy syllabus guesses. Shape per entry: {course, title, date, url?}."""
    out = []
    for m in (items or []):
        iso = str(m.get("date") or "").strip()
        try:
            day = datetime.date.fromisoformat(iso[:10])
        except ValueError:
            continue
        out.append({"course": m.get("course") or "?", "title": (m.get("title") or "Exam").strip(),
                    "date": iso, "hasTime": "T" in iso,
                    "studyStart": (day - datetime.timedelta(days=STUDY_LEAD_DAYS)).isoformat(),
                    "source": "manual", "confirmed": True, "url": m.get("url") or "", "context": ""})
    return out


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


def build_exams(courses, assignments, data, today):
    sem = data.get("semester") or {}
    try:
        sem_start = datetime.date.fromisoformat(sem["start"])
        sem_end = datetime.date.fromisoformat(sem["end"])
    except (KeyError, TypeError, ValueError):
        sem_start = today - datetime.timedelta(days=30)
        sem_end = today + datetime.timedelta(days=150)

    syllabi, syl_exams, seen_codes = [], [], set()
    for c in courses:
        code = code_of(c.get("course_code") or c.get("name"))
        if code in seen_codes:
            continue
        seen_codes.add(code)
        text = html_to_text(c.get("syllabus_body") or "")
        url = f"{BASE}/courses/{c.get('id')}/assignments/syllabus"
        syllabi.append({"course": code, "url": url, "hasContent": bool(text)})
        if text:
            syl_exams.extend(extract_syllabus_exams(code, text, url, sem_start, sem_end))

    can = canvas_exams(assignments, sem_start, sem_end)
    manual = manual_exams(data.get("manualExams"))

    def _near(keys, course, iso):
        d0 = datetime.date.fromisoformat(iso[:10])
        return any((course, (d0 + datetime.timedelta(days=off)).isoformat()) in keys
                   for off in (-1, 0, 1))

    canvas_keys = {(e["course"], e["date"][:10]) for e in can}
    firm_keys = canvas_keys | {(e["course"], e["date"][:10]) for e in manual}

    # precedence: Canvas (exact) > manual (user-stated) > syllabus (fuzzy guess)
    merged = list(can)
    merged += [e for e in manual if not _near(canvas_keys, e["course"], e["date"])]
    merged += [e for e in syl_exams if not _near(firm_keys, e["course"], e["date"])]

    cutoff = today - datetime.timedelta(days=1)
    upcoming = []
    for e in merged:
        if datetime.date.fromisoformat(e["date"][:10]) >= cutoff:
            e["id"] = f'{_slug(e["course"])}-{_slug(e["title"])}-{e["date"][:10]}'
            upcoming.append(e)
    upcoming.sort(key=lambda e: (e["date"][:10], e["course"]))
    return upcoming, syllabi


def fetch_courses():
    out = []
    for c in api("/api/v1/courses?enrollment_state=active&include[]=syllabus_body&per_page=50"):
        if c.get("id") in IGNORE_COURSES:
            continue
        out.append(c)
    return out


def main():
    f = ROOT / "school-data.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    data["assignments"] = fetch_assignments()
    now = datetime.datetime.now(ET)
    data["generated"] = now.isoformat(timespec="minutes")

    try:
        exams, syllabi = build_exams(fetch_courses(), data["assignments"], data, now.date())
        data["exams"], data["syllabi"] = exams, syllabi
    except Exception as e:
        print("exam sync skipped:", repr(e))
        data.setdefault("exams", [])
        data.setdefault("syllabi", [])

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
    print(f"assignments: {len(data['assignments'])} | exams: {len(data.get('exams', []))} "
          f"| generated: {data['generated']}")


if __name__ == "__main__":
    main()
