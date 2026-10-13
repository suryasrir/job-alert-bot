"""SQLite storage with versioned migrations and cautious deduplication.

The database stores public job data and match results only. It never stores the
candidate profile, salary expectations or contact details.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import STATUSES, Job
from .text import canonical_url, normalize_key, now_iso

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_job_id TEXT,
        board TEXT,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        description TEXT,
        required_skills TEXT,
        experience_text TEXT,
        employment_type TEXT,
        work_arrangement TEXT,
        location TEXT,
        city TEXT,
        country TEXT,
        salary_min REAL,
        salary_max REAL,
        salary_currency TEXT,
        salary_period TEXT,
        salary_text TEXT,
        url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        apply_url TEXT,
        posted_at TEXT,
        closing_date TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'discovered',
        score REAL,
        score_breakdown TEXT,
        match_category TEXT,
        match_reasons TEXT,
        concerns TEXT,
        unknowns TEXT,
        fuzzy_key TEXT,
        duplicate_of TEXT,
        notified_at TEXT,
        notify_channels TEXT,
        ai_analysis TEXT,
        ai_analyzed_at TEXT,
        application_prepared_at TEXT,
        user_review TEXT,
        user_updated_at TEXT
    );
    CREATE UNIQUE INDEX idx_jobs_source_id ON jobs(source, source_job_id) WHERE source_job_id IS NOT NULL;
    CREATE INDEX idx_jobs_canonical_url ON jobs(canonical_url);
    CREATE INDEX idx_jobs_fuzzy ON jobs(fuzzy_key);
    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        fetched INTEGER DEFAULT 0,
        new_jobs INTEGER DEFAULT 0,
        notified INTEGER DEFAULT 0,
        errors TEXT
    );
    """,
    # 2: public Telegram channel posting and per-source fetch intervals
    """
    ALTER TABLE jobs ADD COLUMN public_ok INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN channel_posted_at TEXT;
    CREATE INDEX idx_jobs_channel ON jobs(public_ok, channel_posted_at);
    CREATE TABLE source_state (
        source TEXT PRIMARY KEY,
        last_fetched_at TEXT NOT NULL
    );
    """,
]

JSON_FIELDS = ("required_skills", "score_breakdown", "match_reasons", "concerns", "unknowns")
JOB_COLUMNS = (
    "source", "source_job_id", "board", "title", "company", "description", "experience_text",
    "employment_type", "work_arrangement", "location", "city", "country", "salary_min", "salary_max",
    "salary_currency", "salary_period", "salary_text", "url", "apply_url", "posted_at", "closing_date",
    "score", "match_category", "public_ok",
) + JSON_FIELDS


def fuzzy_key(job: Job) -> str:
    return "|".join((normalize_key(job.company), normalize_key(job.title), normalize_key(job.city or job.location)))


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ---- schema -----------------------------------------------------------------
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        version = self.schema_version()
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.conn:
                self.conn.executescript(sql)
                self.conn.execute(f"PRAGMA user_version = {i}")

    # ---- upsert / dedup -----------------------------------------------------------
    def find_existing(self, job: Job) -> Optional[sqlite3.Row]:
        """Exact matches only: same source ID, or same canonical URL."""
        if job.source_job_id:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?", (job.source, job.source_job_id)
            ).fetchone()
            if row:
                return row
        return self.conn.execute(
            "SELECT * FROM jobs WHERE canonical_url = ? ORDER BY first_seen_at LIMIT 1", (canonical_url(job.url),)
        ).fetchone()

    def find_fuzzy_duplicate(self, job: Job) -> Optional[str]:
        """Possible cross-source duplicate: identical company+title+location from a *different* source.

        Same-source matches are not merged because a company can post several
        vacancies with identical titles. Duplicates are linked, not merged.
        """
        key = fuzzy_key(job)
        if not all(key.split("|")):
            return None  # not enough information to compare safely
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE fuzzy_key = ? AND source != ? AND duplicate_of IS NULL "
            "AND status NOT IN ('closed','expired') ORDER BY first_seen_at LIMIT 1",
            (key, job.source),
        ).fetchone()
        return row["id"] if row else None

    def upsert(self, job: Job) -> tuple[str, bool]:
        """Insert or update a job. Returns (job_id, is_new)."""
        now = now_iso()
        existing = self.find_existing(job)
        values = self._values(job)
        if existing and existing["source"] != job.source:
            # Same posting reached through another source (same URL): keep the original record.
            with self.conn:
                self.conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (now, existing["id"]))
            job.id = existing["id"]
            return existing["id"], False
        if existing:
            sets = ", ".join(f"{c} = ?" for c in JOB_COLUMNS)
            # A job that reappears after being marked closed is open again.
            status = existing["status"]
            if status in ("closed", "expired"):
                status = "discovered"
            with self.conn:
                self.conn.execute(
                    f"UPDATE jobs SET {sets}, canonical_url = ?, fuzzy_key = ?, last_seen_at = ?, status = ? WHERE id = ?",
                    (*values, canonical_url(job.url), fuzzy_key(job), now, status, existing["id"]),
                )
            job.id = existing["id"]
            job.first_seen_at = existing["first_seen_at"]
            job.last_seen_at = now
            job.status = status
            return existing["id"], False

        job.id = job.id or job.compute_id()
        duplicate_of = self.find_fuzzy_duplicate(job)
        cols = ", ".join(JOB_COLUMNS)
        marks = ", ".join("?" for _ in JOB_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO jobs (id, {cols}, canonical_url, fuzzy_key, duplicate_of, first_seen_at, last_seen_at, status) "
                f"VALUES (?, {marks}, ?, ?, ?, ?, ?, 'discovered')",
                (job.id, *values, canonical_url(job.url), fuzzy_key(job), duplicate_of, now, now),
            )
        job.first_seen_at = job.last_seen_at = now
        return job.id, duplicate_of is None

    def _values(self, job: Job) -> tuple[Any, ...]:
        out = []
        for col in JOB_COLUMNS:
            v = getattr(job, col)
            out.append(json.dumps(v) if col in JSON_FIELDS else v)
        return tuple(out)

    # ---- lifecycle --------------------------------------------------------------
    def mark_missing_closed(self, source: str, board: str, seen_ids: set[str]) -> int:
        """Mark jobs from a fully-fetched board as closed when they are no longer listed."""
        rows = self.conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND board = ? AND status NOT IN ('closed','expired','applied_by_user','dismissed')",
            (source, board),
        ).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in seen_ids]
        with self.conn:
            self.conn.executemany("UPDATE jobs SET status = 'closed' WHERE id = ?", [(i,) for i in gone])
        return len(gone)

    def expire_past_deadlines(self) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET status = 'expired' WHERE closing_date IS NOT NULL AND closing_date < ? "
                "AND status IN ('discovered','shortlisted')",
                (now_iso(),),
            )
        return cur.rowcount

    def set_status(self, job_id: str, status: str, note: Optional[str] = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET status = ?, user_review = COALESCE(?, user_review), user_updated_at = ? WHERE id = ?",
                (status, note, now_iso(), job_id),
            )
        if cur.rowcount == 0:
            raise KeyError(f"no job with id {job_id}")

    # ---- notifications -------------------------------------------------------------
    def pending_notifications(self, min_score: float, since_iso: Optional[str] = None) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM jobs WHERE notified_at IS NULL AND duplicate_of IS NULL "
               "AND status IN ('discovered','shortlisted') AND score >= ? "
               "AND match_category != 'experience_mismatch'")
        params: list[Any] = [min_score]
        if since_iso:
            sql += " AND (posted_at IS NULL OR posted_at >= ?)"
            params.append(since_iso)
        sql += " ORDER BY score DESC, posted_at DESC"
        return self.conn.execute(sql, params).fetchall()

    def mark_notified(self, job_ids: list[str], channel: str) -> None:
        now = now_iso()
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET notified_at = ?, notify_channels = ? WHERE id = ?",
                [(now, channel, i) for i in job_ids],
            )

    # ---- public channel --------------------------------------------------------------
    def pending_channel_posts(self, since_iso: str) -> list[sqlite3.Row]:
        """Public-suitable open jobs not yet posted, posted (or first seen) after `since_iso`."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE public_ok = 1 AND channel_posted_at IS NULL AND duplicate_of IS NULL "
            "AND status NOT IN ('closed','expired') AND COALESCE(posted_at, first_seen_at) >= ? "
            "ORDER BY COALESCE(posted_at, first_seen_at) DESC, score DESC",
            (since_iso,),
        ).fetchall()

    def mark_channel_posted(self, job_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE jobs SET channel_posted_at = ? WHERE id = ?", (now_iso(), job_id))

    # ---- per-source fetch intervals ---------------------------------------------------
    def source_last_fetched(self, source: str) -> Optional[str]:
        row = self.conn.execute("SELECT last_fetched_at FROM source_state WHERE source = ?", (source,)).fetchone()
        return row["last_fetched_at"] if row else None

    def set_source_fetched(self, source: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_state (source, last_fetched_at) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_fetched_at = excluded.last_fetched_at",
                (source, now_iso()),
            )

    # ---- AI / application prep ---------------------------------------------------
    def save_ai_analysis(self, job_id: str, analysis: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET ai_analysis = ?, ai_analyzed_at = ? WHERE id = ?",
                (json.dumps(analysis), now_iso(), job_id),
            )

    def mark_application_prepared(self, job_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET application_prepared_at = ?, "
                "status = CASE WHEN status IN ('discovered','shortlisted') THEN 'application_prepared' ELSE status END "
                "WHERE id = ?",
                (now_iso(), job_id),
            )

    # ---- queries ---------------------------------------------------------------------
    def get(self, job_id: str) -> Optional[sqlite3.Row]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None and len(job_id) >= 4:
            rows = self.conn.execute("SELECT * FROM jobs WHERE id LIKE ?", (job_id + "%",)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        return row

    def list_jobs(self, status: Optional[str] = None, min_score: float = 0, limit: int = 30) -> list[sqlite3.Row]:
        sql = "SELECT * FROM jobs WHERE score >= ? AND duplicate_of IS NULL"
        params: list[Any] = [min_score]
        if status:
            sql += " AND status = ?"
            params.append(status)
        else:
            sql += " AND status NOT IN ('closed','expired','dismissed')"
        sql += " ORDER BY score DESC, posted_at DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def record_run(self, started_at: str, fetched: int, new_jobs: int, notified: int, errors: list[str]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (started_at, finished_at, fetched, new_jobs, notified, errors) VALUES (?,?,?,?,?,?)",
                (started_at, now_iso(), fetched, new_jobs, notified, json.dumps(errors)),
            )


def row_to_job(row: sqlite3.Row) -> Job:
    data = dict(row)
    kwargs = {k: data.get(k) for k in Job.__dataclass_fields__ if k in data}
    for f in JSON_FIELDS:
        kwargs[f] = json.loads(data[f]) if data.get(f) else ([] if f != "score_breakdown" else {})
    kwargs["public_ok"] = bool(data.get("public_ok"))
    kwargs["first_seen_at"] = data.get("first_seen_at")
    kwargs["last_seen_at"] = data.get("last_seen_at")
    return Job(**kwargs)
