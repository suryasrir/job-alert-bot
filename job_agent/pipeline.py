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
from .notifications import Notifier, available_notifiers, notify_jobs
from .sources import REGISTRY, SourceAdapter
from .text import now_iso

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
    return title_frac > 0 or (job.score or 0) >= profile.thresholds["store_min"]


def run(
    profile: Profile,
    settings: Settings,
    sources_cfg: dict,
    db: Database,
    adapters: Optional[list[SourceAdapter]] = None,
    notifiers: Optional[list[Notifier]] = None,
    ai_provider: Optional[ai_analysis.LLMProvider] = None,
) -> RunReport:
    enforce_application_mode(settings.application_mode)
    report = RunReport()
    adapters = adapters if adapters is not None else build_adapters(sources_cfg)

    for adapter in adapters:
        reason = adapter.missing_credentials()
        if reason:
            report.skipped_sources.append(f"{adapter.name}: {reason}")
            continue
        try:
            result = adapter.fetch()
        except Exception as exc:  # one broken adapter must never stop the run
            report.errors.append(f"{adapter.name}: unexpected {type(exc).__name__}")
            log.exception("Source %s crashed", adapter.name)
            continue
        report.errors.extend(result.errors)
        report.fetched += len(result.jobs)
        report.per_source[adapter.name] = len(result.jobs)

        seen_by_board: dict[str, set[str]] = {}
        for job in result.jobs:
            score_job(job, profile)
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

    db.record_run(report.started_at, report.fetched, report.new, report.notified, report.errors)
    return report


def format_report(report: RunReport) -> str:
    lines = [
        "## Job alert run",
        f"- Fetched: {report.fetched} ({', '.join(f'{k}: {v}' for k, v in report.per_source.items()) or 'none'})",
        f"- Stored/updated relevant jobs: {report.stored} (new: {report.new}); skipped as irrelevant: {report.skipped_irrelevant}",
        f"- Marked closed: {report.closed}; expired: {report.expired}",
        f"- Alerts sent: {report.notified}; AI analyses: {report.ai_analyzed}",
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
