"""Normalized job record shared by every source adapter."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Job lifecycle statuses. `applied_by_user` is only ever set by an explicit
# user command, never by the pipeline.
STATUSES = (
    "discovered",
    "shortlisted",
    "dismissed",
    "application_prepared",
    "applied_by_user",
    "closed",
    "expired",
)

WORK_ARRANGEMENTS = ("remote", "hybrid", "onsite", "unknown")


@dataclass
class Job:
    source: str
    title: str
    company: str
    url: str
    source_job_id: Optional[str] = None
    board: Optional[str] = None  # e.g. greenhouse board token; used for closed-job detection
    description: str = ""
    required_skills: list[str] = field(default_factory=list)
    experience_text: Optional[str] = None
    employment_type: Optional[str] = None
    work_arrangement: str = "unknown"
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None  # "year" | "month" | "hour"
    salary_text: Optional[str] = None
    apply_url: Optional[str] = None
    posted_at: Optional[str] = None  # ISO-8601 UTC
    closing_date: Optional[str] = None

    # Filled in by the pipeline / database
    id: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    status: str = "discovered"
    score: Optional[float] = None
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    match_category: Optional[str] = None
    match_reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Return a list of validation problems (empty means valid)."""
        problems = []
        if not (self.source or "").strip():
            problems.append("missing source")
        if not (self.title or "").strip():
            problems.append("missing title")
        if not (self.company or "").strip():
            problems.append("missing company")
        if not (self.url or "").startswith(("https://", "http://")):
            problems.append("missing or non-http url")
        if self.work_arrangement not in WORK_ARRANGEMENTS:
            problems.append(f"invalid work_arrangement {self.work_arrangement!r}")
        if self.status not in STATUSES:
            problems.append(f"invalid status {self.status!r}")
        return problems

    def compute_id(self) -> str:
        """Stable internal ID. Prefers the source's own ID, falls back to the URL."""
        basis = f"{self.source}|{self.source_job_id}" if self.source_job_id else f"{self.source}|url|{self.url}"
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
