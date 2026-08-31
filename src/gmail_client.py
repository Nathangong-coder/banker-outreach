"""Create Gmail drafts. This module never sends — sending stays a human action.

Auth uses an installed-app OAuth refresh token so the scheduled run needs no
browser. Generate one with: python -m src.cli gmail-auth
"""
from __future__ import annotations

import base64
import logging
from email.message import EmailMessage

import httpx

from .config import settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
SCOPE = "https://www.googleapis.com/auth/gmail.compose"


class GmailError(RuntimeError):
    pass


class Gmail:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 refresh_token: str | None = None):
        self.client_id = client_id or settings.gmail_client_id
        self.client_secret = client_secret or settings.gmail_client_secret
        self.refresh_token = refresh_token or settings.gmail_refresh_token
        self.client = httpx.Client(timeout=30.0)
        self._access_token: str | None = None

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        if not all([self.client_id, self.client_secret, self.refresh_token]):
            raise GmailError(
                "Gmail credentials incomplete. Run: python -m src.cli gmail-auth"
            )
        r = self.client.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        })
        if r.status_code >= 400:
            raise GmailError(f"Token refresh failed: {r.text[:300]}")
        self._access_token = r.json()["access_token"]
        return self._access_token

    def create_draft(self, to: str, subject: str, body: str,
                     sender: str | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        if sender or settings.sender_email:
            msg["From"] = sender or settings.sender_email
        msg.set_content(body)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = self.client.post(
            DRAFTS_URL,
            headers={"Authorization": f"Bearer {self._token()}"},
            json={"message": {"raw": raw}},
        )
        if r.status_code >= 400:
            raise GmailError(f"Draft creation failed: {r.status_code} {r.text[:300]}")
        return r.json()["id"]
