"""Command line entry point. `python -m src.cli --help`"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db, pipeline
from .config import settings


def _log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def gmail_auth():
    """Walk through the OAuth consent flow once and print a refresh token."""
    import urllib.parse
    import httpx

    cid = settings.gmail_client_id or input("Gmail OAuth client ID: ").strip()
    secret = settings.gmail_client_secret or input("Client secret: ").strip()
    redirect = "urn:ietf:wg:oauth:2.0:oob"

    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.compose",
        "access_type": "offline",
        "prompt": "consent",
    })
    print("\nOpen this in a browser, approve, then paste the code back here:\n")
    print(f"https://accounts.google.com/o/oauth2/v2/auth?{params}\n")
    code = input("Code: ").strip()

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    if r.status_code >= 400:
        print(f"Exchange failed: {r.text}", file=sys.stderr)
        sys.exit(1)
    token = r.json().get("refresh_token")
    if not token:
        print("No refresh token returned. Revoke prior access and retry.",
              file=sys.stderr)
        sys.exit(1)
    print(f"\nAdd this to .env and to your GitHub repo secrets:\n"
          f"GMAIL_REFRESH_TOKEN={token}\n")


def status():
    db.init()
    with db.session() as conn:
        c = db.counts(conn)
        runs = [dict(r) for r in db.recent_runs(conn, 5)]
    print(json.dumps({"counts": c, "recent_runs": runs}, indent=2))
    gaps = settings.missing()
    if gaps:
        print("\nStill needs configuring: " + ", ".join(gaps))


def main():
    p = argparse.ArgumentParser(prog="outreach", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database")
    sub.add_parser("load-banks", help="import config/banks.csv")
    r = sub.add_parser("refresh", help="search Apollo for people at every bank")
    r.add_argument("--per-bank", type=int, default=None)
    e = sub.add_parser("enrich", help="reveal emails and score a batch")
    e.add_argument("--limit", type=int, default=None)
    d = sub.add_parser("draft", help="write emails for top-scoring contacts")
    d.add_argument("--limit", type=int, default=None)
    sub.add_parser("push", help="move approved drafts into Gmail")
    sub.add_parser("daily", help="the scheduled job: enrich, draft, push")
    sub.add_parser("status", help="counts and recent runs")
    sub.add_parser("gmail-auth", help="one-time OAuth setup")

    args = p.parse_args()
    _log()

    if args.cmd == "gmail-auth":
        return gmail_auth()
    if args.cmd == "status":
        return status()

    db.init()
    if args.cmd == "init":
        print(f"Database ready.")
    elif args.cmd == "load-banks":
        print(f"Loaded {pipeline.load_banks()} banks.")
    elif args.cmd == "refresh":
        print(json.dumps(pipeline.refresh_targets(args.per_bank), indent=2))
    elif args.cmd == "enrich":
        print(json.dumps(pipeline.enrich_batch(args.limit), indent=2))
    elif args.cmd == "draft":
        print(json.dumps(pipeline.draft_batch(args.limit), indent=2))
    elif args.cmd == "push":
        print(json.dumps(pipeline.push_to_gmail(), indent=2))
    elif args.cmd == "daily":
        print(json.dumps(pipeline.daily(), indent=2))


if __name__ == "__main__":
    main()
