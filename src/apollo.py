"""Apollo client: people search and email enrichment, across a pool of keys.

Endpoint paths and plan gating change from time to time. If search returns 403
or an unexpected shape, check your plan's API access first — that is the most
common cause. Paths are overridable via APOLLO_BASE_URL.
"""
from __future__ import annotations

import logging
import time

import httpx

from .config import settings
from .keypool import AllKeysExhausted, KeyPool

log = logging.getLogger(__name__)

SEARCH_PATH = "/mixed_people/search"
BULK_MATCH_PATH = "/people/bulk_match"


class Apollo:
    def __init__(self, keys: list[str] | None = None, base: str | None = None):
        self.pool = KeyPool("apollo", keys if keys is not None else settings.apollo_keys)
        self.base = (base or settings.apollo_base).rstrip("/")
        self.client = httpx.Client(timeout=45.0)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _post(self, path: str, payload: dict, attempts: int = 4) -> dict:
        last_err: Exception | None = None
        for attempt in range(attempts):
            state = self.pool.acquire()
            try:
                r = self.client.post(
                    f"{self.base}{path}",
                    json=payload,
                    headers={
                        "x-api-key": state.key,
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                    },
                )
            except httpx.RequestError as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:
                retry_after = float(r.headers.get("retry-after", 60))
                self.pool.penalize(state, retry_after)
                continue
            if r.status_code in (401, 403):
                self.pool.retire(state, f"HTTP {r.status_code}: {r.text[:200]}")
                continue
            if r.status_code >= 500:
                last_err = RuntimeError(f"Apollo {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Apollo {r.status_code} on {path}: {r.text[:300]}")
            return r.json()

        raise AllKeysExhausted(f"Apollo {path} failed after {attempts} attempts: {last_err}")

    def search_people(self, domain: str | None, org_name: str | None,
                      titles: list[str], seniorities: list[str],
                      per_page: int = 25, page: int = 1) -> list[dict]:
        """One page of people at one bank. Returns normalized dicts."""
        payload: dict = {
            "page": page,
            "per_page": min(per_page, 100),
            "person_titles": titles,
        }
        if seniorities:
            payload["person_seniorities"] = seniorities
        if domain:
            payload["q_organization_domains_list"] = [domain]
        elif org_name:
            payload["q_organization_name"] = org_name
        else:
            return []

        data = self._post(SEARCH_PATH, payload)
        people = data.get("people") or data.get("contacts") or []
        return [self._normalize(p) for p in people]

    def enrich(self, people: list[dict]) -> dict[str, dict]:
        """Reveal work emails for up to 10 people. Keyed by apollo_id.

        reveal_personal_emails is deliberately off — work addresses only.
        """
        if not people:
            return {}
        details = []
        for p in people[:10]:
            d = {}
            if p.get("apollo_id"):
                d["id"] = p["apollo_id"]
            if p.get("linkedin_url"):
                d["linkedin_url"] = p["linkedin_url"]
            if p.get("first_name"):
                d["first_name"] = p["first_name"]
            if p.get("last_name"):
                d["last_name"] = p["last_name"]
            if p.get("domain"):
                d["domain"] = p["domain"]
            details.append(d)

        data = self._post(BULK_MATCH_PATH, {
            "details": details,
            "reveal_personal_emails": False,
        })
        out = {}
        for match in (data.get("matches") or []):
            if not match:
                continue
            norm = self._normalize(match)
            if norm.get("apollo_id"):
                out[norm["apollo_id"]] = norm
        return out

    @staticmethod
    def _normalize(p: dict) -> dict:
        org = p.get("organization") or {}
        history = p.get("employment_history") or []
        education = []
        # Apollo returns schooling inside employment_history entries flagged as such,
        # and sometimes under a top-level key depending on the endpoint.
        for entry in history:
            if entry.get("degree") or entry.get("kind") == "education":
                education.append({
                    "school": entry.get("organization_name") or entry.get("school"),
                    "degree": entry.get("degree"),
                    "start": entry.get("start_date"),
                    "end": entry.get("end_date"),
                })
        for entry in (p.get("education") or []):
            education.append({
                "school": entry.get("school") or entry.get("organization_name"),
                "degree": entry.get("degree"),
                "start": entry.get("start_date"),
                "end": entry.get("end_date"),
            })
        return {
            "apollo_id": p.get("id"),
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "title": p.get("title"),
            "seniority": p.get("seniority"),
            "linkedin_url": p.get("linkedin_url"),
            "city": p.get("city"),
            "email": p.get("email"),
            "email_status": p.get("email_status"),
            "domain": org.get("primary_domain") or org.get("website_url"),
            "org_name": org.get("name"),
            "education": education,
        }
