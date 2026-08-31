"""Settings loaded from environment. Nothing secret is ever written to the DB."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "outreach.db"))
BANKS_CSV = Path(os.getenv("BANKS_CSV", ROOT / "config" / "banks.csv"))
AFFINITY_YML = Path(os.getenv("AFFINITY_YML", ROOT / "config" / "affinity.yml"))


def _split_keys(raw: str | None) -> list[str]:
    """Accept one key or several, comma- or newline-separated."""
    if not raw:
        return []
    parts = raw.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


@dataclass
class LLMConfig:
    provider: str = os.getenv("LLM_PROVIDER", "anthropic")
    model: str = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    keys: list[str] = field(default_factory=lambda: _split_keys(
        os.getenv("ANTHROPIC_API_KEYS")
        or os.getenv("OPENAI_API_KEYS")
        or os.getenv("GOOGLE_API_KEYS")
        or os.getenv("OPENROUTER_API_KEYS")
        or os.getenv("LLM_API_KEYS")
    ))
    base_url: str | None = os.getenv("LLM_BASE_URL") or None


@dataclass
class Settings:
    apollo_keys: list[str] = field(
        default_factory=lambda: _split_keys(os.getenv("APOLLO_API_KEYS"))
    )
    apollo_base: str = os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1")
    llm: LLMConfig = field(default_factory=LLMConfig)

    # Volume control. Keep drafts_per_day low — deliverability matters more than reach.
    drafts_per_day: int = int(os.getenv("DRAFTS_PER_DAY", "12"))
    enrich_batch: int = int(os.getenv("ENRICH_BATCH", "10"))
    search_per_bank: int = int(os.getenv("SEARCH_PER_BANK", "25"))
    min_affinity: int = int(os.getenv("MIN_AFFINITY", "1"))

    target_titles: list[str] = field(default_factory=lambda: _split_keys(
        os.getenv("TARGET_TITLES", "Managing Director,Vice President,Associate")
    ))
    target_seniorities: list[str] = field(default_factory=lambda: _split_keys(
        os.getenv("TARGET_SENIORITIES", "director,vp,senior,manager")
    ))

    sender_name: str = os.getenv("SENDER_NAME", "")
    sender_blurb: str = os.getenv("SENDER_BLURB", "")
    sender_email: str = os.getenv("SENDER_EMAIL", "")

    gmail_client_id: str = os.getenv("GMAIL_CLIENT_ID", "")
    gmail_client_secret: str = os.getenv("GMAIL_CLIENT_SECRET", "")
    gmail_refresh_token: str = os.getenv("GMAIL_REFRESH_TOKEN", "")

    dry_run: bool = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes"}

    def affinity_rules(self) -> dict:
        if AFFINITY_YML.exists():
            return yaml.safe_load(AFFINITY_YML.read_text()) or {}
        return {}

    def missing(self) -> list[str]:
        """What still needs filling in before a real run can work."""
        gaps = []
        if not self.apollo_keys:
            gaps.append("APOLLO_API_KEYS")
        if not self.llm.keys:
            gaps.append(f"{self.llm.provider.upper()} API key")
        if not self.gmail_refresh_token:
            gaps.append("GMAIL_REFRESH_TOKEN")
        if not self.sender_name:
            gaps.append("SENDER_NAME")
        return gaps


settings = Settings()
