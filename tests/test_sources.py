"""Source adapters against mocked API responses (no network)."""

from unittest.mock import MagicMock

import pytest
import requests

from job_agent import http
from job_agent.config import Settings
from job_agent.pipeline import run
from job_agent.sources import REGISTRY
from job_agent.sources.aggregators import AdzunaSource, RemoteOKSource, RemotiveSource
from job_agent.sources.ats import (AshbySource, GreenhouseSource, LeverSource, RecruiteeSource,
                                   SmartRecruitersSource, WorkableSource)

GREENHOUSE = {"jobs": [{
    "id": 111, "title": "iOS Engineer", "company_name": "Acme",
    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/111",
    "location": {"name": "Hyderabad, India"},
    "content": "&lt;p&gt;3+ years of &lt;b&gt;Swift&lt;/b&gt; experience&lt;/p&gt;",
    "first_published": "2026-10-10T10:00:00-04:00", "application_deadline": None,
}, {"id": 112}]}  # second record is malformed

LEVER = [{
    "id": "abc", "text": "React Native Developer", "hostedUrl": "https://jobs.lever.co/acme/abc",
    "applyUrl": "https://jobs.lever.co/acme/abc/apply", "createdAt": 1791849618000, "country": "IN",
    "workplaceType": "remote", "categories": {"location": "Remote", "commitment": "Full-time", "allLocations": ["Remote"]},
    "descriptionPlain": "Build apps", "lists": [{"text": "Requirements", "content": "<li>2+ years React Native</li>"}],
}]

ASHBY = {"jobs": [{
    "id": "u1", "title": "Mobile Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/u1", "location": "Bengaluru",
    "workplaceType": "Hybrid", "isRemote": False, "isListed": True, "publishedAt": "2026-10-01T00:00:00Z",
    "descriptionPlain": "Swift", "address": {"postalAddress": {"addressCountry": "India"}},
}, {"id": "u2", "title": "Hidden", "jobUrl": "https://x", "isListed": False}]}

SMARTRECRUITERS = {"content": [{
    "id": "999", "name": "iOS Developer", "company": {"name": "Bosch"}, "releasedDate": "2026-10-12T10:00:00Z",
    "location": {"city": "hyderabad", "country": "in", "remote": False, "fullLocation": "hyderabad, , India"},
    "experienceLevel": {"label": "Associate"},
}]}

REMOTIVE = {"jobs": [{"id": 5, "url": "https://remotive.com/remote-jobs/5", "title": "iOS Dev", "company_name": "R",
                      "candidate_required_location": "India", "publication_date": "2026-10-08T21:47:32",
                      "description": "<p>Swift</p>", "salary": ""}]}

REMOTEOK = [{"legal": "notice"}, {"id": "7", "position": "Senior iOS Engineer", "company": "OK",
                                  "url": "https://remoteok.com/remote-jobs/7", "location": "Worldwide",
                                  "date": "2026-10-01T00:00:00+00:00", "tags": ["ios"], "salary_min": 0}]


def fake_get(mapping):
    def _get(url, **kw):
        for key, value in mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise http.FetchError("no mock for " + url)
    return _get


def test_greenhouse_parses_and_skips_malformed(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"greenhouse": GREENHOUSE}))
    result = GreenhouseSource({"boards": ["acme"]}).fetch()
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job.city == "Hyderabad" and job.country == "India"
    assert "Swift" in job.description and "<b>" not in job.description
    assert job.posted_at.startswith("2026-10-10T14:00")
    assert "acme" in result.complete_boards
    assert any("malformed" in e for e in result.errors)


def test_lever_parses_lists_and_remote(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"lever": LEVER}))
    job = LeverSource({"companies": ["acme"]}).fetch().jobs[0]
    assert job.work_arrangement == "remote"
    assert "2+ years React Native" in job.description
    assert job.apply_url.endswith("/apply")
    assert job.country == "India"


def test_ashby_skips_unlisted(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"ashby": ASHBY}))
    jobs = AshbySource({"boards": ["acme"]}).fetch().jobs
    assert [j.title for j in jobs] == ["Mobile Engineer"]
    assert jobs[0].work_arrangement == "hybrid" and jobs[0].country == "India"


def test_smartrecruiters(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"smartrecruiters": SMARTRECRUITERS}))
    monkeypatch.setattr("job_agent.sources.ats.time.sleep", lambda *_: None)
    jobs = SmartRecruitersSource({"companies": ["BoschGroup"], "queries": ["ios", "swift"]}).fetch().jobs
    assert len(jobs) == 1  # de-duplicated across queries
    assert jobs[0].url == "https://jobs.smartrecruiters.com/BoschGroup/999"


def test_remotive_and_remoteok(monkeypatch):
    monkeypatch.setattr("job_agent.sources.aggregators.get_json",
                        fake_get({"remotive": REMOTIVE, "remoteok": REMOTEOK}))
    monkeypatch.setattr("job_agent.sources.aggregators.time.sleep", lambda *_: None)
    r = RemotiveSource({"queries": ["ios"]}).fetch().jobs
    assert r[0].work_arrangement == "remote" and r[0].country == "India"
    ok = RemoteOKSource({"tags": ["ios"]}).fetch().jobs
    assert len(ok) == 1 and ok[0].salary_min is None  # 0 salary treated as missing


def test_http_error_recorded_not_raised(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json",
                        fake_get({"greenhouse": http.FetchError("GET x failed: HTTP 404")}))
    result = GreenhouseSource({"boards": ["gone"]}).fetch()
    assert result.jobs == [] and "gone" not in result.complete_boards
    assert "HTTP 404" in result.errors[0]


def test_unexpected_format_recorded(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"greenhouse": {"oops": 1}}))
    result = GreenhouseSource({"boards": ["acme"]}).fetch()
    assert "unexpected response format" in result.errors[0]


def test_adzuna_skipped_without_keys():
    assert AdzunaSource({}).missing_credentials()


def test_adzuna_ignores_predicted_salary(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id1234")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key1234")
    data = {"results": [{"id": 1, "title": "<strong>iOS</strong> Developer", "redirect_url": "https://adzuna.in/1",
                         "company": {"display_name": "Z"}, "location": {"display_name": "Hyderabad, Telangana"},
                         "salary_min": 900000, "salary_max": 900000, "salary_is_predicted": "1",
                         "created": "2026-10-10T00:00:00Z", "description": "Swift"}]}
    monkeypatch.setattr("job_agent.sources.aggregators.get_json", fake_get({"adzuna": data}))
    jobs = AdzunaSource({"queries": ["ios"], "locations": [""], "max_pages": 1}).fetch().jobs
    assert jobs[0].title == "iOS Developer" and jobs[0].salary_min is None and jobs[0].city == "Hyderabad"


def test_one_crashing_source_does_not_stop_others(db, profile, monkeypatch):
    class Boom(GreenhouseSource):
        def fetch(self):
            raise RuntimeError("bug")

    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"lever": LEVER}))
    report = run(profile, Settings(db_path=":memory:", dry_run=True), {}, db,
                 adapters=[Boom({}), LeverSource({"companies": ["acme"]})], notifiers=[])
    assert report.per_source.get("lever") == 1
    assert any("greenhouse: unexpected RuntimeError" in e for e in report.errors)


def test_retry_then_success(monkeypatch):
    calls = []

    def fake_request(method, url, timeout=None, **kw):
        calls.append(1)
        resp = MagicMock()
        resp.status_code = 503 if len(calls) < 3 else 200
        resp.headers = {}
        return resp

    monkeypatch.setattr(http.session(), "request", fake_request)
    assert http.request("GET", "https://x.test", retries=3).status_code == 200
    assert len(calls) == 3


def test_retries_are_bounded_and_secret_redacted(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_KEY", "supersecretkey")

    def fail(*a, **k):
        raise requests.ConnectionError("https://x.test/?app_key=supersecretkey")

    monkeypatch.setattr(http.session(), "request", fail)
    with pytest.raises(http.FetchError) as exc:
        http.request("GET", "https://x.test/path?app_key=supersecretkey", retries=2)
    assert "supersecretkey" not in str(exc.value)


def test_malformed_json(monkeypatch):
    resp = MagicMock(status_code=200, headers={})
    resp.json.side_effect = ValueError("bad json")
    monkeypatch.setattr(http.session(), "request", lambda *a, **k: resp)
    with pytest.raises(http.FetchError, match="malformed JSON"):
        http.get_json("https://x.test")


def test_registry_has_all_sources():
    assert {"greenhouse", "lever", "ashby", "recruitee", "workable", "smartrecruiters", "remotive", "remoteok", "arbeitnow", "adzuna"} <= set(REGISTRY)


RECRUITEE = {"offers": [{
    "id": 2772826, "title": "IOS Engineer", "company_name": "Lenskart", "status": "published",
    "careers_url": "https://lenskartcareers.recruitee.com/o/ios-engineer",
    "careers_apply_url": "https://lenskartcareers.recruitee.com/o/ios-engineer/c/new",
    "location": "Bangalore, Karnātaka, India", "city": "Bangalore", "country": "India", "country_code": "IN",
    "remote": False, "hybrid": False, "on_site": True, "published_at": "2026-10-06 10:37:53 UTC",
    "employment_type_code": "fulltime_permanent", "description": "<p>Build iOS apps in Swift</p>",
    "requirements": "<ul><li>3+ years of experience</li></ul>",
    "salary": {"min": None, "max": None, "period": None, "currency": None}, "close_at": None,
}, {"id": 2, "title": "Draft", "status": "draft", "careers_url": "https://x"}]}

WORKABLE = {"name": "Apna", "jobs": [{
    "title": "SDE II - iOS", "shortcode": "ABC123", "employment_type": "Full-time", "telecommuting": False,
    "url": "https://apply.workable.com/j/ABC123", "application_url": "https://apply.workable.com/j/ABC123/apply",
    "published_on": "2026-10-11", "country": "India", "city": "Bengaluru", "state": "Karnataka",
    "locations": [{"country": "India", "countryCode": "IN", "city": "Bengaluru", "region": "Karnataka"}],
    "description": "<p>Swift, UIKit</p>", "experience": "Mid-Senior level",
}, {"title": "Broken"}]}


def test_recruitee(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"recruitee.com": RECRUITEE}))
    result = RecruiteeSource({"companies": ["lenskartcareers"]}).fetch()
    assert len(result.jobs) == 1  # draft skipped
    job = result.jobs[0]
    assert job.company == "Lenskart" and job.city == "Bengaluru" and job.country == "India"
    assert job.work_arrangement == "onsite"
    assert job.posted_at == "2026-10-06T10:37:53+00:00"
    assert "3+ years" in job.description and "<li>" not in job.description
    assert "lenskartcareers" in result.complete_boards


def test_workable(monkeypatch):
    monkeypatch.setattr("job_agent.sources.ats.get_json", fake_get({"workable.com": WORKABLE}))
    monkeypatch.setattr("job_agent.sources.ats.time.sleep", lambda *_: None)
    result = WorkableSource({"companies": ["apna"]}).fetch()
    assert [j.title for j in result.jobs] == ["SDE II - iOS"]
    job = result.jobs[0]
    assert job.company == "Apna" and job.city == "Bengaluru" and job.country == "India"
    assert job.apply_url.endswith("/apply") and job.posted_at.startswith("2026-10-11")
    assert any("malformed" in e for e in result.errors)
