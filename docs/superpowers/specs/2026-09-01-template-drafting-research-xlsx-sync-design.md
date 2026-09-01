# Template-driven drafting, background research, and Excel sync

Status: approved by user, ready for implementation planning
Date: 2026-09-01

## Context

The pipeline currently drafts fully freeform emails via a single hardcoded
system prompt (`src/drafting.py`), scored only on Apollo's `education` data
against `config/affinity.yml` school rules. The user has a separate manual
process — a workbook (`config/banks.xlsx`) with one tab per bank tracking
`Name | Email | Position | Location | Date | Conversation | Notes |
Comments`, and a set of hand-written cold email templates
(`config/Cold Email Templates.docx`) keyed by the recipient's background
(UCLA, Anderson, USC, UC system, non-target school, VP/MD+, and others out
of scope for this round: hometown, club, high school, international
student).

This spec covers three additions:
1. A background-research step that infers a "Chinese-speaking" signal via
   web search, and a graduation gate using data already collected.
2. Template-aware drafting: pick the right template category per contact
   and pass its distinguishing talking point into the existing LLM prompt.
3. A one-way sync from the SQLite DB into `banks.xlsx`, run manually from
   the local web UI, that never overwrites the user's own manual notes.

`config/banks.csv` remains the sole input for which banks the pipeline
searches. `banks.xlsx` becomes a pure output artifact.

## Non-goals

- No change to `push_to_gmail` — it still only ever creates Gmail drafts
  (`gmail.compose` scope, `drafts.create` endpoint). Nothing in this spec
  adds a send path.
- No HOMETOWN / CLUB / HIGH SCHOOL / INTERNATIONAL STUDENT templates —
  no reliable automated signal exists for these yet.
- No change to the GitHub Actions cron — Excel sync is local-only.
- No change to how banks are searched/selected (`banks.csv` stays canonical).

## Data model changes (`src/db.py`)

Add four nullable columns to `contacts`:

- `graduated INTEGER` — 1/0/NULL. NULL means "unknown, treated as graduated"
  per the trust-Apollo-by-default policy below.
- `chinese_speaking INTEGER` — 1/0/NULL. NULL means "not checked" (e.g.
  provider isn't `google`, or the check failed).
- `research_notes TEXT` — short human-readable string, same style as
  `affinity_notes`, e.g. `"Chinese-speaking (inferred)"`.
- `template_used TEXT` — the selected template category, e.g. `"UCLA ALUM"`,
  stored for visibility in the web UI and for debugging bad picks.

`CREATE TABLE IF NOT EXISTS` won't add columns to an existing DB file, so
this needs an `ALTER TABLE ... ADD COLUMN` migration path in `db.init()`
(guarded by checking `PRAGMA table_info(contacts)` first, since SQLite
has no `IF NOT EXISTS` for columns).

## Graduation gate

New helper, e.g. `matching.is_graduated(education: list[dict]) -> bool`:

- Look at the most recent education entry (by `end` date, falling back to
  `start` date if `end` is missing).
- If that entry's `end` date is missing or in the past (or unparseable),
  return `True`.
- If that entry's `end` date is clearly in the future (parses to a year
  greater than the current year), return `False`.

This runs during `enrich_batch` (data is already on hand from Apollo
enrichment) and is stored via the new `graduated` column. `draft_batch`
excludes `graduated = 0` contacts from its target query — they stay in
`enriched` status, untouched, available for manual review in the web UI.

## Research step (`src/research.py`, new file)

```python
def chinese_speaking_signal(llm: LLM, contact: dict) -> dict:
    """Best-effort inference only. Returns
    {"chinese_speaking": bool | None, "confidence": "low"|"medium"|"high", "notes": str}.
    Returns chinese_speaking=None, notes="" when provider != google or on any error.
    """
```

- Only runs when `settings.llm.provider == "google"`. Any other provider:
  short-circuit, return the None/"" shape, log once per run (not once per
  contact) that the check is unavailable for the configured provider.
- Uses a new `LLM` method, e.g. `LLM.search_complete(system, user) ->
  dict`, which builds the same Gemini `generateContent` request as
  `complete()`'s google branch but adds `"tools": [{"google_search": {}}]`
  to the payload. Google's grounding is server-side — one HTTP round trip,
  no client-side tool-call loop needed, consistent with the rest of
  `llm.py`'s minimal-HTTP style. Reuses the existing `KeyPool`/retry logic
  in `complete()` — refactor the retry loop into a shared internal method
  parameterized by payload-builder, rather than duplicating it.
- Prompt gives the model the contact's name, title, bank, city, and
  LinkedIn URL (if present) and asks it to search and judge, from public
  information only, whether there's a real signal (name origin combined
  with other public bio evidence, education in a Chinese-speaking region,
  language listed) that they speak Chinese. Explicitly instructed: this is
  inference, not confirmation; return `null` rather than guessing when
  evidence is thin.
- Always surfaces as "(inferred)" in `research_notes` — never asserted as
  fact — e.g. `"Chinese-speaking (inferred, medium confidence)"`.
- Called from `draft_batch`, once per contact in the batch about to be
  drafted (not during `enrich_batch`) — keeps search-grounded calls scoped
  to contacts that are actually about to be reached out to. Result is
  persisted to the `chinese_speaking` / `research_notes` columns so it's
  not redone on a future run for the same contact.

## Template module (`src/templates.py`, new file)

- Parses `config/Cold Email Templates.docx` at import time using the same
  approach used during design exploration: read `word/document.xml` from
  the zip, iterate `<w:p>` paragraphs, join `<w:t>` runs. Splits on the
  ALL-CAPS category headers, and cuts off before the "FOLLOW UP TEMPLATES"
  section entirely (out of scope here — those are for manual reply
  follow-ups, not the initial draft).
- In-scope categories only: `STANDARD`, `UCLA ALUM`, `ANDERSON ALUM`,
  `UNIVERSITY OF CALIFORNIA ALUM`, `USC ALUM`, `NON TARGET SCHOOL`,
  `VICE PRESIDENTS, MANAGING DIRECTORS, AND ABOVE`. For each, extract the
  one sentence that names the specific personal angle (e.g. Anderson's:
  "...how you found yourself at FIRM after graduating from Anderson") as
  that category's `talking_point`. Parsed once at import; if the docx is
  ever missing or a category can't be found, fall back to `STANDARD`'s
  talking point and log a warning rather than raising.

```python
def select_template(affinity_notes: list[str], seniority: str | None,
                     title: str | None, has_education: bool) -> tuple[str, str]:
    """Returns (category, talking_point)."""
```

Priority order (first match wins):
1. Anderson match (affinity_notes contains "UCLA Anderson")
2. UCLA match (affinity_notes contains "UCLA", not Anderson)
3. USC match — note: `affinity.yml` has no USC rule today; this spec adds
   one (`label: "USC"`, matching "University of Southern California" /
   "USC" in schools) since the user explicitly asked for a USC template.
4. UC-system match (affinity_notes contains "UC system")
5. VP/MD+ (seniority or title matches `director|vp|managing director` —
   reuse `settings.target_seniorities`-style matching already used
   elsewhere)
6. Non-target school (`has_education` true, but none of the above matched)
7. Standard (no education data at all)

## Drafting change (`src/drafting.py`)

- `build_user_prompt` gains one new line, inserted after `Shared
  background`: `Angle to use: {talking_point}`.
- If `chinese_speaking` is `True`, append `"; Chinese-speaking (inferred)"`
  to the `Shared background` line (reuses the existing `summarize()`
  pattern — just append to the `reasons` list before calling it).
- `draft_for` (or `draft_batch`, which calls it) also passes the selected
  `category` through so `pipeline.draft_batch` can persist it to
  `template_used` alongside the draft.
- The SYSTEM prompt's existing rules are unchanged — this keeps the AI
  writing in its own voice (no cliché opener, under 120 words, etc.) per
  the decision to use templates as structure only, not literal copy.

## Excel sync (`src/xlsx_sync.py`, new file)

```python
def sync_to_workbook(path: Path = None) -> dict:
    """Returns {"updated": n, "created_rows": n, "new_sheets": [...]}."""
```

- Reads every contact with `status IN ('enriched', 'drafted', 'queued')`
  and a non-null `email`, joined to its bank.
- **Sheet resolution.** `banks.csv` names are full (`Goldman Sachs`,
  `Morgan Stanley`) but the workbook's existing tabs are abbreviated
  (`GS`, `MS`) — a bare name match would silently create duplicate
  `Goldman Sachs`/`Morgan Stanley` tabs instead of filling in the ones
  the user already tracks by hand. To resolve: (1) try an exact match on
  the bank name, (2) fall back to a small hardcoded alias table in
  `xlsx_sync.py` covering the two banks that currently overlap between
  `banks.csv` and the existing tabs — `{"Goldman Sachs": "GS", "Morgan
  Stanley": "MS"}` — (3) otherwise create a new sheet named after the
  bank. Location-split tabs (`MS (NY)`, `Evercore (NY)`) are not routed
  to automatically — the alias always points at the base tab; splitting
  by office would need city-parsing this spec doesn't attempt, and it's
  a one-row manual move if the user wants a contact under the NY tab
  instead. New sheets (sanitized: Excel's 31-character limit, invalid
  characters `: \ / ? * [ ]` stripped) are seeded with the same header
  row used by existing tabs: `# | Name | Email | Position | Location |
  Date | Conversation | Notes | Comments`.
- Matches existing rows by `Name` (case-insensitive, whitespace-trimmed).
  If a row exists: only fills `Email`/`Position`/`Location`/`Notes` cells
  that are currently blank — never overwrites a cell the user has already
  filled in, and never touches `Date`, `Conversation`, or `Comments`.
  If no row exists: appends a new row with `#` = next sequential number
  in that sheet, `Name`, `Email`, `Position`, `Location`, and `Notes`
  (built from `affinity_notes` + `research_notes`, joined the same way
  `matching.summarize()` joins reasons); `Date`/`Conversation`/`Comments`
  left blank for the user to fill in manually.
- Writes to a temp file and replaces the original atomically (avoids a
  half-written workbook if the process dies mid-save).
- Non-bank sheets (`OVERVIEW`, `APPLICATIONS`, `ACTIVE BAY`, `WALL
  STREET`, `Sheet4`) are left completely untouched.

### Web UI trigger

- New route `POST /sync-xlsx` in `web/app.py`, following the existing
  `BackgroundTasks` pattern used by `/run/{stage}`. New button on the
  dashboard or settings page. Not added to the GitHub Actions cron.

## Testing

- `tests/test_templates.py` — parsing produces all 7 in-scope categories
  with non-empty talking points; `select_template` priority order,
  including ties (Anderson beats plain UCLA; USC beats UC-system).
- `tests/test_research.py` — `chinese_speaking_signal` returns the
  None/"" shape immediately (no HTTP call) when provider isn't `google`;
  malformed JSON from the model degrades to the same safe shape rather
  than raising.
- `tests/test_matching.py` (extend existing) — `is_graduated` against
  missing/past/future end dates and missing education entirely.
- `tests/test_xlsx_sync.py` — run against a small fixture workbook copied
  to a temp path: new bank creates a correctly-headed sheet; existing row
  with a manually-filled `Notes` cell is not overwritten; existing row
  with blank `Email` gets filled; `Date`/`Conversation`/`Comments` are
  never touched.
- Manual verification (documented in the implementation plan, not
  automated): run `refresh` → `enrich` → `draft` against the real DB,
  inspect a handful of drafts in the web UI for sensible template choice,
  then run the new Excel sync button and confirm `banks.xlsx` looks right
  before this ever runs unattended.

## Open items carried into implementation (not blocking, but worth a note)

- `affinity.yml` needs a new USC rule added (see template selection,
  priority 3) — small, uncontroversial addition alongside this work.
- `PyYAML`/`openpyxl` need adding to `requirements.txt` (`openpyxl` is
  new; `PyYAML` is already there).
