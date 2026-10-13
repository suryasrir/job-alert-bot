"""Job aggregator APIs.

- Remotive:  https://remotive.com/api-documentation  (free; max ~4 calls/day advised; link back to Remotive)
- RemoteOK:  https://remoteok.com/api                (free; link back to Remote OK as the source)
- Arbeitnow: https://www.arbeitnow.com/api/job-board-api (free; mostly European jobs)
- Adzuna:    https://developer.adzuna.com             (free key; has an India endpoint)
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional
from urllib.parse import urlencode

from ..http import FetchError, get_json
from ..models import Job
from ..text import detect_arrangement, detect_city, html_to_inline, html_to_text, mentions_india, to_iso
from .base import FetchResult, SourceAdapter


def _country_from(location: str) -> Optional[str]:
    return "India" if mentions_india(location) else None


class RemotiveSource(SourceAdapter):
    name = "remotive"

    def fetch(self) -> FetchResult:
        result = FetchResult()
        seen: set[Any] = set()
        for q in self.config.get("queries", ["ios"]):
            try:
                jobs = get_json("https://remotive.com/api/remote-jobs?" + urlencode({"search": q}))["jobs"]
                if not isinstance(jobs, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"remotive:{q}: {exc}")
                continue
            except (KeyError, TypeError):
                result.errors.append(f"remotive:{q}: unexpected response format")
                continue
            fresh = [j for j in jobs if j.get("id") not in seen]
            seen.update(j.get("id") for j in fresh)
            self._collect(result, fresh, self._parse, q)
            time.sleep(1)
        return result

    def _parse(self, j: dict[str, Any]) -> Job:
        location = j.get("candidate_required_location") or ""
        return Job(
            source=self.name,
            source_job_id=str(j["id"]),
            title=j["title"].strip(),
            company=j["company_name"].strip(),
            url=j["url"],
            description=html_to_text(j.get("description")),
            location=f"Remote ({location})" if location else "Remote",
            country=_country_from(location),
            employment_type=j.get("job_type"),
            work_arrangement="remote",
            salary_text=j.get("salary") or None,
            posted_at=to_iso(j.get("publication_date")),
        )


class RemoteOKSource(SourceAdapter):
    name = "remoteok"

    def fetch(self) -> FetchResult:
        result = FetchResult()
        seen: set[Any] = set()
        for tag in self.config.get("tags", ["ios"]):
            try:
                data = get_json("https://remoteok.com/api?" + urlencode({"tag": tag}))
                if not isinstance(data, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"remoteok:{tag}: {exc}")
                continue
            except TypeError:
                result.errors.append(f"remoteok:{tag}: unexpected response format")
                continue
            # The first element is a legal notice, not a job.
            jobs = [j for j in data if isinstance(j, dict) and j.get("id") and j.get("position")]
            fresh = [j for j in jobs if j["id"] not in seen]
            seen.update(j["id"] for j in fresh)
            self._collect(result, fresh, self._parse, tag)
            time.sleep(1)
        return result

    def _parse(self, j: dict[str, Any]) -> Job:
        location = html_to_inline(j.get("location"))
        return Job(
            source=self.name,
            source_job_id=str(j["id"]),
            title=html_to_inline(j["position"]),
            company=html_to_inline(j.get("company")) or "Unknown",
            url=j.get("url") or f"https://remoteok.com/remote-jobs/{j['id']}",
            apply_url=j.get("apply_url") or None,
            description=html_to_text(j.get("description")),
            required_skills=[t for t in j.get("tags") or [] if isinstance(t, str)],
            location=f"Remote ({location})" if location else "Remote",
            country=_country_from(location),
            work_arrangement="remote",
            salary_min=j.get("salary_min") or None,
            salary_max=j.get("salary_max") or None,
            salary_currency="USD" if j.get("salary_min") else None,
            salary_period="year" if j.get("salary_min") else None,
            posted_at=to_iso(j.get("date") or j.get("epoch")),
        )


class ArbeitnowSource(SourceAdapter):
    name = "arbeitnow"

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for page in range(1, int(self.config.get("max_pages", 3)) + 1):
            try:
                data = get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
                jobs = data["data"]
                if not isinstance(jobs, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"arbeitnow:page{page}: {exc}")
                break
            except (KeyError, TypeError):
                result.errors.append(f"arbeitnow:page{page}: unexpected response format")
                break
            self._collect(result, jobs, self._parse)
            if not (data.get("links") or {}).get("next"):
                break
            time.sleep(1)
        return result

    def _parse(self, j: dict[str, Any]) -> Job:
        location = j.get("location") or ""
        return Job(
            source=self.name,
            source_job_id=j["slug"],
            title=j["title"].strip(),
            company=j["company_name"].strip(),
            url=j["url"],
            description=html_to_text(j.get("description")),
            required_skills=[t for t in j.get("tags") or [] if isinstance(t, str)],
            location=location or None,
            country=_country_from(location),
            work_arrangement="remote" if j.get("remote") else detect_arrangement(location),
            employment_type=", ".join(j.get("job_types") or []) or None,
            posted_at=to_iso(j.get("created_at")),
        )


class AdzunaSource(SourceAdapter):
    name = "adzuna"

    def missing_credentials(self) -> Optional[str]:
        if not (os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")):
            return "ADZUNA_APP_ID / ADZUNA_APP_KEY not set (free keys at https://developer.adzuna.com)"
        return None

    def fetch(self) -> FetchResult:
        result = FetchResult()
        app_id, app_key = os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")
        country = self.config.get("country", "in")
        seen: set[str] = set()
        for q in self.config.get("queries", ["ios developer"]):
            for where in self.config.get("locations", [""]):
                for page in range(1, int(self.config.get("max_pages", 2)) + 1):
                    params = {"app_id": app_id, "app_key": app_key, "what": q,
                              "results_per_page": 50, "max_days_old": 30, "sort_by": "date"}
                    if where:
                        params["where"] = where
                    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?" + urlencode(params)
                    try:
                        items = get_json(url)["results"]
                        if not isinstance(items, list):
                            raise TypeError
                    except FetchError as exc:
                        result.errors.append(f"adzuna:{q}/{where or 'all'}: {exc}")
                        break
                    except (KeyError, TypeError):
                        result.errors.append(f"adzuna:{q}/{where or 'all'}: unexpected response format")
                        break
                    fresh = [i for i in items if str(i.get("id")) not in seen]
                    seen.update(str(i.get("id")) for i in fresh)
                    self._collect(result, fresh, self._parse, q)
                    if len(items) < 50:
                        break
                    time.sleep(1)
        return result

    def _parse(self, j: dict[str, Any]) -> Job:
        location = (j.get("location") or {}).get("display_name") or ""
        title = html_to_inline(j["title"])
        description = html_to_text(j.get("description"))  # Adzuna returns a truncated snippet
        predicted = str(j.get("salary_is_predicted")) == "1"
        return Job(
            source=self.name,
            source_job_id=str(j["id"]),
            title=title,
            company=(j.get("company") or {}).get("display_name") or "Unknown",
            url=j["redirect_url"],
            description=description,
            location=location or None,
            city=detect_city(location),
            country="India" if self.config.get("country", "in") == "in" else None,
            employment_type=j.get("contract_time"),
            work_arrangement=detect_arrangement(title, description),
            # Adzuna "predicted" salaries are estimates, not employer data, so ignore them.
            salary_min=None if predicted else j.get("salary_min"),
            salary_max=None if predicted else j.get("salary_max"),
            salary_currency=None if predicted or not j.get("salary_min") else "INR",
            salary_period=None if predicted else "year",
            posted_at=to_iso(j.get("created")),
        )
