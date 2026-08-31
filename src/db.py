"""SQLite store. Single file, committed back to the repo by the scheduled run."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS banks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    domain      TEXT,
    category    TEXT,
    priority    INTEGER DEFAULT 5,
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY,
    apollo_id       TEXT UNIQUE,
    first_name      TEXT,
    last_name       TEXT,
    title           TEXT,
    seniority       TEXT,
    bank_id         INTEGER REFERENCES banks(id),
    linkedin_url    TEXT,
    city            TEXT,
    email           TEXT,
    email_status    TEXT,
    education       TEXT,
    affinity_score  INTEGER DEFAULT 0,
    affinity_notes  TEXT,
    status          TEXT DEFAULT 'new',
    hook            TEXT,
    found_at        TEXT,
    enriched_at     TEXT,
    UNIQUE(first_name, last_name, bank_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER REFERENCES contacts(id),
    subject         TEXT,
    body            TEXT,
    provider        TEXT,
    model           TEXT,
    status          TEXT DEFAULT 'pending',
    gmail_draft_id  TEXT,
    created_at      TEXT,
    reviewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT,
    started_at  TEXT,
    finished_at TEXT,
    stats       TEXT,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_contacts_affinity ON contacts(affinity_score DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
"""

# contacts.status flows: new -> enriched -> drafted -> queued -> sent
#                             \-> no_email      \-> skipped


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init(path: Path | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)


# --- banks ---------------------------------------------------------------

def upsert_bank(conn, name: str, domain: str | None, category: str | None,
                priority: int = 5) -> int:
    conn.execute(
        """INSERT INTO banks (name, domain, category, priority)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             domain=excluded.domain,
             category=excluded.category,
             priority=excluded.priority""",
        (name.strip(), (domain or "").strip().lower() or None, category, priority),
    )
    row = conn.execute("SELECT id FROM banks WHERE name = ?", (name.strip(),)).fetchone()
    return row["id"]


def active_banks(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM banks WHERE active = 1 ORDER BY priority ASC, name ASC"
    ).fetchall()


# --- contacts ------------------------------------------------------------

def upsert_contact(conn, bank_id: int, person: dict) -> int | None:
    """Insert a search hit. Returns contact id, or None if we already had them."""
    existing = conn.execute(
        "SELECT id FROM contacts WHERE apollo_id = ?", (person.get("apollo_id"),)
    ).fetchone()
    if existing:
        return None
    try:
        cur = conn.execute(
            """INSERT INTO contacts
               (apollo_id, first_name, last_name, title, seniority, bank_id,
                linkedin_url, city, status, found_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (
                person.get("apollo_id"), person.get("first_name"),
                person.get("last_name"), person.get("title"),
                person.get("seniority"), bank_id, person.get("linkedin_url"),
                person.get("city"), now(),
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # same human, different Apollo record


def set_enrichment(conn, contact_id: int, email: str | None, email_status: str | None,
                   education: list | None, score: int, notes: list[str]) -> None:
    conn.execute(
        """UPDATE contacts SET email=?, email_status=?, education=?,
             affinity_score=?, affinity_notes=?, enriched_at=?,
             status = CASE WHEN ? IS NULL THEN 'no_email' ELSE 'enriched' END
           WHERE id = ?""",
        (email, email_status, json.dumps(education or []), score,
         json.dumps(notes), now(), email, contact_id),
    )


def pending_enrichment(conn, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.*, b.name AS bank_name, b.priority
           FROM contacts c JOIN banks b ON b.id = c.bank_id
           WHERE c.status = 'new'
           ORDER BY b.priority ASC, c.id ASC LIMIT ?""",
        (limit,),
    ).fetchall()


def ready_to_draft(conn, limit: int, min_affinity: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.*, b.name AS bank_name, b.category
           FROM contacts c JOIN banks b ON b.id = c.bank_id
           WHERE c.status = 'enriched' AND c.email IS NOT NULL
             AND c.affinity_score >= ?
           ORDER BY c.affinity_score DESC, b.priority ASC LIMIT ?""",
        (min_affinity, limit),
    ).fetchall()


# --- drafts --------------------------------------------------------------

def save_draft(conn, contact_id: int, subject: str, body: str,
               provider: str, model: str) -> int:
    cur = conn.execute(
        """INSERT INTO drafts (contact_id, subject, body, provider, model,
                               status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (contact_id, subject, body, provider, model, now()),
    )
    conn.execute("UPDATE contacts SET status='drafted' WHERE id=?", (contact_id,))
    return cur.lastrowid


def drafts_with_contacts(conn, status: str | None = None) -> list[sqlite3.Row]:
    q = """SELECT d.*, c.first_name, c.last_name, c.title, c.email,
                  c.affinity_score, c.affinity_notes, c.hook, b.name AS bank_name,
                  b.category
           FROM drafts d
           JOIN contacts c ON c.id = d.contact_id
           JOIN banks b ON b.id = c.bank_id"""
    params: tuple = ()
    if status:
        q += " WHERE d.status = ?"
        params = (status,)
    q += " ORDER BY c.affinity_score DESC, d.created_at DESC"
    return conn.execute(q, params).fetchall()


# --- runs ----------------------------------------------------------------

def start_run(conn, kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, now())
    )
    return cur.lastrowid


def finish_run(conn, run_id: int, stats: dict, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, stats=?, error=? WHERE id=?",
        (now(), json.dumps(stats), error, run_id),
    )


def recent_runs(conn, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def counts(conn) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM contacts GROUP BY status"
    ).fetchall()
    out = {r["status"]: r["n"] for r in rows}
    out["banks"] = conn.execute(
        "SELECT COUNT(*) n FROM banks WHERE active=1"
    ).fetchone()["n"]
    out["pending_drafts"] = conn.execute(
        "SELECT COUNT(*) n FROM drafts WHERE status='pending'"
    ).fetchone()["n"]
    return out
