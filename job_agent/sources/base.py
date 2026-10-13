"""Source adapter interface."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..models import Job

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Boards whose *complete* open-job list was fetched successfully. Jobs from these
    # boards that were not returned can safely be marked closed.
    complete_boards: set[str] = field(default_factory=set)


class SourceAdapter:
    """Subclasses implement `fetch`. One failing board/query must not stop the rest."""

    name: str = "base"
    # True when a board fetch returns every open posting (lets us detect closed jobs).
    complete_listing: bool = False

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def missing_credentials(self) -> Optional[str]:
        """Return a human-readable reason if the adapter cannot run, else None."""
        return None

    def fetch(self) -> FetchResult:
        raise NotImplementedError

    def _collect(self, result: FetchResult, items: Iterable[Any], parse, board: Optional[str] = None) -> None:
        """Parse each raw item, skipping (and recording) malformed ones."""
        bad = 0
        for item in items:
            try:
                job = parse(item)
            except (KeyError, TypeError, ValueError, AttributeError):
                bad += 1
                continue
            if job is None:
                continue
            problems = job.validate()
            if problems:
                bad += 1
                continue
            result.jobs.append(job)
        if bad:
            result.errors.append(f"{self.name}{':' + board if board else ''}: skipped {bad} malformed record(s)")
