"""Command line entry point. `python -m src.cli --help`"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import db, pipeline
from .config import settings


def _log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def gmail_auth(port: int = 0):
    """Walk through the OAuth consent flow once and print a refresh token.

    Uses a loopback redirect. Google retired the old copy-a-code-from-the-page
    flow (urn:ietf:wg:oauth:2.0:oob), so we stand up a throwaway HTTP server,
    let Google redirect the browser back to it, and read the code off the URL.
    Desktop app clients are allowed to use any localhost port for this, so
    nothing needs registering in the console.
    """
    import http.server
    import secrets
    import socket
    import threading
    import urllib.parse
    import webbrowser

    import httpx

    cid = settings.gmail_client_id or input("Gmail OAuth client ID: ").strip()
    secret = settings.gmail_client_secret or input("Client secret: ").strip()

    if not port:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    redirect = f"http://localhost:{port}"
    state = secrets.token_urlsafe(16)
    caught: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            caught.update({k: v[0] for k, v in q.items()})
            ok = "code" in caught and caught.get("state") == state
            body = (
                "<h2>Authorized.</h2><p>Close this tab and return to your terminal.</p>"
                if ok else
                f"<h2>Authorization failed.</h2><p>{caught.get('error', 'No code returned.')}</p>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:system-ui;padding:3rem'>{body}</body></html>".encode())

        def log_message(self, *args):
            pass  # keep the console clean

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.compose",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"

    print(f"\nOpening your browser. If it doesn't open, paste this in:\n\n{url}\n")
    print("You'll see an 'unverified app' warning. Click Advanced, then "
          "'Go to (your app name)'. That warning is expected.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"Waiting for the redirect on {redirect} ...")

    deadline = time.time() + 300
    while "code" not in caught and "error" not in caught and time.time() < deadline:
        time.sleep(0.4)
    server.server_close()

    if "code" not in caught:
        print(f"\nNo code received: {caught.get('error', 'timed out')}", file=sys.stderr)
        sys.exit(1)
    if caught.get("state") != state:
        print("\nState mismatch. Start over.", file=sys.stderr)
        sys.exit(1)

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": caught["code"], "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    if r.status_code >= 400:
        print(f"Exchange failed: {r.text}", file=sys.stderr)
        sys.exit(1)
    token = r.json().get("refresh_token")
    if not token:
        print("No refresh token returned. Google only sends one on first "
              "consent — revoke this app at myaccount.google.com/permissions "
              "and run again.", file=sys.stderr)
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
    g = sub.add_parser("gmail-auth", help="one-time OAuth setup")
    g.add_argument("--port", type=int, default=0,
                   help="fixed loopback port; default picks a free one")

    args = p.parse_args()
    _log()

    if args.cmd == "gmail-auth":
        return gmail_auth(args.port)
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