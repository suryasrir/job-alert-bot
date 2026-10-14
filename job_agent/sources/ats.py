"""Public applicant-tracking-system job boards (documented, unauthenticated APIs).

- Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html
- Lever Postings API:       https://github.com/lever/postings-api
- Ashby Job Postings API:   https://developers.ashbyhq.com/docs/public-job-posting-api
- SmartRecruiters Posting API: https://developers.smartrecruiters.com/docs/posting-api
- Recruitee careers API, Workable job widget, Workday career-site JSON (see each class)
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from ..http import FetchError, get_json, request
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


class RecruiteeSource(SourceAdapter):
    """Recruitee public careers API: https://docs.recruitee.com/reference/offers"""

    name = "recruitee"
    complete_listing = True

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for company in self.config.get("companies", []):
            url = f"https://{quote(company)}.recruitee.com/api/offers/"
            try:
                offers = get_json(url)["offers"]
                if not isinstance(offers, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"recruitee:{company}: {exc}")
                continue
            except (KeyError, TypeError):
                result.errors.append(f"recruitee:{company}: unexpected response format")
                continue
            self._collect(result, [o for o in offers if o.get("status", "published") == "published"],
                          lambda j: self._parse(j, company), company)
            result.complete_boards.add(company)
        return result

    def _parse(self, j: dict[str, Any], company: str) -> Job:
        location = j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        arrangement = ("remote" if j.get("remote") else "hybrid" if j.get("hybrid")
                       else "onsite" if j.get("on_site") else detect_arrangement(location))
        salary = j.get("salary") or {}
        published = (j.get("published_at") or "").replace(" UTC", "+00:00").replace(" ", "T", 1)
        return Job(
            source=self.name,
            source_job_id=str(j["id"]),
            board=company,
            title=j["title"].strip(),
            company=j.get("company_name") or company,
            url=j["careers_url"],
            apply_url=j.get("careers_apply_url"),
            description="\n\n".join(html_to_text(j.get(k)) for k in ("description", "requirements") if j.get(k)),
            location=location or None,
            city=detect_city(location),
            country=_country(location, j.get("country_code")),
            employment_type=(j.get("employment_type_code") or "").replace("_", " ") or None,
            work_arrangement=arrangement,
            salary_min=salary.get("min"),
            salary_max=salary.get("max"),
            salary_currency=salary.get("currency"),
            salary_period=salary.get("period"),
            posted_at=to_iso(published),
            closing_date=to_iso(j.get("close_at")),
        )


class WorkableSource(SourceAdapter):
    """Workable public job widget API (the feed behind each company's apply.workable.com page)."""

    name = "workable"
    complete_listing = True

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for company in self.config.get("companies", []):
            url = f"https://apply.workable.com/api/v1/widget/accounts/{quote(company)}?details=true"
            try:
                data = get_json(url)
                jobs = data["jobs"]
                if not isinstance(jobs, list):
                    raise TypeError
            except FetchError as exc:
                result.errors.append(f"workable:{company}: {exc}")
                continue
            except (KeyError, TypeError):
                result.errors.append(f"workable:{company}: unexpected response format")
                continue
            org = data.get("name") or company
            self._collect(result, jobs, lambda j: self._parse(j, company, org), company)
            result.complete_boards.add(company)
            time.sleep(2)  # Workable rate-limits bursts
        return result

    def _parse(self, j: dict[str, Any], company: str, org: str) -> Job:
        locs = j.get("locations") or [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
        names = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in locs]
        location = " / ".join(n for n in names if n)
        country = (locs[0].get("countryCode") or locs[0].get("country")) if locs else None
        url = j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{quote(company)}/j/{j['shortcode']}/"
        return Job(
            source=self.name,
            source_job_id=str(j["shortcode"]),
            board=company,
            title=j["title"].strip(),
            company=org,
            url=url,
            apply_url=j.get("application_url"),
            description=html_to_text(j.get("description")),
            experience_text=j.get("experience") or None,
            location=location or None,
            city=detect_city(location),
            country=_country(location, country),
            employment_type=j.get("employment_type") or None,
            work_arrangement="remote" if j.get("telecommuting") else detect_arrangement(location),
            posted_at=to_iso(j.get("published_on") or j.get("created_at")),
        )


class WorkdaySource(SourceAdapter):
    """Workday career sites (*.myworkdayjobs.com), via the same JSON the public careers page loads.

    Each site's robots.txt allows its careers paths. Results come by relevance, not date, so we
    read a bounded number of pages per search and rely on `postedOn` to find fresh jobs.
    Config: {"sites": [{"name": "Accenture", "url": "https://accenture.wd103.myworkdayjobs.com/AccentureCareers"}],
             "queries": ["developer", "engineer"], "max_pages": 5}
    """

    name = "workday"
    complete_listing = False
    PAGE = 20

    def fetch(self) -> FetchResult:
        result = FetchResult()
        for site in self.config.get("sites", []):
            try:
                host, tenant, path = self._parts(site["url"])
            except (KeyError, ValueError):
                result.errors.append(f"workday:{site.get('name')}: bad url")
                continue
            api = f"https://{host}/wday/cxs/{tenant}/{path}/jobs"
            try:
                facets = self._india_facet(api)
            except FetchError as exc:
                result.errors.append(f"workday:{site['name']}: {exc}")
                continue
            seen: set[str] = set()
            for q in self.config.get("queries", ["developer"]):
                for page in range(int(self.config.get("max_pages", 5))):
                    body = {"appliedFacets": facets, "limit": self.PAGE, "offset": page * self.PAGE, "searchText": q}
                    try:
                        data = request("POST", api, json=body, headers={"Content-Type": "application/json"}).json()
                        postings = data["jobPostings"]
                        if not isinstance(postings, list):
                            raise TypeError
                    except FetchError as exc:
                        result.errors.append(f"workday:{site['name']}: {exc}")
                        break
                    except (KeyError, TypeError, ValueError):
                        result.errors.append(f"workday:{site['name']}: unexpected response format")
                        break
                    fresh = [p for p in postings if p.get("externalPath") and p["externalPath"] not in seen]
                    seen.update(p["externalPath"] for p in fresh)
                    self._collect(result, fresh, lambda p: self._parse(p, site, host, path, bool(facets)), site["name"])
                    if len(postings) < self.PAGE:
                        break
                    time.sleep(1)
        return result

    @staticmethod
    def _parts(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url)
        host = parts.netloc
        if not host.endswith(".myworkdayjobs.com"):
            raise ValueError(url)
        segs = [s for s in parts.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if not segs:
            raise ValueError(url)
        return host, host.split(".")[0], segs[0]

    def _india_facet(self, api: str) -> dict[str, list[str]]:
        """Find this site's own id for 'India' (ids differ between companies). {} when there is none."""
        data = request("POST", api, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                       headers={"Content-Type": "application/json"}).json()

        def walk(node: dict, param: Optional[str]) -> Optional[tuple[str, str]]:
            for v in node.get("values", []):
                p = v.get("facetParameter") or param
                if "values" in v:
                    found = walk(v, p)
                    if found:
                        return found
                elif str(v.get("descriptor", "")).strip().lower() == "india" and p:
                    return p, v["id"]
            return None

        for facet in data.get("facets", []):
            found = walk(facet, facet.get("facetParameter"))
            if found:
                return {found[0]: [found[1]]}
        return {}

    def _parse(self, p: dict[str, Any], site: dict, host: str, path: str, india_filtered: bool) -> Optional[Job]:
        location = p.get("locationsText") or ""
        if not india_filtered and not mentions_india(location) and not re.search(r"\bIND\b", location):
            return None  # site has no country filter: keep only jobs that name an Indian location
        return Job(
            source=self.name,
            source_job_id=f"{host}{p['externalPath']}",
            board=site["name"],
            title=p["title"].strip(),
            company=site["name"],
            url=f"https://{host}/{path}{p['externalPath']}",
            description="",
            location=location or ("India" if india_filtered else None),
            city=detect_city(location),
            country="India" if india_filtered or mentions_india(location) or "IND" in location else None,
            work_arrangement=detect_arrangement(location, p.get("remoteType")),
            posted_at=_workday_posted(p.get("postedOn")),
        )


def _workday_posted(text: Optional[str]) -> Optional[str]:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    if not text:
        return None
    t = text.lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        if not m:
            return None
        days = int(m.group(1)) + (1 if "+" in t else 0)
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


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
