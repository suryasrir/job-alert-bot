"""Public Telegram channel posting and per-source fetch intervals."""

import sqlite3

from conftest import make_job, recent
from test_notifications import FakeNotifier, StaticSource

from job_agent.config import Settings
from job_agent.database import MIGRATIONS, Database
from job_agent.matching import score_job
from job_agent.notifications import format_public_post
from job_agent.pipeline import run


def _settings(**kw):
    s = Settings(db_path=":memory:", telegram_channel_id="@test_channel", post_delay_seconds=0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_public_post_has_no_personal_details(profile):
    job = score_job(make_job(description="Requirements: 8+ years of experience with Swift, UIKit."), profile)
    text = format_public_post(job, footer="Join @test_channel")
    assert "iOS Developer" in text and "Acme" in text
    assert "/100" not in text  # no personal score
    assert "you have" not in text.lower()  # no personal concerns
    assert "8+ years" in text  # the employer's requirement is a public fact
    assert "#iOS" in text and "#Hyderabad" in text
    assert "Join @test_channel" in text


def test_public_post_credits_source(profile):
    job = score_job(make_job(source="remotive", url="https://remotive.com/remote-jobs/1"), profile)
    assert "Remotive" in format_public_post(job)


def test_public_post_escapes_html(profile):
    job = score_job(make_job(title="<b>iOS</b> & Swift Dev"), profile)
    text = format_public_post(job)
    assert "<b><b>" not in text and "&lt;b&gt;iOS&lt;/b&gt; &amp; Swift" in text


def test_public_ok_rules(profile):
    assert score_job(make_job(), profile).public_ok
    # Experience gap does not exclude a job from the public channel
    assert score_job(make_job(description="Requirements: 9+ years of iOS experience"), profile).public_ok
    # Not India-eligible
    assert not score_job(make_job(location="Remote - USA", city=None, country=None, work_arrangement="remote"), profile).public_ok
    # Not a mobile role
    assert not score_job(make_job(title="Data Scientist"), profile).public_ok
    # Generic title is not enough
    assert not score_job(make_job(title="Software Engineer", description="We use Swift and iOS"), profile).public_ok


def test_channel_posts_once_and_respects_limit(db, profile):
    jobs = [make_job(source_job_id=str(i), url=f"https://e.com/{i}") for i in range(4)]
    channel = FakeNotifier()
    src = StaticSource(jobs)
    r1 = run(profile, _settings(channel_max_posts=3), {}, db, adapters=[src], notifiers=[], channel_notifier=channel)
    assert r1.channel_posted == 3
    r2 = run(profile, _settings(channel_max_posts=3), {}, db, adapters=[src], notifiers=[], channel_notifier=channel)
    assert r2.channel_posted == 1
    r3 = run(profile, _settings(channel_max_posts=3), {}, db, adapters=[src], notifiers=[], channel_notifier=channel)
    assert r3.channel_posted == 0
    assert len(channel.sent) == 4


def test_channel_failure_retried(db, profile):
    src = StaticSource([make_job()])
    r1 = run(profile, _settings(), {}, db, adapters=[src], notifiers=[], channel_notifier=FakeNotifier(ok=False))
    assert r1.channel_posted == 0 and any("channel admin" in e for e in r1.errors)
    r2 = run(profile, _settings(), {}, db, adapters=[src], notifiers=[], channel_notifier=FakeNotifier())
    assert r2.channel_posted == 1


def test_old_jobs_not_posted(db, profile):
    src = StaticSource([make_job(posted_at=recent(10))])
    channel = FakeNotifier()
    run(profile, _settings(channel_max_age_days=3), {}, db, adapters=[src], notifiers=[], channel_notifier=channel)
    assert channel.sent == []


def test_dry_run_does_not_post(db, profile):
    channel = FakeNotifier()
    r = run(profile, _settings(dry_run=True), {}, db, adapters=[StaticSource([make_job()])], notifiers=[],
            channel_notifier=channel)
    assert channel.sent == [] and any("channel post" in n for n in r.notes)


def test_no_channel_configured_does_nothing(db, profile):
    r = run(profile, Settings(db_path=":memory:"), {}, db, adapters=[StaticSource([make_job()])], notifiers=[])
    assert r.channel_posted == 0


def test_min_interval_skips_recent_source(db, profile):
    src = StaticSource([make_job()])
    src.config["min_interval_hours"] = 6
    r1 = run(profile, Settings(db_path=":memory:"), {}, db, adapters=[src], notifiers=[])
    assert r1.fetched == 1
    r2 = run(profile, Settings(db_path=":memory:"), {}, db, adapters=[src], notifiers=[])
    assert r2.fetched == 0 and any("fetched recently" in s for s in r2.skipped_sources)
    # A skipped source must not cause its jobs to be marked closed
    assert db.get(make_job().compute_id())["status"] == "discovered"


def test_hourly_source_not_skipped(db, profile):
    src = StaticSource([make_job()])
    src.config["min_interval_hours"] = 1
    db.set_source_fetched("greenhouse")
    db.conn.execute("UPDATE source_state SET last_fetched_at = datetime('now', '-50 minutes') || '+00:00'")
    r = run(profile, Settings(db_path=":memory:"), {}, db, adapters=[src], notifiers=[])
    assert r.fetched == 1  # late cron starts are tolerated


def test_migration_from_v1_keeps_data(tmp_path, profile):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0])
    conn.execute(
        "INSERT INTO jobs (id, source, title, company, url, canonical_url, first_seen_at, last_seen_at) "
        "VALUES ('x', 'lever', 'iOS Dev', 'A', 'https://a', 'https://a', '2026-01-01', '2026-01-01')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    d = Database(path)
    assert d.schema_version() == len(MIGRATIONS)
    row = d.get("x")
    assert row["title"] == "iOS Dev" and row["public_ok"] == 0 and row["channel_posted_at"] is None
    d.close()


def test_public_title_rules(profile):
    ok = ["iOS Developer", "Senior Mobile APP (IOS/AOS) Developer", "React Native Engineer",
          "Mobile Software Engineer", "SwiftUI Developer", "Mobile App Developer"]
    bad = ["Solution Manager - Custom Web & Mobile Solutions", "Java Application Developer",
           "Engineering Manager, Mobile", "iOS QA Engineer", "Mobile Product Designer"]
    for t in ok:
        assert score_job(make_job(title=t), profile).public_ok, t
    for t in bad:
        assert not score_job(make_job(title=t), profile).public_ok, t
