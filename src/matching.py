"""Score a contact on shared background, using rules from config/affinity.yml.

Everything here keys off things people publish about themselves: schools,
degrees, prior employers, stated affiliations. Nothing is inferred from a name.
"""
from __future__ import annotations

import re
import unicodedata


def _norm(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def score_contact(person: dict, rules: dict) -> tuple[int, list[str]]:
    """Return (score, human-readable reasons).

    Reasons feed straight into the drafting prompt, so they should read like
    something you'd actually say out loud: "UCLA — Economics".
    """
    total = 0
    reasons: list[str] = []
    schools = [_norm(e.get("school")) for e in (person.get("education") or [])]
    degrees = {_norm(e.get("school")): (e.get("degree") or "") for e in
               (person.get("education") or [])}
    haystack = " | ".join(filter(None, [
        _norm(person.get("title")),
        _norm(person.get("city")),
        *schools,
    ]))

    for rule in (rules.get("rules") or []):
        weight = int(rule.get("weight", 1))
        label = rule.get("label", "match")
        matched_on = None

        for term in (rule.get("schools") or []):
            nterm = _norm(term)
            for school in schools:
                if nterm and nterm in school:
                    matched_on = term
                    break
            if matched_on:
                break

        if not matched_on:
            for term in (rule.get("keywords") or []):
                nterm = _norm(term)
                if nterm and nterm in haystack:
                    matched_on = term
                    break

        if matched_on:
            total += weight
            degree = degrees.get(_norm(matched_on), "")
            detail = f"{label} — {degree}" if degree else label
            if detail not in reasons:
                reasons.append(detail)

    return total, reasons


def summarize(reasons: list[str]) -> str:
    if not reasons:
        return "no shared background found"
    return "; ".join(reasons)
