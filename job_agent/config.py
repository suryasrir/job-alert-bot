"""Configuration: candidate profile, source list and runtime settings.

Profile lookup order (first found wins):
  1. CANDIDATE_PROFILE_JSON environment variable (a GitHub Actions secret)
  2. config/profile.json (local, git-ignored)
  3. config/profile.example.json (safe public sample)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

DEFAULT_WEIGHTS = {
    "title": 25,
    "skills": 30,
    "experience": 15,
    "location": 15,
    "salary": 10,
    "recency": 5,
}

DEFAULT_THRESHOLDS = {
    "strong": 75,
    "good": 60,
    "possible": 45,
    "notify_min": 60,  # minimum score to send an alert
    "store_min": 30,   # jobs below this (and with no mobile title) are not stored
}


@dataclass
class Profile:
    name: str = "Candidate"
    total_experience_years: float = 0.0
    ios_experience_years: float = 0.0
    react_native_experience_years: float = 0.0
    current_ctc_lpa: Optional[float] = None
    expected_ctc_lpa: Optional[float] = None
    notice_period_days: Optional[int] = None
    preferred_locations: list[str] = field(default_factory=lambda: ["Hyderabad", "Remote"])
    open_to_other_india_locations: bool = True
    willing_to_relocate_to: list[str] = field(default_factory=list)
    remote_ok: bool = True
    preferred_titles: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skill_synonyms: dict[str, list[str]] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    # Free-form factual background used only for application preparation.
    summary: str = ""
    experience: list[dict[str, Any]] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    contact: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            log.warning("Ignoring unknown profile fields: %s", ", ".join(sorted(unknown)))
        p = cls(**{k: v for k, v in data.items() if k in known})
        p.weights = {**DEFAULT_WEIGHTS, **(data.get("weights") or {})}
        p.thresholds = {**DEFAULT_THRESHOLDS, **(data.get("thresholds") or {})}
        if sum(p.weights.values()) <= 0:
            raise ValueError("profile weights must sum to a positive number")
        return p


def load_profile(path: Optional[str] = None) -> tuple[Profile, str]:
    """Return (profile, description_of_where_it_came_from)."""
    if path:
        return Profile.from_dict(json.loads(Path(path).read_text())), path
    env = os.environ.get("CANDIDATE_PROFILE_JSON", "").strip()
    if env:
        try:
            return Profile.from_dict(json.loads(env)), "CANDIDATE_PROFILE_JSON secret"
        except (ValueError, TypeError) as exc:
            # Never echo the secret's content.
            raise ValueError("CANDIDATE_PROFILE_JSON is not valid profile JSON") from exc
    for candidate in (CONFIG_DIR / "profile.json", CONFIG_DIR / "profile.example.json"):
        if candidate.exists():
            return Profile.from_dict(json.loads(candidate.read_text())), str(candidate.relative_to(ROOT))
    raise FileNotFoundError("No candidate profile found (expected config/profile.json)")


def load_sources_config(path: Optional[str] = None) -> dict[str, Any]:
    p = Path(path) if path else CONFIG_DIR / "sources.json"
    return json.loads(p.read_text())


@dataclass
class Settings:
    db_path: str = str(ROOT / "data" / "jobs.db")
    dry_run: bool = False
    max_notifications: int = 10
    max_job_age_days: int = 30  # ignore postings older than this when alerting
    application_mode: str = "approval_required"
    ai_enabled: bool = False
    ai_max_jobs_per_run: int = 5
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    # Public channel (e.g. "@my_ios_jobs" or "-1001234567890"); the bot must be a channel admin.
    telegram_channel_id: Optional[str] = None
    channel_max_posts: int = 5      # per run; hourly runs → up to 120 posts/day
    channel_max_age_days: int = 3   # only post fresh jobs
    channel_footer: str = ""        # e.g. "Join @my_ios_jobs for more"
    post_delay_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "Settings":
        def env_int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except ValueError:
                log.warning("%s is not an integer; using %s", name, default)
                return default

        return cls(
            db_path=os.environ.get("JOB_DB_PATH", cls.db_path),
            dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
            max_notifications=env_int("MAX_NOTIFICATIONS", 10),
            max_job_age_days=env_int("MAX_JOB_AGE_DAYS", 30),
            application_mode=os.environ.get("APPLICATION_MODE", "approval_required"),
            ai_enabled=bool(os.environ.get("OPENAI_API_KEY"))
            and os.environ.get("AI_ENABLED", "true").lower() not in ("0", "false", "no"),
            ai_max_jobs_per_run=env_int("AI_MAX_JOBS_PER_RUN", 5),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
            telegram_channel_id=os.environ.get("TELEGRAM_CHANNEL_ID") or None,
            channel_max_posts=env_int("CHANNEL_MAX_POSTS", 5),
            channel_max_age_days=env_int("CHANNEL_MAX_AGE_DAYS", 3),
            channel_footer=os.environ.get("CHANNEL_FOOTER", ""),
        )


def enforce_application_mode(mode: str) -> None:
    """Only `approval_required` is supported. Automatic submission is not implemented."""
    if mode != "approval_required":
        raise ValueError(
            f"APPLICATION_MODE={mode!r} is not supported. This bot never submits applications; "
            "only 'approval_required' is allowed."
        )
