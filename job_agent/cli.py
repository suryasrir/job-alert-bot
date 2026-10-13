"""Command-line interface. Run `python jobs.py --help`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from . import ai_analysis
from .application_prep import prepare_application
from .config import Settings, load_profile, load_sources_config
from .database import Database, row_to_job
from .matching import explain
from .models import STATUSES
from .pipeline import build_adapters, format_report, run
from .sources import MANUAL_SEARCH_PORTALS, REGISTRY

log = logging.getLogger("job_agent")


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader for local runs (does not override real env vars)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@contextmanager
def run_lock(db_path: str):
    """Prevent two local runs from writing the same database at once."""
    lock_path = Path(db_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows: rely on SQLite's own locking
        yield
        return
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Another run is already in progress; exiting.")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def cmd_run(args, settings: Settings) -> int:
    profile, origin = load_profile(args.profile)
    log.info("Using candidate profile from %s", origin)
    if args.dry_run:
        settings.dry_run = True
    sources_cfg = load_sources_config(args.sources)
    adapters = build_adapters(sources_cfg, args.source or None)
    provider = ai_analysis.get_provider() if settings.ai_enabled else None
    with run_lock(settings.db_path):
        db = Database(settings.db_path)
        try:
            report = run(profile, settings, sources_cfg, db, adapters=adapters, ai_provider=provider)
        finally:
            db.close()
    text = format_report(report)
    print(text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as fh:
            fh.write(text + "\n")
    # Fail the run only when *every* source failed to return anything.
    if adapters and report.fetched == 0 and report.errors:
        log.error("All sources failed")
        return 1
    return 0


def cmd_list(args, settings: Settings) -> int:
    db = Database(settings.db_path)
    rows = db.list_jobs(status=args.status, min_score=args.min_score, limit=args.limit)
    if not rows:
        print("No jobs found. Run `python jobs.py run` first.")
    for r in rows:
        print(f"{r['id']}  {r['score']:5.1f}  {r['status']:<20} {r['title'][:50]:<50}  {r['company'][:22]:<22}  {r['location'] or ''}")
    return 0


def cmd_show(args, settings: Settings) -> int:
    db = Database(settings.db_path)
    row = db.get(args.job_id)
    if not row:
        print("Job not found")
        return 1
    job = row_to_job(row)
    print(f"{job.title} — {job.company}\n{job.location or 'location not stated'} ({job.work_arrangement})")
    print(f"Status: {job.status} | Source: {job.source} | Posted: {job.posted_at or 'unknown'}")
    print(f"Link: {job.apply_url or job.url}\n")
    print(explain(job))
    print("Score breakdown:", json.dumps(job.score_breakdown))
    if row["ai_analysis"]:
        print("\nAI analysis (unverified):", json.dumps(json.loads(row["ai_analysis"]), indent=1))
    if args.full:
        print("\n" + (job.description or "(no description)"))
    return 0


def cmd_prepare(args, settings: Settings) -> int:
    profile, _ = load_profile(args.profile)
    db = Database(settings.db_path)
    row = db.get(args.job_id)
    if not row:
        print("Job not found")
        return 1
    job = row_to_job(row)
    provider = ai_analysis.get_provider() if (settings.ai_enabled and not args.no_ai) else None
    path = prepare_application(job, profile, settings.application_mode, provider)
    db.mark_application_prepared(job.id)
    print(f"Application pack written to {path}\nReview it, apply yourself on the employer's site, then run:\n"
          f"  python jobs.py mark {job.id} applied_by_user --confirm")
    return 0


def cmd_mark(args, settings: Settings) -> int:
    if args.status == "applied_by_user" and not args.confirm:
        print("Use --confirm to record that YOU submitted this application.")
        return 1
    db = Database(settings.db_path)
    row = db.get(args.job_id)
    if not row:
        print("Job not found")
        return 1
    db.set_status(row["id"], args.status, args.note)
    print(f"{row['id']} → {args.status}")
    return 0


def cmd_stats(args, settings: Settings) -> int:
    db = Database(settings.db_path)
    print(json.dumps(db.stats(), indent=1))
    return 0


def cmd_links(args, settings: Settings) -> int:
    print("These portals have no permitted public API. Open their searches manually or set up their own email alerts:")
    for name, url in MANUAL_SEARCH_PORTALS.items():
        print(f"  {name:<13} {url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobs.py", description="AI-assisted job alert bot")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--profile", help="path to a profile JSON (default: config/profile.json)")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="discover, score, store and notify")
    r.add_argument("--dry-run", action="store_true", help="do not send notifications or call AI")
    r.add_argument("--source", action="append", choices=sorted(REGISTRY), help="only run these sources")
    r.add_argument("--sources", help="path to sources config (default: config/sources.json)")

    l = sub.add_parser("list", help="list stored jobs by score")
    l.add_argument("--status", choices=STATUSES)
    l.add_argument("--min-score", type=float, default=0)
    l.add_argument("--limit", type=int, default=30)

    s = sub.add_parser("show", help="show one job with its score explanation")
    s.add_argument("job_id")
    s.add_argument("--full", action="store_true", help="print the full description")

    pr = sub.add_parser("prepare", help="generate application materials for review")
    pr.add_argument("job_id")
    pr.add_argument("--no-ai", action="store_true")

    m = sub.add_parser("mark", help="update a job's status")
    m.add_argument("job_id")
    m.add_argument("status", choices=STATUSES)
    m.add_argument("--note")
    m.add_argument("--confirm", action="store_true", help="required for applied_by_user")

    sub.add_parser("stats", help="count jobs by status")
    sub.add_parser("links", help="manual search links for portals without an API")
    return p


def main(argv=None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    settings = Settings.from_env()
    handlers = {"run": cmd_run, "list": cmd_list, "show": cmd_show, "prepare": cmd_prepare,
                "mark": cmd_mark, "stats": cmd_stats, "links": cmd_links}
    if args.command is None:  # `python jobs.py` alone means `run`
        args = build_parser().parse_args([*(sys.argv[1:] if argv is None else argv), "run"])
    try:
        return handlers[args.command](args, settings)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
