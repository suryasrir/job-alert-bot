"""Public applicant-tracking-system job boards (documented, unauthenticated APIs).

- Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html
- Lever Postings API:       https://github.com/lever/postings-api
- Ashby Job Postings API:   https://developers.ashbyhq.com/docs/public-job-posting-api
- SmartRecruiters Posting API: https://developers.smartrecruiters.com/docs/posting-api
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

from ..http import FetchError, get_json
from ..models import Job
from ..text import detect_arrangement, detect_city, html_to_text, mentions_india, to_iso
from .base import FetchResult, SourceAdapter


def _country(location: str, explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return "India" if explicit.lower() in ("in", "ind", "india") else explicit
    return "India" if mentions_india(location) else None


class GreenhouseSource(SourceAdapter):
    name = "greenhouse"
    complete_listing = True

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for board in self.config.get("boards", []):
            url = f"https://boards-api.greenhouse.io/v1/boards/{quote(board)}/jobs?content=true"
            try:
                data = get_json(url)
                jobs = data["jobs"]
                if not isinstance(jobs, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"greenhouse:{board}: {exc}")
                continue
            except (KeyError, TypeError):
                result.errors.append(f"greenhouse:{board}: unexpected response format")
                continue
            self._collect(result, jobs, lambda j: self._parse(j, board), board)
            result.complete_boards.add(board)
        return result

    def _parse(self, j: dict[str, Any], board: str) -> Job:
        location = (j.get("location") or {}).get("name") or ""
        description = html_to_text(j.get("content"))
        return Job(
            source=self.name,
            source_job_id=str(j["id"]),
            board=board,
            title=j["title"].strip(),
            company=j.get("company_name") or board,
            url=j["absolute_url"],
            description=description,
            location=location or None,
            city=detect_city(location),
            country=_country(location),
            work_arrangement=detect_arrangement(location, j["title"]),
            posted_at=to_iso(j.get("first_published") or j.get("updated_at")),
            closing_date=to_iso(j.get("application_deadline")),
        )


class LeverSource(SourceAdapter):
    name = "lever"
    complete_listing = True

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for company in self.config.get("companies", []):
            url = f"https://api.lever.co/v0/postings/{quote(company)}?mode=json"
            try:
                data = get_json(url)
                if not isinstance(data, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"lever:{company}: {exc}")
                continue
            except TypeError:
                result.errors.append(f"lever:{company}: unexpected response format")
                continue
            self._collect(result, data, lambda j: self._parse(j, company), company)
            result.complete_boards.add(company)
        return result

    def _parse(self, j: dict[str, Any], company: str) -> Job:
        cats = j.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        location = ", ".join(all_locs) if all_locs else (cats.get("location") or "")
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        workplace = (j.get("workplaceType") or "").lower()
        salary = j.get("salaryRange") or {}
        return Job(
            source=self.name,
            source_job_id=j["id"],
            board=company,
            title=j["text"].strip(),
            company=company,
            url=j["hostedUrl"],
            apply_url=j.get("applyUrl"),
            description="\n\n".join(p for p in parts if p).strip(),
            location=location or None,
            city=detect_city(location),
            country=_country(location, j.get("country")),
            employment_type=cats.get("commitment"),
            work_arrangement=workplace if workplace in ("remote", "hybrid", "onsite")
            else detect_arrangement(location, cats.get("commitment")),
            salary_min=salary.get("min"),
            salary_max=salary.get("max"),
            salary_currency=salary.get("currency"),
            salary_period="year" if (salary.get("interval") or "").startswith("per-year") else salary.get("interval"),
            posted_at=to_iso(j.get("createdAt")),
        )


class AshbySource(SourceAdapter):
    name = "ashby"
    complete_listing = True

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for board in self.config.get("boards", []):
            url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board)}?includeCompensation=true"
            try:
                jobs = get_json(url)["jobs"]
                if not isinstance(jobs, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"ashby:{board}: {exc}")
                continue
            except (KeyError, TypeError):
                result.errors.append(f"ashby:{board}: unexpected response format")
                continue
            self._collect(result, [j for j in jobs if j.get("isListed", True)],
                          lambda j: self._parse(j, board), board)
            result.complete_boards.add(board)
        return result

    def _parse(self, j: dict[str, Any], board: str) -> Job:
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        location = ", ".join(l for l in locs if l)
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        workplace = (j.get("workplaceType") or "").lower()
        arrangement = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(workplace)
        if not arrangement:
            arrangement = "remote" if j.get("isRemote") else detect_arrangement(location)
        comp = j.get("compensation") or {}
        return Job(
            source=self.name,
            source_job_id=j["id"],
            board=board,
            title=j["title"].strip(),
            company=board,
            url=j["jobUrl"],
            apply_url=j.get("applyUrl"),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            location=location or None,
            city=detect_city(location),
            country=_country(location, country),
            employment_type=j.get("employmentType"),
            work_arrangement=arrangement,
            salary_text=comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary"),
            posted_at=to_iso(j.get("publishedAt")),
        )


class SmartRecruitersSource(SourceAdapter):
    """Searches each company's public postings by keyword (not a complete listing)."""

    name = "smartrecruiters"
    complete_listing = False

    def fetch(self) -> FetchResult:
        result = FetchResult()
        country = self.config.get("country", "in")
        for company in self.config.get("companies", []):
            seen: set[str] = set()
            for q in self.config.get("queries", ["ios"]):
                url = (f"https://api.smartrecruiters.com/v1/companies/{quote(company)}/postings"
                       f"?limit=100&country={quote(country)}&q={quote(q)}")
                try:
                    content = get_json(url)["content"]
                    if not isinstance(content, list):
                        raise TypeError
                except FetchError as exc:
                    result.errors.append(f"smartrecruiters:{company}: {exc}")
                    break
                except (KeyError, TypeError):
                    result.errors.append(f"smartrecruiters:{company}: unexpected response format")
                    break
                fresh = [c for c in content if c.get("id") not in seen]
                seen.update(c.get("id") for c in fresh)
                self._collect(result, fresh, lambda j: self._parse(j, company), company)
                time.sleep(0.5)
        return result

    def _parse(self, j: dict[str, Any], company: str) -> Job:
        loc = j.get("location") or {}
        raw = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
        # "bangalore, , India" -> "Bangalore, India"
        location = ", ".join(p.strip().title() if p.strip().islower() else p.strip()
                             for p in raw.split(",") if p.strip())
        arrangement = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "onsite" if loc.get("city") else "unknown"
        level = (j.get("experienceLevel") or {}).get("label")
        return Job(
            source=self.name,
            source_job_id=str(j["id"]),
            board=company,
            title=j["name"].strip(),
            company=(j.get("company") or {}).get("name") or company,
            url=f"https://jobs.smartrecruiters.com/{quote(company)}/{quote(str(j['id']))}",
            # The listing API returns no description; level and function give partial context.
            description=" ".join(x for x in (
                f"Experience level: {level}" if level else "",
                (j.get("function") or {}).get("label", ""),
            ) if x),
            location=location or None,
            city=detect_city(location),
            country=_country(location, loc.get("country")),
            employment_type=(j.get("typeOfEmployment") or {}).get("label"),
            work_arrangement=arrangement,
            posted_at=to_iso(j.get("releasedDate")),
        )
