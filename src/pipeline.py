"""The stages, and the daily job that strings them together.

Search is expensive and slow-changing, so it runs on its own cadence.
The daily job only enriches, drafts, and pushes to Gmail.
"""
from __future__ import annotations

import csv
import json
import logging

from . import db
from .apollo import Apollo
from .config import BANKS_CSV, settings
from .drafting import draft_for
from .gmail_client import Gmail, GmailError
from .llm import LLM
from .matching import score_contact

log = logging.getLogger(__name__)


def load_banks(csv_path=None) -> int:
    """Read config/banks.csv into the DB. Safe to re-run."""
    path = csv_path or BANKS_CSV
    n = 0
    with db.session() as conn, open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or row.get("bank") or "").strip()
            if not name:
                continue
            db.upsert_bank(
                conn, name,
                domain=row.get("domain"),
                category=(row.get("category") or "").strip() or None,
                priority=int(row.get("priority") or 5),
            )
            n += 1
    log.info("Loaded %d banks", n)
    return n


def refresh_targets(per_bank: int | None = None) -> dict:
    """Search every active bank for people matching the target titles.

    Run this monthly, not daily. Rosters barely move week to week.
    """
    per_bank = per_bank or settings.search_per_bank
    stats = {"banks": 0, "found": 0, "new": 0, "errors": []}
    with db.session() as conn:
        run_id = db.start_run(conn, "refresh")
        banks = db.active_banks(conn)

    with Apollo() as apollo:
        for bank in banks:
            try:
                people = apollo.search_people(
                    domain=bank["domain"],
                    org_name=bank["name"],
                    titles=settings.target_titles,
                    seniorities=settings.target_seniorities,
                    per_page=per_bank,
                )
            except Exception as e:
                stats["errors"].append(f"{bank['name']}: {e}")
                log.error("Search failed for %s: %s", bank["name"], e)
                continue

            stats["banks"] += 1
            stats["found"] += len(people)
            with db.session() as conn:
                for person in people:
                    if db.upsert_contact(conn, bank["id"], person):
                        stats["new"] += 1
            log.info("%s: %d found", bank["name"], len(people))

    with db.session() as conn:
        db.finish_run(conn, run_id, stats)
    return stats


def enrich_batch(limit: int | None = None) -> dict:
    """Reveal emails for the highest-priority un-enriched contacts, then score them."""
    limit = limit or settings.enrich_batch
    rules = settings.affinity_rules()
    stats = {"attempted": 0, "with_email": 0, "no_email": 0, "scored": 0}

    with db.session() as conn:
        pending = [dict(r) for r in db.pending_enrichment(conn, limit)]
    if not pending:
        return stats

    with Apollo() as apollo:
        payload = [
            {"apollo_id": p["apollo_id"], "linkedin_url": p["linkedin_url"],
             "first_name": p["first_name"], "last_name": p["last_name"]}
            for p in pending
        ]
        matches = apollo.enrich(payload)

    with db.session() as conn:
        for p in pending:
            stats["attempted"] += 1
            m = matches.get(p["apollo_id"], {})
            email = m.get("email")
            # Apollo marks guessed addresses; only trust verified ones.
            if m.get("email_status") not in (None, "verified", "likely"):
                email = None
            education = m.get("education") or []
            score, reasons = score_contact(
                {**p, "education": education}, rules
            )
            db.set_enrichment(conn, p["id"], email, m.get("email_status"),
                              education, score, reasons)
            if email:
                stats["with_email"] += 1
            else:
                stats["no_email"] += 1
            if score > 0:
                stats["scored"] += 1
    return stats


def draft_batch(limit: int | None = None) -> dict:
    """Write emails for the top-scoring contacts that have a verified address."""
    limit = limit or settings.drafts_per_day
    stats = {"drafted": 0, "errors": []}
    with db.session() as conn:
        targets = [dict(r) for r in db.ready_to_draft(conn, limit, settings.min_affinity)]
    if not targets:
        return stats

    with LLM() as llm:
        for contact in targets:
            try:
                subject, body = draft_for(llm, contact)
            except Exception as e:
                stats["errors"].append(f"{contact['first_name']} {contact['last_name']}: {e}")
                log.error("Draft failed: %s", e)
                continue
            with db.session() as conn:
                db.save_draft(conn, contact["id"], subject, body,
                              llm.provider, llm.model)
            stats["drafted"] += 1
    return stats


def push_to_gmail(limit: int = 50) -> dict:
    """Move approved drafts into the Gmail drafts folder. Never sends."""
    stats = {"pushed": 0, "errors": []}
    with db.session() as conn:
        approved = [dict(r) for r in db.drafts_with_contacts(conn, status="approved")][:limit]
    if not approved:
        return stats

    try:
        gmail = Gmail()
    except GmailError as e:
        stats["errors"].append(str(e))
        return stats

    with gmail:
        for d in approved:
            try:
                gid = gmail.create_draft(d["email"], d["subject"], d["body"])
            except GmailError as e:
                stats["errors"].append(f"draft {d['id']}: {e}")
                continue
            with db.session() as conn:
                conn.execute(
                    """UPDATE drafts SET status='in_gmail', gmail_draft_id=?,
                       reviewed_at=? WHERE id=?""",
                    (gid, db.now(), d["id"]),
                )
                conn.execute("UPDATE contacts SET status='queued' WHERE id=?",
                             (d["contact_id"],))
            stats["pushed"] += 1
    return stats


def daily() -> dict:
    """What the cron runs. Enrich, draft, push whatever was already approved."""
    db.init()
    with db.session() as conn:
        run_id = db.start_run(conn, "daily")
    stats: dict = {}
    error = None
    try:
        stats["enrich"] = enrich_batch()
        stats["draft"] = draft_batch()
        stats["gmail"] = push_to_gmail()
    except Exception as e:
        error = str(e)
        log.exception("Daily run failed")
    with db.session() as conn:
        db.finish_run(conn, run_id, stats, error)
    return stats
