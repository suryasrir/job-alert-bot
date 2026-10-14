import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_agent.config import Profile, load_profile  # noqa: E402
from job_agent.database import Database  # noqa: E402
from job_agent.models import Job  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "ADZUNA_APP_ID",
                "ADZUNA_APP_KEY", "CANDIDATE_PROFILE_JSON", "SMTP_HOST", "APPLICATION_MODE"):
        monkeypatch.delenv(var, raising=False)
    # Never sleep during retry tests.
    monkeypatch.setattr("job_agent.http.time.sleep", lambda *_: None)


@pytest.fixture
def profile() -> Profile:
    # A fictional test candidate; values are set here so tests don't depend on the sample profile.
    p, _ = load_profile(str(ROOT / "config" / "profile.example.json"))
    p.total_experience_years = 3
    p.ios_experience_years = 2
    p.react_native_experience_years = 1
    p.current_ctc_lpa = 8
    p.expected_ctc_lpa = 12
    p.notice_period_days = 30
    p.preferred_locations = ["Hyderabad", "Remote"]
    p.willing_to_relocate_to = ["Hyderabad"]
    return p


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def recent(days: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def make_job(**overrides) -> Job:
    base = dict(
        source="greenhouse",
        source_job_id="123",
        board="acme",
        title="iOS Developer",
        company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/123",
        description="Requirements:\n3+ years of experience with Swift, UIKit and SwiftUI. REST APIs, Core Data, Git.",
        location="Hyderabad, India",
        city="Hyderabad",
        country="India",
        work_arrangement="hybrid",
        posted_at=recent(1),
    )
    base.update(overrides)
    return Job(**base)
