from unittest.mock import MagicMock

from conftest import make_job

from job_agent.config import Settings
from job_agent.matching import score_job
from job_agent.notifications import TelegramNotifier, format_job, notify_jobs
from job_agent.pipeline import run
from job_agent.sources.base import FetchResult, SourceAdapter


class FakeNotifier:
    name = "telegram"

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def configured(self):
        return True

    def send(self, text):
        self.sent.append(text)
        return self.ok


class StaticSource(SourceAdapter):
    name = "greenhouse"
    complete_listing = True

    def __init__(self, jobs):
        super().__init__({"enabled": True})
        self._jobs = jobs

    def fetch(self):
        return FetchResult(jobs=[j for j in self._jobs], complete_boards={"acme"})


def _settings(**kw):
    s = Settings(db_path=":memory:")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_missing_telegram_credentials_does_not_break_discovery(db, profile):
    report = run(profile, _settings(), {}, db, adapters=[StaticSource([make_job()])], notifiers=[])
    assert report.stored == 1
    assert report.notified == 0
    assert any("No notification channel" in n for n in report.notes)
    assert db.get(make_job().compute_id())["notified_at"] is None


def test_telegram_not_configured_without_env():
    assert not TelegramNotifier().configured()


def test_alerts_are_not_repeated(db, profile):
    notifier = FakeNotifier()
    src = StaticSource([make_job()])
    run(profile, _settings(), {}, db, adapters=[src], notifiers=[notifier])
    run(profile, _settings(), {}, db, adapters=[src], notifiers=[notifier])
    assert len(notifier.sent) == 1


def test_failed_delivery_is_retried_next_run(db, profile):
    src = StaticSource([make_job()])
    failing = FakeNotifier(ok=False)
    report = run(profile, _settings(), {}, db, adapters=[src], notifiers=[failing])
    assert report.notified == 0
    working = FakeNotifier()
    report2 = run(profile, _settings(), {}, db, adapters=[src], notifiers=[working])
    assert report2.notified == 1


def test_dry_run_sends_nothing(db, profile):
    notifier = FakeNotifier()
    run(profile, _settings(dry_run=True), {}, db, adapters=[StaticSource([make_job()])], notifiers=[notifier])
    assert notifier.sent == []


def test_max_notifications_and_summary(profile):
    jobs = [score_job(make_job(source_job_id=str(i), url=f"https://e.com/{i}"), profile) for i in range(5)]
    for j in jobs:
        j.id = j.compute_id()
    notifier = FakeNotifier()
    delivered, channels = notify_jobs(jobs, [notifier], max_individual=2)
    assert len(notifier.sent) == 3  # 2 individual + 1 summary
    assert len(delivered) == 5 and channels == "telegram"


def test_format_escapes_untrusted_html(profile):
    job = score_job(make_job(title="<script>alert(1)</script> iOS Dev", company="A & B"), profile)
    job.id = "abc"
    text = format_job(job)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "A &amp; B" in text
    assert "https://boards.greenhouse.io/acme/jobs/123" in text


def test_telegram_requires_ok_true(monkeypatch):
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "description": "chat not found"}
    monkeypatch.setattr("job_agent.notifications.request", lambda *a, **k: resp)
    assert TelegramNotifier("t0ken123", "42").send("hi") is False
    resp.json.return_value = {"ok": True}
    assert TelegramNotifier("t0ken123", "42").send("hi") is True


def test_telegram_error_does_not_leak_token(monkeypatch, caplog):
    from job_agent.http import FetchError

    def boom(*a, **k):
        raise FetchError("POST https://api.telegram.org/botSECRET-TOKEN-1/sendMessage failed: HTTP 401")

    monkeypatch.setattr("job_agent.notifications.request", boom)
    assert TelegramNotifier("SECRET-TOKEN-1", "42").send("hi") is False
    assert "SECRET-TOKEN-1" not in caplog.text


def test_low_score_and_mismatch_not_notified(db, profile):
    notifier = FakeNotifier()
    jobs = [
        make_job(source_job_id="1", url="https://e.com/1", description="Requirements: 9+ years experience, Swift"),
        make_job(source_job_id="2", url="https://e.com/2", title="Accountant", description="Tally"),
    ]
    run(profile, _settings(), {}, db, adapters=[StaticSource(jobs)], notifiers=[notifier])
    assert notifier.sent == []
