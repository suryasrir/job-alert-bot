"""Company list (config/companies.csv) loading and the Workday reader, with mocked responses."""

import csv

from job_agent.config import merge_company_list
from job_agent.sources.ats import WorkdaySource, _workday_posted


def _write(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "system", "feed", "status"])
        w.writeheader()
        w.writerows(rows)


def test_only_connected_rows_are_added(tmp_path):
    p = tmp_path / "companies.csv"
    _write(p, [
        {"company": "Acme", "system": "greenhouse", "feed": "acme", "status": "connected"},
        {"company": "Big IT", "system": "workday", "feed": "https://bigit.wd1.myworkdayjobs.com/Careers", "status": "connected"},
        {"company": "Paused", "system": "lever", "feed": "paused", "status": "manual"},
        {"company": "Broken", "system": "unknown", "feed": "x", "status": "connected"},
    ])
    cfg = {"greenhouse": {"boards": ["acme"]}}
    assert merge_company_list(cfg, p) == 1  # acme already present, Big IT added
    assert cfg["greenhouse"]["boards"] == ["acme"]
    assert cfg["workday"]["sites"] == [{"name": "Big IT", "url": "https://bigit.wd1.myworkdayjobs.com/Careers"}]
    assert "lever" not in cfg


def test_missing_company_list_is_fine(tmp_path):
    assert merge_company_list({}, tmp_path / "none.csv") == 0


FACETS = {"facets": [{"facetParameter": "locationMainGroup", "values": [
    {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
        {"descriptor": "Poland", "id": "pl1"}, {"descriptor": "India", "id": "in42"}]}]}], "jobPostings": []}
JOBS = {"total": 2, "jobPostings": [
    {"title": "Java Developer", "externalPath": "/job/Pune/Java-Developer_R1", "locationsText": "Pune, India",
     "postedOn": "Posted 2 Days Ago"},
    {"title": "React Developer", "externalPath": "/job/Bengaluru/React_R2", "locationsText": "3 Locations",
     "postedOn": "Posted Today"},
]}


class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_workday_finds_india_and_parses(monkeypatch):
    calls = []

    def fake_request(method, url, json=None, **kw):
        calls.append(json)
        return FakeResp(FACETS if json["limit"] == 1 else JOBS)

    monkeypatch.setattr("job_agent.sources.ats.request", fake_request)
    monkeypatch.setattr("job_agent.sources.ats.time.sleep", lambda *_: None)
    src = WorkdaySource({"sites": [{"name": "Big IT", "url": "https://bigit.wd1.myworkdayjobs.com/en-US/Careers"}],
                         "queries": ["developer"], "max_pages": 2})
    result = src.fetch()
    assert calls[1]["appliedFacets"] == {"locationCountry": ["in42"]}  # the site's own India id
    assert [j.title for j in result.jobs] == ["Java Developer", "React Developer"]
    job = result.jobs[1]
    assert job.company == "Big IT" and job.country == "India"
    assert job.url == "https://bigit.wd1.myworkdayjobs.com/Careers/job/Bengaluru/React_R2"
    assert result.jobs[0].city == "Pune"


def test_workday_without_country_filter_keeps_only_india(monkeypatch):
    jobs = {"jobPostings": [
        {"title": "Dev A", "externalPath": "/a", "locationsText": "Krakow, Poland", "postedOn": "Posted Today"},
        {"title": "Dev B", "externalPath": "/b", "locationsText": "IND - AP - HYDERABAD", "postedOn": "Posted Today"},
    ]}
    monkeypatch.setattr("job_agent.sources.ats.request",
                        lambda m, u, json=None, **k: FakeResp({"facets": []} if json["limit"] == 1 else jobs))
    monkeypatch.setattr("job_agent.sources.ats.time.sleep", lambda *_: None)
    result = WorkdaySource({"sites": [{"name": "X", "url": "https://x.wd1.myworkdayjobs.com/C"}], "max_pages": 1}).fetch()
    assert [j.title for j in result.jobs] == ["Dev B"]


def test_workday_bad_url_recorded():
    result = WorkdaySource({"sites": [{"name": "Bad", "url": "https://example.com/jobs"}]}).fetch()
    assert result.jobs == [] and "bad url" in result.errors[0]


def test_workday_posted_dates():
    assert _workday_posted("Posted Today")[:10] == _workday_posted("Posted 0 Days Ago")[:10]
    assert _workday_posted("Posted 30+ Days Ago") < _workday_posted("Posted 3 Days Ago")
    assert _workday_posted("Posted Yesterday") and _workday_posted(None) is None and _workday_posted("soon") is None
