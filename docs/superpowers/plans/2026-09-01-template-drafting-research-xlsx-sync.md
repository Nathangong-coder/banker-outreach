# Template-Driven Drafting, Background Research, and Excel Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draft emails using the user's own cold-email templates (picked by school/seniority affinity), skip contacts who haven't graduated, add a best-effort Chinese-speaking signal via Gemini's search grounding, and sync discovered contacts into the user's existing `banks.xlsx` tracking workbook without ever clobbering their manual notes.

**Architecture:** Four small new/extended modules plug into the existing `enrich_batch` → `draft_batch` → `push_to_gmail` pipeline: a graduation check runs during enrichment (data already on hand), a research step and template selector run during drafting (only for contacts about to actually be drafted), and a one-way Excel sync runs on demand from the local web UI (never from the cron).

**Tech Stack:** Python 3.12, SQLite, FastAPI/Jinja2 (existing), `openpyxl` (new), `pytest` (new, dev-only), raw `httpx` calls to the Gemini API (existing pattern in `src/llm.py`).

**Spec:** [docs/superpowers/specs/2026-09-01-template-drafting-research-xlsx-sync-design.md](../specs/2026-09-01-template-drafting-research-xlsx-sync-design.md)

## Global Constraints

- Emails are only ever drafted (Gmail `drafts.create`, `gmail.compose` scope) — nothing in this plan adds a send path. `src/gmail_client.py` is not modified.
- `config/banks.csv` remains the sole input for which banks the pipeline searches. `config/banks.xlsx` is a pure output the sync writes to.
- The Chinese-speaking research step only runs when `settings.llm.provider == "google"`; every other provider must short-circuit to a safe empty result, never raise.
- The graduation gate defaults to "graduated" (`True`) whenever education data is missing or unparseable — Apollo search already targets people with real job titles at the bank; only an explicit future end-date flips it to `False`.
- Excel sync never overwrites a cell the user has already filled in by hand, and never touches `Date`, `Conversation`, or `Comments` columns, or any non-bank sheet (`OVERVIEW`, `APPLICATIONS`, `ACTIVE BAY`, `WALL STREET`, `Sheet4`).
- Excel sync is triggered only from the local web UI (`web/app.py`), never from `.github/workflows/daily.yml`.
- Out of scope: `HOMETOWN`, `CLUB`, `HIGH SCHOOL`, `INTERNATIONAL STUDENT` templates — no reliable automated signal exists for these yet.

---

## Task 1: Prerequisite config files, dependencies, and test scaffold

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/__init__.py` (empty, makes `tests` an importable package for `from src import ...` imports to resolve consistently)
- Commit (currently untracked): `config/Cold Email Templates.docx`, `config/banks.xlsx`

**Interfaces:**
- Produces: a working `pytest` command from the repo root; `openpyxl` importable in `.venv`.

- [ ] **Step 1: Add `openpyxl` to production requirements**

Add this line to `requirements.txt` (openpyxl is a runtime dependency — `src/xlsx_sync.py` in Task 10 needs it whenever the web UI's sync button is used, not just in tests):

```
openpyxl>=3.1
```

- [ ] **Step 2: Create `requirements-dev.txt`**

```
-r requirements.txt
pytest>=8.0
```

- [ ] **Step 3: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
```

- [ ] **Step 4: Create empty `tests/__init__.py`**

Empty file — just makes `tests/` a package so test files can sit alongside fixtures cleanly.

- [ ] **Step 5: Install dev dependencies**

```bash
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

- [ ] **Step 6: Verify pytest runs with zero collected tests (no errors)**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: `collected 0 items` — no import errors, no failures.

- [ ] **Step 7: Commit the config files this pipeline now depends on, plus the scaffold**

`config/Cold Email Templates.docx` is read by `src/templates.py` (Task 6), which runs as part of the daily cron's `draft` stage — it must be committed or a fresh GitHub Actions checkout will have nothing to parse. `config/banks.xlsx` is the sync target for Task 10 and should be versioned too so it isn't only on one machine.

```bash
git add "config/Cold Email Templates.docx" config/banks.xlsx requirements.txt requirements-dev.txt pytest.ini tests/__init__.py
git commit -m "chore: add cold email templates, tracking workbook, and test scaffold"
```

---

## Task 2: DB schema migration for research and template columns

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db_migration.py`

**Interfaces:**
- Produces: `contacts` rows gain nullable columns `graduated INTEGER`, `chinese_speaking INTEGER`, `research_notes TEXT`, `template_used TEXT`. `db.init(path=None)` (existing signature, unchanged) now also runs the migration.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_migration.py`:

```python
import sqlite3

from src import db

NEW_COLUMNS = {"graduated", "chinese_speaking", "research_notes", "template_used"}


def test_init_adds_new_columns_to_fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init()
    with db.session() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
    assert NEW_COLUMNS <= cols


def test_init_migrates_old_schema_missing_new_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE contacts (
            id INTEGER PRIMARY KEY, apollo_id TEXT UNIQUE, first_name TEXT,
            last_name TEXT, title TEXT, seniority TEXT, bank_id INTEGER,
            linkedin_url TEXT, city TEXT, email TEXT, email_status TEXT,
            education TEXT, affinity_score INTEGER DEFAULT 0, affinity_notes TEXT,
            status TEXT DEFAULT 'new', hook TEXT, found_at TEXT, enriched_at TEXT)"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init()
    with db.session() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
    assert NEW_COLUMNS <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db_migration.py -v`
Expected: FAIL — `NEW_COLUMNS <= cols` is false, the columns don't exist yet.

- [ ] **Step 3: Add the columns to `SCHEMA` and add a migration function**

In `src/db.py`, extend the `contacts` table definition inside `SCHEMA` (so a brand-new DB gets the columns at `CREATE TABLE` time) — add these four lines right before the closing `UNIQUE(first_name, last_name, bank_id)` line of the `contacts` table:

```python
    graduated       INTEGER,
    chinese_speaking INTEGER,
    research_notes  TEXT,
    template_used   TEXT,
```

Then add a migration function and call it from `init()`:

```python
_NEW_CONTACT_COLUMNS = {
    "graduated": "INTEGER",
    "chinese_speaking": "INTEGER",
    "research_notes": "TEXT",
    "template_used": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """ALTER TABLE for columns added after a DB already exists.

    CREATE TABLE IF NOT EXISTS in SCHEMA only helps brand-new databases;
    an existing contacts table needs its missing columns added by hand.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(contacts)")}
    for col, sqltype in _NEW_CONTACT_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {sqltype}")


def init(path: Path | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db_migration.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db_migration.py
git commit -m "feat: add research/template columns to contacts, with migration for existing DBs"
```

---

## Task 3: Graduation gate (`matching.is_graduated`)

**Files:**
- Modify: `src/matching.py`
- Test: `tests/test_matching.py` (new file)

**Interfaces:**
- Consumes: `education: list[dict] | None` — same shape already produced by `Apollo._normalize` and stored via `db.set_enrichment` (each entry has `school`, `degree`, `start`, `end`, all optional strings).
- Produces: `is_graduated(education: list[dict] | None) -> bool`, used by Task 9 (`pipeline.enrich_batch`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_matching.py`:

```python
from datetime import date

from src.matching import is_graduated, score_contact


def test_is_graduated_true_when_no_education():
    assert is_graduated([]) is True
    assert is_graduated(None) is True


def test_is_graduated_true_when_end_date_in_past():
    education = [{"school": "UCLA", "degree": "BA", "start": "2018", "end": "2022"}]
    assert is_graduated(education) is True


def test_is_graduated_true_when_end_date_missing():
    education = [{"school": "UCLA", "degree": "BA", "start": "2022", "end": None}]
    assert is_graduated(education) is True


def test_is_graduated_false_when_end_date_in_future():
    future_year = date.today().year + 1
    education = [{"school": "UCLA", "degree": "MBA", "start": "2023", "end": str(future_year)}]
    assert is_graduated(education) is False


def test_is_graduated_uses_most_recent_entry_by_end_date():
    future_year = date.today().year + 1
    education = [
        {"school": "High School", "start": "2016", "end": "2020"},
        {"school": "UCLA", "start": "2020", "end": str(future_year)},
    ]
    assert is_graduated(education) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_matching.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_graduated'`

- [ ] **Step 3: Implement `is_graduated` in `src/matching.py`**

Add this import at the top of `src/matching.py` (alongside the existing `import re` / `import unicodedata`):

```python
from datetime import date
```

Add this function anywhere below `_norm`:

```python
def _entry_year(value: str | None) -> int | None:
    if not value:
        return None
    for token in re.split(r"[-/]", str(value)):
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None


def is_graduated(education: list[dict] | None) -> bool:
    """True unless the most recent education entry clearly hasn't ended yet.

    Missing or unparseable dates default to True: Apollo search already
    targets people holding real job titles at the bank, so incomplete
    data is a reason to trust it, not a reason to withhold outreach.
    """
    if not education:
        return True

    def sort_key(entry: dict) -> tuple[int, int]:
        return (_entry_year(entry.get("end")) or 0, _entry_year(entry.get("start")) or 0)

    latest = max(education, key=sort_key)
    end_year = _entry_year(latest.get("end"))
    if end_year is None:
        return True
    return end_year <= date.today().year
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_matching.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/matching.py tests/test_matching.py
git commit -m "feat: add is_graduated gate based on existing education data"
```

---

## Task 4: USC affinity rule

**Files:**
- Modify: `config/affinity.yml`
- Modify: `tests/test_matching.py`

**Interfaces:**
- Produces: `score_contact()` now returns a reason containing `"USC"` for a USC-educated contact — consumed by Task 6's `select_template`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_matching.py`:

```python
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_score_contact_matches_usc():
    rules = yaml.safe_load((REPO_ROOT / "config" / "affinity.yml").read_text())
    person = {"education": [{"school": "University of Southern California", "degree": "BA"}]}
    score, reasons = score_contact(person, rules)
    assert score > 0
    assert any("USC" in r for r in reasons)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_matching.py::test_score_contact_matches_usc -v`
Expected: FAIL — `score == 0`, no rule matches "University of Southern California".

- [ ] **Step 3: Add the USC rule**

In `config/affinity.yml`, add this block after the `UC system` rule and before the `Southern California` keyword rule:

```yaml
  - label: "USC"
    weight: 2
    schools: ["University of Southern California", "USC"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_matching.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add config/affinity.yml tests/test_matching.py
git commit -m "feat: add USC affinity rule"
```

---

## Task 5: LLM client — refactor retry loop, add Google Search grounding

**Files:**
- Modify: `src/llm.py`
- Test: `tests/test_llm.py` (new file)

**Interfaces:**
- Produces: `LLM.search_complete(system: str, user: str, max_tokens: int = 800, attempts: int = 4) -> str` — Google-only, raises `ValueError` immediately (no HTTP call) for any other provider. `LLM.complete(...)` keeps its existing signature and behavior unchanged.
- Consumed by: Task 7 (`src/research.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm.py`:

```python
from unittest.mock import MagicMock

import pytest

from src.llm import LLM


def _mock_response(status_code, json_data=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.headers = headers or {}
    return resp


def test_complete_still_works_after_refactor():
    llm = LLM(provider="google", model="gemini-3.6-flash", keys=["fake-key"])
    llm.client.post = MagicMock(return_value=_mock_response(
        200, {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    ))
    assert llm.complete("system", "user") == "hello"
    llm.close()


def test_search_complete_sends_google_search_tool_and_extracts_text():
    llm = LLM(provider="google", model="gemini-3.6-flash", keys=["fake-key"])
    llm.client.post = MagicMock(return_value=_mock_response(
        200, {"candidates": [{"content": {"parts": [{"text": "grounded answer"}]}}]}
    ))
    result = llm.search_complete("system", "user")
    assert result == "grounded answer"
    sent_payload = llm.client.post.call_args.kwargs["json"]
    assert sent_payload["tools"] == [{"google_search": {}}]
    llm.close()


def test_search_complete_rejects_non_google_provider_without_http_call():
    llm = LLM(provider="anthropic", model="claude-sonnet-4-6", keys=["fake-key"])
    llm.client.post = MagicMock()
    with pytest.raises(ValueError):
        llm.search_complete("system", "user")
    llm.client.post.assert_not_called()
    llm.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: FAIL — `search_complete` doesn't exist yet (`AttributeError`).

- [ ] **Step 3: Refactor `complete()` into a shared `_send` helper, then add `search_complete`**

In `src/llm.py`, replace the existing `complete` method body with a refactored version that extracts the retry loop into `_send`, taking a small builder callback so both plain completion and search-grounded completion share identical retry/error handling:

```python
    def complete(self, system: str, user: str, max_tokens: int = 1200,
                 attempts: int = 4) -> str:
        return self._send(
            lambda key: self._build(key, system, user, max_tokens), attempts
        )

    def search_complete(self, system: str, user: str, max_tokens: int = 800,
                         attempts: int = 4) -> str:
        """Google-only: grounds the response in live Google Search results.

        Gemini's search grounding is a single server-side HTTP call (the
        API performs the search itself and returns the final answer) —
        no client-side tool-call loop needed, so it fits this class's
        existing minimal-HTTP style.
        """
        if self.provider != "google":
            raise ValueError(
                f"search_complete is only supported for provider 'google', got {self.provider!r}"
            )
        return self._send(
            lambda key: self._build_search(key, system, user, max_tokens), attempts
        )

    def _send(self, builder, attempts: int = 4) -> str:
        last_err: Exception | None = None
        for attempt in range(attempts):
            state = self.pool.acquire()
            url, headers, payload = builder(state.key)
            try:
                r = self.client.post(url, headers=headers, json=payload)
            except httpx.RequestError as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:
                self.pool.penalize(state, float(r.headers.get("retry-after", 30)))
                continue
            if r.status_code in (401, 403):
                self.pool.retire(state, f"HTTP {r.status_code}")
                continue
            if r.status_code >= 500:
                last_err = RuntimeError(f"{self.provider} {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(
                    f"{self.provider} {r.status_code}: {r.text[:300]}"
                )
            return self._extract(r.json())

        raise AllKeysExhausted(f"{self.provider} failed after {attempts}: {last_err}")

    def _build_search(self, key: str, system: str, user: str, max_tokens: int):
        return (
            f"{self.base}/models/{self.model}:generateContent?key={key}",
            {"content-type": "application/json"},
            {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": user}]}],
             "tools": [{"google_search": {}}],
             "generationConfig": {"maxOutputTokens": max_tokens}},
        )
```

This removes the old `complete` method's inline retry loop entirely — `complete` now just calls `_send` with a builder closure over `_build` (the existing per-provider payload method, unchanged).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/llm.py tests/test_llm.py
git commit -m "feat: add Gemini search-grounded completion via LLM.search_complete"
```

---

## Task 6: Template parsing and selection (`src/templates.py`)

**Files:**
- Create: `src/templates.py`
- Test: `tests/test_templates.py`

**Interfaces:**
- Consumes: `config/Cold Email Templates.docx` (committed in Task 1).
- Produces: `TALKING_POINTS: dict[str, str]` (module-level, built at import time) and `select_template(affinity_notes: list[str] | None, seniority: str | None, title: str | None, has_education: bool) -> tuple[str, str]` returning `(category, talking_point)`. Consumed by Task 9 (`pipeline.draft_batch`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_templates.py`:

```python
from src.templates import IN_SCOPE, TALKING_POINTS, select_template


def test_all_in_scope_categories_have_talking_points():
    for category in IN_SCOPE:
        assert TALKING_POINTS[category]


def test_select_template_prioritizes_anderson_over_plain_ucla():
    category, _ = select_template(
        ["UCLA — Economics", "UCLA Anderson — MBA"], "vp", "Vice President", True
    )
    assert category == "ANDERSON ALUM"


def test_select_template_matches_plain_ucla():
    category, _ = select_template(["UCLA — Economics"], "associate", "Associate", True)
    assert category == "UCLA ALUM"


def test_select_template_matches_usc():
    category, _ = select_template(["USC — MBA"], "manager", "Associate", True)
    assert category == "USC ALUM"


def test_select_template_matches_uc_system():
    category, _ = select_template(["UC system — Economics"], "senior", "Analyst", True)
    assert category == "UNIVERSITY OF CALIFORNIA ALUM"


def test_select_template_falls_back_to_seniority_when_no_school_match():
    category, _ = select_template([], "managing director", "Managing Director", True)
    assert category == "VICE PRESIDENTS, MANAGING DIRECTORS, AND ABOVE"


def test_select_template_non_target_school_when_educated_but_no_match():
    category, _ = select_template([], "associate", "Associate", True)
    assert category == "NON TARGET SCHOOL"


def test_select_template_standard_when_no_education_at_all():
    category, _ = select_template([], "associate", "Associate", False)
    assert category == "STANDARD"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_templates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.templates'`

- [ ] **Step 3: Implement `src/templates.py`**

```python
"""Parses config/Cold Email Templates.docx into per-category talking points
and picks the right one for a contact.

Templates are used as structure, not literal copy: drafting.py keeps its
own voice and rules, and only borrows the one distinguishing "angle" each
category's template uses (e.g. Anderson's is "you also went to UCLA,
graduating from Anderson").
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DOCX_PATH = ROOT / "config" / "Cold Email Templates.docx"

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Category headers in document order. Everything from "FOLLOW UP TEMPLATES"
# onward is for manual replies, not the initial draft, and is not parsed.
ALL_CATEGORIES = [
    "STANDARD", "UCLA ALUM", "ANDERSON ALUM", "CLUB ALUM",
    "UNIVERSITY OF CALIFORNIA ALUM", "LA UNIVERSITY ALUM", "USC ALUM",
    "HOMETOWN", "HOMETOWN STUDY / WORK ABROAD", "HIGH SCHOOL",
    "NON TARGET SCHOOL", "INTERNATIONAL STUDENT",
    "VICE PRESIDENTS, MANAGING DIRECTORS, AND ABOVE",
]
STOP_MARKER = "FOLLOW UP TEMPLATES"

# Categories this build actually selects between. The rest need signals
# (hometown, club, high school, study-abroad location) not collected yet.
IN_SCOPE = [
    "STANDARD", "UCLA ALUM", "ANDERSON ALUM", "UNIVERSITY OF CALIFORNIA ALUM",
    "USC ALUM", "NON TARGET SCHOOL",
    "VICE PRESIDENTS, MANAGING DIRECTORS, AND ABOVE",
]

_FALLBACK_TALKING_POINTS = {
    "STANDARD": "no shared background found; lead with the group or coverage area",
    "UCLA ALUM": "you also went to UCLA",
    "ANDERSON ALUM": "you also went to UCLA, graduating from Anderson",
    "UNIVERSITY OF CALIFORNIA ALUM": "you also went to a UC school",
    "USC ALUM": "you attended USC, given the close relationship between our schools",
    "NON TARGET SCHOOL": "you came from a non-target school, same as me",
    "VICE PRESIDENTS, MANAGING DIRECTORS, AND ABOVE": (
        "your seniority gives you a longer-term view on the industry"
    ),
}

_BOILERPLATE_PREFIXES = (
    "Hi ", "Sincerely", "Thank you so much", "I know that as an",
)


def _paragraphs(docx_path: Path) -> list[str]:
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    out = []
    for p in root.iter(f"{_W_NS}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_W_NS}t"))
        out.append(text.strip())
    return out


def _extract_talking_points(docx_path: Path = DOCX_PATH) -> dict[str, str]:
    if not docx_path.exists():
        log.warning("%s not found; using fallback talking points", docx_path)
        return dict(_FALLBACK_TALKING_POINTS)

    try:
        paragraphs = [p for p in _paragraphs(docx_path) if p]
    except Exception:
        log.warning("Could not parse %s; using fallback talking points", docx_path, exc_info=True)
        return dict(_FALLBACK_TALKING_POINTS)

    known_headers = set(ALL_CATEGORIES) | {STOP_MARKER}
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in paragraphs:
        if line == STOP_MARKER:
            break
        if line in known_headers:
            current = line
            blocks[current] = []
            continue
        if current:
            blocks[current].append(line)

    points = dict(_FALLBACK_TALKING_POINTS)
    for category in IN_SCOPE:
        body_lines = blocks.get(category) or []
        candidates = [
            ln for ln in body_lines[1:]  # skip the subject-line paragraph
            if ln
            and "I hope this email finds you well" not in ln
            and not ln.startswith(_BOILERPLATE_PREFIXES)
        ]
        if candidates:
            points[category] = max(candidates, key=len)
    return points


TALKING_POINTS: dict[str, str] = _extract_talking_points()

_SENIOR_PATTERN = re.compile(
    r"\b(vice president|vp|managing director|\bmd\b|director)\b", re.IGNORECASE
)


def _matches(affinity_notes: list[str], substring: str) -> bool:
    haystack = " | ".join(affinity_notes).lower()
    return substring.lower() in haystack


def select_template(affinity_notes: list[str] | None, seniority: str | None,
                     title: str | None, has_education: bool) -> tuple[str, str]:
    """Priority: Anderson > UCLA > USC > UC system > VP/MD+ > non-target > standard."""
    notes = affinity_notes or []
    if _matches(notes, "Anderson"):
        category = "ANDERSON ALUM"
    elif _matches(notes, "UCLA"):
        category = "UCLA ALUM"
    elif _matches(notes, "USC"):
        category = "USC ALUM"
    elif _matches(notes, "UC system"):
        category = "UNIVERSITY OF CALIFORNIA ALUM"
    elif _SENIOR_PATTERN.search(f"{seniority or ''} {title or ''}"):
        category = "VICE PRESIDENTS, MANAGING DIRECTORS, AND ABOVE"
    elif has_education:
        category = "NON TARGET SCHOOL"
    else:
        category = "STANDARD"
    return category, TALKING_POINTS.get(category, TALKING_POINTS["STANDARD"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_templates.py -v`
Expected: PASS (7 tests). If `test_all_in_scope_categories_have_talking_points` fails because a category's extracted point equals its fallback unexpectedly, print `TALKING_POINTS` and check the docx's paragraph structure for that category against `_BOILERPLATE_PREFIXES` — the docx text in this repo is known to work with this exact filter as of the design spec's exploration.

- [ ] **Step 5: Commit**

```bash
git add src/templates.py tests/test_templates.py
git commit -m "feat: parse cold email templates and select one per contact"
```

---

## Task 7: Chinese-speaking research step (`src/research.py`)

**Files:**
- Create: `src/research.py`
- Test: `tests/test_research.py`

**Interfaces:**
- Consumes: `LLM.search_complete` (Task 5), `settings.llm.provider` (`src/config.py`, unchanged).
- Produces: `chinese_speaking_signal(llm: LLM, contact: dict) -> dict` returning `{"chinese_speaking": bool | None, "confidence": str, "notes": str}`. Consumed by Task 9 (`pipeline.draft_batch`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_research.py`:

```python
from unittest.mock import MagicMock

from src import research
from src.config import settings

EMPTY = {"chinese_speaking": None, "confidence": "low", "notes": ""}


def test_skips_when_provider_not_google(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "anthropic")
    llm = MagicMock()
    result = research.chinese_speaking_signal(llm, {"id": 1, "first_name": "A", "last_name": "B"})
    assert result == EMPTY
    llm.search_complete.assert_not_called()


def test_parses_valid_json_response(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "google")
    llm = MagicMock()
    llm.search_complete.return_value = (
        '{"chinese_speaking": true, "confidence": "medium", "notes": "posts in Mandarin"}'
    )
    result = research.chinese_speaking_signal(
        llm, {"id": 1, "first_name": "A", "last_name": "B", "bank_name": "GS"}
    )
    assert result == {"chinese_speaking": True, "confidence": "medium", "notes": "posts in Mandarin"}


def test_parses_fenced_json_response(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "google")
    llm = MagicMock()
    llm.search_complete.return_value = (
        '```json\n{"chinese_speaking": null, "confidence": "low", "notes": ""}\n```'
    )
    result = research.chinese_speaking_signal(llm, {"id": 1})
    assert result["chinese_speaking"] is None


def test_degrades_gracefully_on_malformed_response(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "google")
    llm = MagicMock()
    llm.search_complete.return_value = "not json at all"
    result = research.chinese_speaking_signal(llm, {"id": 1})
    assert result == EMPTY


def test_degrades_gracefully_when_search_raises(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "google")
    llm = MagicMock()
    llm.search_complete.side_effect = RuntimeError("boom")
    result = research.chinese_speaking_signal(llm, {"id": 1})
    assert result == EMPTY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_research.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.research'`

- [ ] **Step 3: Implement `src/research.py`**

```python
"""Best-effort inference of a Chinese-speaking signal from public web
search, using Gemini's search grounding. Never asserted as fact — always
surfaced as "(inferred)" by the caller.
"""
from __future__ import annotations

import json
import logging
import re

from .config import settings
from .llm import LLM

log = logging.getLogger(__name__)

_EMPTY = {"chinese_speaking": None, "confidence": "low", "notes": ""}

SYSTEM = """You research a single person from public information only, using \
web search. You are given their name, job title, employer, city, and \
LinkedIn URL if known.

Judge whether there is a real public signal that this person speaks \
Chinese: a bio or post in Chinese, education at a university in mainland \
China, Hong Kong, or Taiwan, a LinkedIn language listed as Chinese, or \
similar concrete evidence. A Chinese-sounding name alone is NOT enough \
evidence on its own.

Return ONLY a JSON object, no markdown fence:
{"chinese_speaking": true|false|null, "confidence": "low"|"medium"|"high", \
"notes": "one short phrase citing what you found, or empty string"}

Use null when evidence is thin or you found nothing specific — do not guess."""


def chinese_speaking_signal(llm: LLM, contact: dict) -> dict:
    if settings.llm.provider != "google":
        return dict(_EMPTY)

    lines = [
        f"Name: {contact.get('first_name')} {contact.get('last_name')}",
        f"Title: {contact.get('title')}",
        f"Employer: {contact.get('bank_name')}",
    ]
    if contact.get("city"):
        lines.append(f"City: {contact['city']}")
    if contact.get("linkedin_url"):
        lines.append(f"LinkedIn: {contact['linkedin_url']}")
    user = "\n".join(lines)

    try:
        raw = llm.search_complete(SYSTEM, user, max_tokens=400)
    except Exception:
        log.warning("Chinese-speaking research failed for contact %s", contact.get("id"), exc_info=True)
        return dict(_EMPTY)

    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return dict(_EMPTY)
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError:
            return dict(_EMPTY)

    return {
        "chinese_speaking": result.get("chinese_speaking"),
        "confidence": result.get("confidence") or "low",
        "notes": (result.get("notes") or "").strip(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_research.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/research.py tests/test_research.py
git commit -m "feat: add best-effort Chinese-speaking research via Gemini search grounding"
```

---

## Task 8: Drafting prompt integration

**Files:**
- Modify: `src/drafting.py`
- Test: `tests/test_drafting.py` (new file)

**Interfaces:**
- Consumes: `talking_point: str | None` (from Task 6's `select_template`), `contact["research_notes"]` (populated by Task 9 before calling this).
- Produces: `build_user_prompt(contact, sender_name, sender_blurb, talking_point=None) -> str` (new optional 4th parameter) and `draft_for(llm, contact, talking_point=None) -> tuple[str, str]` (new optional 3rd parameter). Both existing call sites without the new argument keep working unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_drafting.py`:

```python
from src.drafting import build_user_prompt


def test_build_user_prompt_includes_angle_to_use_when_given():
    contact = {"first_name": "Jane", "last_name": "Doe", "title": "VP", "bank_name": "Goldman Sachs"}
    prompt = build_user_prompt(
        contact, "Nathan", "blurb", talking_point="you also went to UCLA, graduating from Anderson"
    )
    assert "Angle to use: you also went to UCLA, graduating from Anderson" in prompt


def test_build_user_prompt_omits_angle_to_use_when_none():
    contact = {"first_name": "Jane", "last_name": "Doe", "title": "VP", "bank_name": "GS"}
    prompt = build_user_prompt(contact, "Nathan", "blurb")
    assert "Angle to use:" not in prompt


def test_build_user_prompt_appends_research_notes_to_shared_background():
    contact = {
        "first_name": "Jane", "last_name": "Doe", "title": "VP", "bank_name": "GS",
        "affinity_notes": ["UCLA — Economics"],
        "research_notes": "Chinese-speaking (inferred, medium confidence)",
    }
    prompt = build_user_prompt(contact, "Nathan", "blurb")
    line = next(l for l in prompt.splitlines() if l.startswith("Shared background:"))
    assert "UCLA — Economics" in line
    assert "Chinese-speaking (inferred, medium confidence)" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_drafting.py -v`
Expected: FAIL — `TypeError: build_user_prompt() got an unexpected keyword argument 'talking_point'`

- [ ] **Step 3: Update `src/drafting.py`**

Replace `build_user_prompt` and `draft_for` with:

```python
def build_user_prompt(contact: dict, sender_name: str, sender_blurb: str,
                       talking_point: str | None = None) -> str:
    reasons = contact.get("affinity_notes") or []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except json.JSONDecodeError:
            reasons = [reasons]
    if contact.get("research_notes"):
        reasons = [*reasons, contact["research_notes"]]

    lines = [
        f"Recipient: {contact.get('first_name')} {contact.get('last_name')}",
        f"Title: {contact.get('title')}",
        f"Bank: {contact.get('bank_name')}",
    ]
    if contact.get("category"):
        lines.append(f"Bank type: {contact['category']}")
    if contact.get("city"):
        lines.append(f"Based in: {contact['city']}")
    lines.append(f"Shared background: {summarize(reasons)}")
    if talking_point:
        lines.append(f"Angle to use: {talking_point}")
    if contact.get("hook"):
        lines.append(f"Recent context worth referencing: {contact['hook']}")
    else:
        lines.append(
            "Recent context: none available. Do not fabricate a deal, "
            "promotion, or news item."
        )
    lines.append("")
    lines.append(f"Sender: {sender_name}")
    lines.append(f"About the sender: {sender_blurb}")
    return "\n".join(lines)


def draft_for(llm, contact: dict, talking_point: str | None = None) -> tuple[str, str]:
    user = build_user_prompt(contact, settings.sender_name, settings.sender_blurb, talking_point)
    result = llm.complete_json(SYSTEM, user, max_tokens=900)
    subject = (result.get("subject") or "").strip()
    body = (result.get("body") or "").strip()
    if not subject or not body:
        raise ValueError(f"Incomplete draft returned: {result}")
    return subject, body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_drafting.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/drafting.py tests/test_drafting.py
git commit -m "feat: thread template angle and research notes into the drafting prompt"
```

---

## Task 9: Pipeline integration

**Files:**
- Modify: `src/db.py` (extend `set_enrichment`, add `set_research`, extend `ready_to_draft`)
- Modify: `src/pipeline.py` (`enrich_batch`, `draft_batch`)
- Test: `tests/test_pipeline.py` (new file)

**Interfaces:**
- Consumes: `matching.is_graduated` (Task 3), `templates.select_template` (Task 6), `research.chinese_speaking_signal` (Task 7), `drafting.draft_for` (Task 8, now taking `talking_point`).
- Produces: `db.set_enrichment(conn, contact_id, email, email_status, education, score, notes, graduated: bool)` (signature extended), `db.set_research(conn, contact_id, chinese_speaking, research_notes, template_used)` (new), `db.ready_to_draft` (excludes `graduated = 0`).

- [ ] **Step 1: Extend `db.set_enrichment` and add `db.set_research` in `src/db.py`**

Replace the existing `set_enrichment` function:

```python
def set_enrichment(conn, contact_id: int, email: str | None, email_status: str | None,
                   education: list | None, score: int, notes: list[str],
                   graduated: bool) -> None:
    conn.execute(
        """UPDATE contacts SET email=?, email_status=?, education=?,
             affinity_score=?, affinity_notes=?, enriched_at=?, graduated=?,
             status = CASE WHEN ? IS NULL THEN 'no_email' ELSE 'enriched' END
           WHERE id = ?""",
        (email, email_status, json.dumps(education or []), score,
         json.dumps(notes), now(), int(graduated), email, contact_id),
    )


def set_research(conn, contact_id: int, chinese_speaking: bool | None,
                 research_notes: str, template_used: str) -> None:
    conn.execute(
        """UPDATE contacts SET chinese_speaking=?, research_notes=?, template_used=?
           WHERE id=?""",
        (chinese_speaking, research_notes, template_used, contact_id),
    )
```

Update `ready_to_draft` to skip contacts flagged as not graduated (unknown/NULL still passes, per the trust-Apollo-by-default policy):

```python
def ready_to_draft(conn, limit: int, min_affinity: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.*, b.name AS bank_name, b.category
           FROM contacts c JOIN banks b ON b.id = c.bank_id
           WHERE c.status = 'enriched' AND c.email IS NOT NULL
             AND c.affinity_score >= ?
             AND (c.graduated IS NULL OR c.graduated = 1)
           ORDER BY c.affinity_score DESC, b.priority ASC LIMIT ?""",
        (min_affinity, limit),
    ).fetchall()
```

- [ ] **Step 2: Update `enrich_batch` in `src/pipeline.py`**

Add `is_graduated` to the `matching` import:

```python
from .matching import is_graduated, score_contact
```

In `enrich_batch`, update the loop body where `score_contact` is called:

```python
            education = m.get("education") or []
            score, reasons = score_contact(
                {**p, "education": education}, rules
            )
            graduated = is_graduated(education)
            db.set_enrichment(conn, p["id"], email, m.get("email_status"),
                              education, score, reasons, graduated)
```

(This replaces the existing two lines that call `score_contact` and `db.set_enrichment` — everything else in `enrich_batch` is unchanged.)

- [ ] **Step 3: Update `draft_batch` in `src/pipeline.py`**

Add these imports at the top of `src/pipeline.py`:

```python
from .research import chinese_speaking_signal
from .templates import select_template
```

Replace `draft_batch` entirely:

```python
def draft_batch(limit: int | None = None) -> dict:
    """Write emails for the top-scoring, graduated contacts with a verified address."""
    limit = limit or settings.drafts_per_day
    stats = {"drafted": 0, "errors": []}
    with db.session() as conn:
        targets = [dict(r) for r in db.ready_to_draft(conn, limit, settings.min_affinity)]
    if not targets:
        return stats

    with LLM() as llm:
        for contact in targets:
            try:
                affinity_notes = contact.get("affinity_notes") or []
                if isinstance(affinity_notes, str):
                    affinity_notes = json.loads(affinity_notes) if affinity_notes else []
                education = json.loads(contact.get("education") or "[]")

                research = chinese_speaking_signal(llm, contact)
                research_notes = ""
                if research.get("chinese_speaking"):
                    conf = research.get("confidence", "low")
                    research_notes = f"Chinese-speaking (inferred, {conf} confidence)"
                contact["research_notes"] = research_notes

                category, talking_point = select_template(
                    affinity_notes, contact.get("seniority"), contact.get("title"),
                    bool(education),
                )
                subject, body = draft_for(llm, contact, talking_point)
            except Exception as e:
                stats["errors"].append(f"{contact['first_name']} {contact['last_name']}: {e}")
                log.error("Draft failed: %s", e)
                continue
            with db.session() as conn:
                db.save_draft(conn, contact["id"], subject, body,
                              llm.provider, llm.model)
                db.set_research(conn, contact["id"], research.get("chinese_speaking"),
                                research_notes, category)
            stats["drafted"] += 1
    return stats
```

- [ ] **Step 4: Write the test**

Create `tests/test_pipeline.py`:

```python
from unittest.mock import patch

from src import db, pipeline


def _seed(conn):
    bank_id = db.upsert_bank(conn, "Goldman Sachs", "gs.com", "BB", 1)
    conn.execute(
        """INSERT INTO contacts (apollo_id, first_name, last_name, title, seniority,
             bank_id, email, email_status, education, affinity_score, affinity_notes,
             status, graduated, found_at, enriched_at)
           VALUES ('a1','Jane','Doe','VP','vp',?, 'jane@gs.com','verified',
             '[{"school":"UCLA Anderson","degree":"MBA"}]', 5,
             '["UCLA Anderson — MBA"]', 'enriched', 1, 'x', 'x')""",
        (bank_id,),
    )


def test_draft_batch_persists_template_and_research(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init()
    with db.session() as conn:
        _seed(conn)

    with patch("src.pipeline.chinese_speaking_signal",
               return_value={"chinese_speaking": True, "confidence": "high", "notes": "x"}), \
         patch("src.pipeline.select_template",
               return_value=("ANDERSON ALUM", "you also went to UCLA, graduating from Anderson")), \
         patch("src.pipeline.draft_for", return_value=("subject", "body")), \
         patch("src.pipeline.LLM") as mock_llm_cls:
        mock_ctx = mock_llm_cls.return_value.__enter__.return_value
        mock_ctx.provider = "google"
        mock_ctx.model = "gemini-3.6-flash"
        stats = pipeline.draft_batch(limit=5)

    assert stats["drafted"] == 1
    assert stats["errors"] == []
    with db.session() as conn:
        row = dict(conn.execute("SELECT * FROM contacts WHERE apollo_id='a1'").fetchone())
    assert row["template_used"] == "ANDERSON ALUM"
    assert row["chinese_speaking"] == 1
    assert row["research_notes"] == "Chinese-speaking (inferred, high confidence)"
    assert row["status"] == "drafted"


def test_ready_to_draft_excludes_not_graduated(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init()
    with db.session() as conn:
        bank_id = db.upsert_bank(conn, "Goldman Sachs", "gs.com", "BB", 1)
        conn.execute(
            """INSERT INTO contacts (apollo_id, first_name, last_name, bank_id,
                 email, affinity_score, status, graduated, found_at)
               VALUES ('a2','Still','Studying',?, 'still@gs.com', 5, 'enriched', 0, 'x')""",
            (bank_id,),
        )
        rows = db.ready_to_draft(conn, 10, 1)
    assert rows == []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: PASS (2 tests). Also re-run the full suite to confirm nothing else broke: `.venv/Scripts/python.exe -m pytest -v`

- [ ] **Step 6: Commit**

```bash
git add src/db.py src/pipeline.py tests/test_pipeline.py
git commit -m "feat: gate drafting on graduation, run research and template selection per contact"
```

---

## Task 10: Excel sync (`src/xlsx_sync.py`)

**Files:**
- Create: `src/xlsx_sync.py`
- Test: `tests/test_xlsx_sync.py`

**Interfaces:**
- Consumes: `db.session()` (existing), `matching.summarize` (existing), the module-level `BANK_SHEET_ALIASES` this task defines.
- Produces: `sync_to_workbook(path: Path | None = None) -> dict` returning `{"updated": int, "created_rows": int, "new_sheets": list[str]}`. Consumed by Task 11 (`web/app.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_xlsx_sync.py`:

```python
import openpyxl

from src import db, xlsx_sync


def _make_fixture_workbook(path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("GS")
    ws.append(xlsx_sync.HEADER)
    ws.append([1, "Existing Person", "", "", "", "3/1/24", "yes", "already has notes", "called once"])
    overview = wb.create_sheet("OVERVIEW")
    overview.append(["untouched"])
    wb.save(path)


def _seed_contact(conn, bank_name="Goldman Sachs", first="Jane", last="Doe",
                  email="jane@gs.com", notes='["UCLA Anderson — MBA"]'):
    bank_id = db.upsert_bank(conn, bank_name, "gs.com", "BB", 1)
    conn.execute(
        """INSERT INTO contacts (apollo_id, first_name, last_name, title, city,
             bank_id, email, status, affinity_notes, research_notes, found_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"apollo-{first}-{last}", first, last, "VP", "Los Angeles", bank_id,
         email, "enriched", notes, "Chinese-speaking (inferred, high confidence)", "x"),
    )


def test_new_contact_routes_into_existing_abbreviated_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    with db.session() as conn:
        _seed_contact(conn)

    xlsx_path = tmp_path / "banks.xlsx"
    _make_fixture_workbook(xlsx_path)

    stats = xlsx_sync.sync_to_workbook(xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path)
    assert "Goldman Sachs" not in wb.sheetnames
    ws = wb["GS"]
    names = [ws.cell(row=r, column=2).value for r in range(2, ws.max_row + 1)]
    assert "Jane Doe" in names
    assert stats["created_rows"] == 1
    assert wb["OVERVIEW"].cell(row=1, column=1).value == "untouched"


def test_existing_row_manual_notes_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    with db.session() as conn:
        _seed_contact(conn, first="Existing", last="Person", email="existing@gs.com")

    xlsx_path = tmp_path / "banks.xlsx"
    _make_fixture_workbook(xlsx_path)

    xlsx_sync.sync_to_workbook(xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["GS"]
    row = [ws.cell(row=2, column=c).value for c in range(1, 10)]
    assert row[7] == "already has notes"
    assert row[2] == "existing@gs.com"


def test_new_bank_creates_new_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    with db.session() as conn:
        _seed_contact(conn, bank_name="Centerview Partners", first="Sam", last="Lee",
                      email="sam@centerview.com")

    xlsx_path = tmp_path / "banks.xlsx"
    _make_fixture_workbook(xlsx_path)

    stats = xlsx_sync.sync_to_workbook(xlsx_path)
    assert "Centerview Partners" in stats["new_sheets"]
    wb = openpyxl.load_workbook(xlsx_path)
    assert wb["Centerview Partners"].cell(row=1, column=1).value == "#"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_xlsx_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.xlsx_sync'`

- [ ] **Step 3: Implement `src/xlsx_sync.py`**

```python
"""One-way sync from the contacts DB into config/banks.xlsx.

Never overwrites a cell the user has already filled in by hand, and never
touches Date/Conversation/Comments or any non-bank sheet.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import openpyxl

from . import db
from .config import ROOT
from .matching import summarize

log = logging.getLogger(__name__)

DEFAULT_PATH = ROOT / "config" / "banks.xlsx"
HEADER = ["#", "Name", "Email", "Position", "Location", "Date", "Conversation", "Notes", "Comments"]

# banks.csv uses full names; the workbook's hand-built tabs use these
# abbreviations. Only banks currently overlapping between the two are
# listed here — anything else gets a brand-new tab named after the bank.
BANK_SHEET_ALIASES = {
    "Goldman Sachs": "GS",
    "Morgan Stanley": "MS",
}

_INVALID_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")


def _sheet_name(bank_name: str, existing: set[str]) -> str:
    if bank_name in existing:
        return bank_name
    alias = BANK_SHEET_ALIASES.get(bank_name)
    if alias and alias in existing:
        return alias
    return _INVALID_SHEET_CHARS.sub("", bank_name)[:31]


def _contact_notes(row: dict) -> str:
    notes: list[str] = []
    try:
        notes.extend(json.loads(row.get("affinity_notes") or "[]"))
    except (json.JSONDecodeError, TypeError):
        pass
    if row.get("research_notes"):
        notes.append(row["research_notes"])
    return summarize(notes) if notes else ""


def _find_row(ws, name: str) -> int | None:
    target = name.strip().lower()
    for r in range(2, ws.max_row + 1):
        cell = ws.cell(row=r, column=2).value
        if cell and str(cell).strip().lower() == target:
            return r
    return None


def _next_number(ws) -> int:
    n = 0
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if isinstance(v, (int, float)):
            n = max(n, int(v))
    return n + 1


def sync_to_workbook(path: Path | None = None) -> dict:
    path = path or DEFAULT_PATH
    stats = {"updated": 0, "created_rows": 0, "new_sheets": []}
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")

    wb = openpyxl.load_workbook(path)
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT c.*, b.name AS bank_name FROM contacts c
               JOIN banks b ON b.id = c.bank_id
               WHERE c.status IN ('enriched','drafted','queued')
                 AND c.email IS NOT NULL"""
        ).fetchall()]

    existing_sheets = set(wb.sheetnames)
    for row in rows:
        name = f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
        if not name:
            continue
        sheet_name = _sheet_name(row["bank_name"], existing_sheets)
        if sheet_name not in wb.sheetnames:
            ws = wb.create_sheet(sheet_name)
            ws.append(HEADER)
            existing_sheets.add(sheet_name)
            stats["new_sheets"].append(sheet_name)
        else:
            ws = wb[sheet_name]

        notes = _contact_notes(row)
        r = _find_row(ws, name)
        if r is None:
            ws.append([
                _next_number(ws), name, row.get("email") or "", row.get("title") or "",
                row.get("city") or "", None, None, notes, None,
            ])
            stats["created_rows"] += 1
            continue

        changed = False
        for col, value in ((3, row.get("email")), (4, row.get("title")),
                           (5, row.get("city")), (8, notes)):
            if value and not ws.cell(row=r, column=col).value:
                ws.cell(row=r, column=col, value=value)
                changed = True
        if changed:
            stats["updated"] += 1

    fd, tmp_name = tempfile.mkstemp(suffix=".xlsx", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    wb.save(tmp_path)
    shutil.move(str(tmp_path), str(path))
    return stats
```

This requires `ROOT` to be importable from `src/config.py`. Check `src/config.py:11` — `ROOT = Path(__file__).resolve().parent.parent` already exists at module level and is not currently exported via `__all__` restrictions, so `from .config import ROOT` works as-is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_xlsx_sync.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/xlsx_sync.py tests/test_xlsx_sync.py
git commit -m "feat: sync discovered contacts into banks.xlsx without overwriting manual notes"
```

---

## Task 11: Web UI trigger

**Files:**
- Modify: `web/app.py`
- Modify: `web/templates/dashboard.html`
- Test: `tests/test_web_sync_route.py`

**Interfaces:**
- Consumes: `xlsx_sync.sync_to_workbook` (Task 10).
- Produces: `POST /sync-xlsx` route, redirects to `/`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_web_sync_route.py`:

```python
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import db
from web.app import app


def test_sync_xlsx_route_triggers_sync_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    client = TestClient(app)
    with patch(
        "web.app.sync_to_workbook",
        return_value={"updated": 1, "created_rows": 0, "new_sheets": []},
    ) as mock_sync:
        resp = client.post("/sync-xlsx", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    mock_sync.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_sync_route.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet) or an import error for `sync_to_workbook` from `web.app`.

- [ ] **Step 3: Add the route to `web/app.py`**

Add this import alongside the existing `src` imports near the top:

```python
from src.xlsx_sync import sync_to_workbook  # noqa: E402
```

Add this route (near `/run/{stage}`):

```python
@app.post("/sync-xlsx")
def sync_xlsx():
    try:
        result = sync_to_workbook()
    except Exception as e:
        result = {"error": str(e)}
    LAST_RUN.clear()
    LAST_RUN.update({"stage": "sync-xlsx", "result": result})
    return RedirectResponse("/", status_code=303)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_sync_route.py -v`
Expected: PASS

- [ ] **Step 5: Add the button to the dashboard**

In `web/templates/dashboard.html`, inside the existing `<div class="control-row">` (after the `/run/push` form), add:

```html
    <form method="post" action="/sync-xlsx">
      <button type="submit">Sync to Excel</button>
      <span class="hint">Writes new contacts into banks.xlsx. Never overwrites your notes.</span>
    </form>
```

- [ ] **Step 6: Run the full test suite**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS (all tests across every task so far).

- [ ] **Step 7: Commit**

```bash
git add web/app.py web/templates/dashboard.html tests/test_web_sync_route.py
git commit -m "feat: add Sync to Excel button and route to the local web UI"
```

---

## Task 12: Manual end-to-end verification

Not automated — run this once against real data before trusting the daily cron with these changes.

- [ ] **Step 1:** Start the web UI: `.venv/Scripts/python.exe -m uvicorn web.app:app --reload --port 8000`
- [ ] **Step 2:** From the dashboard, run `load-banks` (via CLI: `.venv/Scripts/python.exe -m src.cli load-banks`), then click "Search all banks", then "Reveal emails".
- [ ] **Step 3:** Click "Write drafts". Open `/drafts` and check a handful of real drafts:
  - Does the referenced "angle" match the contact's actual background (e.g. a UCLA Anderson grad gets the Anderson angle, not plain UCLA)?
  - Does a non-UCLA, non-USC contact with no seniority match get a sensible non-target/standard draft?
  - If `LLM_PROVIDER=google`, does any drafted contact show a Chinese-speaking research note, and does it read as inferred, not asserted?
- [ ] **Step 4:** Check `/contacts` for anyone who should have been skipped as not-yet-graduated — confirm they're still sitting at `enriched` status, not drafted.
- [ ] **Step 5:** Click "Sync to Excel". Open `config/banks.xlsx` and confirm: new contacts appear under the correct tab (including `GS`/`MS` routing to the existing abbreviated tabs), any manually-typed `Notes`/`Date`/`Conversation`/`Comments` you had are untouched, and a bank with no existing tab got a new one with the right header row.
- [ ] **Step 6:** Only after this looks right, let the next scheduled GitHub Actions run go through unattended.
