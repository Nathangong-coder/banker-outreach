"""Turn a scored contact into a subject line and a short email."""
from __future__ import annotations

import json

from .config import settings
from .matching import summarize

SYSTEM = """You write short cold outreach emails from a student or early-career \
person to someone senior in investment banking. You are writing as the sender, \
in first person.

Rules that matter more than anything else:
- Under 120 words in the body. Bankers read on a phone between meetings.
- Open with the specific reason you're writing to THIS person. Never open with \
"I hope this email finds you well" or any variant.
- One clear ask, and make it cheap to say yes to: 15 minutes, a specific week.
- Shared background is a reason to reach out, not the whole email. Mention it \
once, briefly, then move on to what you actually want to learn.
- No flattery about the bank's "prestigious reputation". No adjective stacking.
- Plain sentences. Do not use em-dashes.
- If the shared-background field says none was found, do not invent one. Lead \
with the group or coverage area instead.

Return ONLY a JSON object, no markdown fence:
{"subject": "...", "body": "..."}

The subject is under 8 words, lowercase or sentence case, and reads like a \
person wrote it, not a marketing team. The body ends with the sender's first \
name on its own line."""


def build_user_prompt(contact: dict, sender_name: str, sender_blurb: str) -> str:
    reasons = contact.get("affinity_notes") or []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except json.JSONDecodeError:
            reasons = [reasons]

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


def draft_for(llm, contact: dict) -> tuple[str, str]:
    user = build_user_prompt(contact, settings.sender_name, settings.sender_blurb)
    result = llm.complete_json(SYSTEM, user, max_tokens=900)
    subject = (result.get("subject") or "").strip()
    body = (result.get("body") or "").strip()
    if not subject or not body:
        raise ValueError(f"Incomplete draft returned: {result}")
    return subject, body
