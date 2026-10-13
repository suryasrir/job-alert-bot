"""Discovery pipeline: fetch → normalise → score → store → (AI) → notify."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import ai_analysis
from .config import Profile, Settings, enforce_application_mode
from .database import Database, row_to_job
from .matching import score_job, score_title
from .models import Job
from .notifications import Notifier, TelegramNotifier, available_notifiers, notify_jobs, post_to_channel
from .sources import REGISTRY, SourceAdapter
from .text import normalize_key, now_iso

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    started_at: str = field(default_factory=now_iso)
    fetched: int = 0
    stored: int = 0
    new: int = 0
    skipped_irrelevant: int = 0
    closed: int = 0
    expired: int = 0
    notified: int = 0
    channel_posted: int = 0
    ai_analyzed: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)
    top: list[Job] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_adapters(sources_cfg: dict, only: Optional[list[str]] = None) -> list[SourceAdapter]:
    adapters = []
    for name, cls in REGISTRY.items():
        cfg = sources_cfg.get(name)
        if cfg is None or (only and name not in only):
            continue
        adapter = cls(cfg)
        if adapter.enabled or (only and name in only):
            adapters.append(adapter)
    return adapters


def _worth_storing(job: Job, profile: Profile) -> bool:
    title_frac, _ = score_title(job, profile)
    return job.public_ok or title_frac > 0 or (job.score or 0) >= profile.thresholds["store_min"]


def run(
    profile: Profile,
    settings: Settings,
    sources_cfg: dict,
    db: Database,
    adapters: Optional[list[SourceAdapter]] = None,
    notifiers: Optional[list[Notifier]] = None,
    ai_provider: Optional[ai_analysis.LLMProvider] = None,
    channel_notifier: Optional[TelegramNotifier] = None,
) -> RunReport:
    enforce_application_mode(settings.application_mode)
    report = RunReport()
    adapters = adapters if adapters is not None else build_adapters(sources_cfg)

    for adapter in adapters:
        reason = adapter.missing_credentials()
        if reason:
            report.skipped_sources.append(f"{adapter.name}: {reason}")
            continue
        wait = _interval_remaining(db, adapter)
        if wait:
            report.skipped_sources.append(f"{adapter.name}: fetched recently; next fetch in about {wait}")
            continue
        try:
            result = adapter.fetch()
        except Exception as exc:  # one broken adapter must never stop the run
            report.errors.append(f"{adapter.name}: unexpected {type(exc).__name__}")
            log.exception("Source %s crashed", adapter.name)
            continue
        db.set_source_fetched(adapter.name)
        report.errors.extend(result.errors)
        report.fetched += len(result.jobs)
        report.per_source[adapter.name] = len(result.jobs)

        seen_by_board: dict[str, set[str]] = {}
        for job in result.jobs:
            try:
                score_job(job, profile)
            except Exception as exc:  # one odd record must never stop the run
                report.errors.append(f"{adapter.name}: could not score '{job.title[:40]}' ({type(exc).__name__})")
                continue
            if not _worth_storing(job, profile):
                report.skipped_irrelevant += 1
                # Still record existing jobs as seen so they are not marked closed.
                existing = db.find_existing(job)
                if existing:
                    seen_by_board.setdefault(job.board or "", set()).add(existing["id"])
                continue
            job_id, is_new = db.upsert(job)
            seen_by_board.setdefault(job.board or "", set()).add(job_id)
            report.stored += 1
            report.new += int(is_new)

        if adapter.complete_listing:
            for board in result.complete_boards:
                report.closed += db.mark_missing_closed(adapter.name, board, seen_by_board.get(board, set()))

    report.expired = db.expire_past_deadlines()

    since = (datetime.now(timezone.utc) - timedelta(days=settings.max_job_age_days)).isoformat(timespec="seconds")
    pending = [row_to_job(r) for r in db.pending_notifications(profile.thresholds["notify_min"], since)]
    report.top = pending[:10]

    # Optional AI analysis of the best new candidates.
    ai_summaries: dict[str, str] = {}
    if ai_provider is not None and not settings.dry_run:
        for job in pending[: settings.ai_max_jobs_per_run]:
            row = db.get(job.id)
            if row and row["ai_analysis"]:
                continue
            try:
                analysis = ai_analysis.analyze_job(job, profile, ai_provider)
            except ai_analysis.AIError as exc:
                report.errors.append(f"ai:{job.id}: {exc}")
                continue
            db.save_ai_analysis(job.id, analysis)
            ai_summaries[job.id] = f"{analysis['recommendation'].upper()}: {analysis['fit_explanation']}"
            report.ai_analyzed += 1

    if settings.dry_run:
        report.notes.append(f"Dry run: {len(pending)} alert(s) would be sent")
    elif pending:
        notifiers = notifiers if notifiers is not None else available_notifiers()
        if not notifiers:
            report.notes.append(
                "No notification channel configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID). "
                "Jobs were saved; alerts will be sent once a channel is configured."
            )
        else:
            delivered, channels = notify_jobs(pending, notifiers, settings.max_notifications, ai_summaries)
            db.mark_notified(delivered, channels)
            report.notified = len(delivered)
            if len(delivered) < len(pending):
                report.errors.append(f"notifications: {len(pending) - len(delivered)} not confirmed; will retry next run")

    _post_channel(db, settings, report, channel_notifier)

    db.record_run(report.started_at, report.fetched, report.new, report.notified, report.errors)
    return report


def _interval_remaining(db: Database, adapter: SourceAdapter) -> Optional[str]:
    """Return a human-readable wait if the source's `min_interval_hours` has not passed, else None.

    Lets busy sources run hourly while rate-limited ones (Remotive, Adzuna) run less often.
    A 15-minute tolerance absorbs GitHub's late cron starts.
    """
    hours = float(adapter.config.get("min_interval_hours", 0) or 0)
    last = db.source_last_fetched(adapter.name)
    if hours <= 0 or not last:
        return None
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    remaining = timedelta(hours=hours) - elapsed - timedelta(minutes=15)
    if remaining <= timedelta(0):
        return None
    mins = int(remaining.total_seconds() // 60) + 15
    return f"{mins // 60}h {mins % 60}m"


def _post_channel(db: Database, settings: Settings, report: RunReport,
                  notifier: Optional[TelegramNotifier]) -> None:
    if not settings.telegram_channel_id and notifier is None:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=settings.channel_max_age_days)).isoformat(timespec="seconds")
    # Same company + same title posted recently (or twice in this batch) looks like spam: retire repeats.
    seen = db.recently_posted_titles()
    pending: list[Job] = []
    for job in (row_to_job(r) for r in db.pending_channel_posts(since)):
        key = (normalize_key(job.company), normalize_key(job.title))
        if key in seen:
            if not settings.dry_run:
                db.mark_channel_posted(job.id)
            continue
        seen.add(key)
        pending.append(job)
    batch = pending[: settings.channel_max_posts]
    if settings.dry_run:
        report.notes.append(f"Dry run: {len(batch)} channel post(s) would be published ({len(pending)} waiting)")
        return
    notifier = notifier or TelegramNotifier(chat_id=settings.telegram_channel_id)
    if not notifier.configured():
        report.notes.append("TELEGRAM_CHANNEL_ID is set but TELEGRAM_BOT_TOKEN is missing; channel posting skipped")
        return
    posted = post_to_channel(batch, notifier, settings.channel_footer, settings.post_delay_seconds)
    for job_id in posted:
        db.mark_channel_posted(job_id)
    report.channel_posted = len(posted)
    if len(posted) < len(batch):
        report.errors.append(
            f"channel: {len(batch) - len(posted)} post(s) not confirmed; check the bot is a channel admin. Will retry."
        )
    if len(pending) > len(batch):
        report.notes.append(f"{len(pending) - len(batch)} more channel post(s) queued for the next runs")


def format_report(report: RunReport) -> str:
    lines = [
        "## Job alert run",
        f"- Fetched: {report.fetched} ({', '.join(f'{k}: {v}' for k, v in report.per_source.items()) or 'none'})",
        f"- Stored/updated relevant jobs: {report.stored} (new: {report.new}); skipped as irrelevant: {report.skipped_irrelevant}",
        f"- Marked closed: {report.closed}; expired: {report.expired}",
        f"- Personal alerts sent: {report.notified}; channel posts: {report.channel_posted}; AI analyses: {report.ai_analyzed}",
    ]
    for note in report.notes:
        lines.append(f"- {note}")
    if report.skipped_sources:
        lines.append("\n### Skipped sources")
        lines += [f"- {s}" for s in report.skipped_sources]
    if report.errors:
        lines.append("\n### Source warnings")
        lines += [f"- {e}" for e in report.errors[:40]]
    if report.top:
        lines.append("\n### Top pending matches")
        lines.append("| Score | Title | Company | Location |")
        lines.append("|---|---|---|---|")
        for j in report.top:
            esc = lambda s: str(s or "").replace("|", "/")
            lines.append(f"| {j.score:g} | {esc(j.title)} | {esc(j.company)} | {esc(j.location)} |")
    return "\n".join(lines)
