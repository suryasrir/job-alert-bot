from conftest import make_job

from job_agent.database import Database, row_to_job
from job_agent.matching import score_job
from job_agent.models import Job
from job_agent.text import canonical_url


def test_validation():
    assert make_job().validate() == []
    bad = Job(source="", title="", company="", url="ftp://x")
    problems = bad.validate()
    assert "missing title" in problems and "missing or non-http url" in problems
    assert make_job(work_arrangement="sometimes").validate()


def test_insert_then_update_same_source_id(db, profile):
    job = score_job(make_job(), profile)
    job_id, new = db.upsert(job)
    assert new
    again = score_job(make_job(title="iOS Developer (updated)"), profile)
    job_id2, new2 = db.upsert(again)
    assert job_id2 == job_id and not new2
    row = db.get(job_id)
    assert row["title"] == "iOS Developer (updated)"
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_same_url_with_tracking_params_is_same_job(db, profile):
    a = make_job(source_job_id=None, url="https://jobs.example.com/ios/42?utm_source=x")
    b = make_job(source_job_id=None, url="https://jobs.example.com/ios/42/?utm_campaign=y#apply")
    db.upsert(score_job(a, profile))
    _, new = db.upsert(score_job(b, profile))
    assert not new


def test_different_query_params_are_not_merged():
    assert canonical_url("https://x.com/job?id=1") != canonical_url("https://x.com/job?id=2")


def test_same_title_same_source_different_ids_not_merged(db, profile):
    db.upsert(score_job(make_job(source_job_id="1", url="https://e.com/1"), profile))
    _, new = db.upsert(score_job(make_job(source_job_id="2", url="https://e.com/2"), profile))
    assert new
    assert db.conn.execute("SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL").fetchone()[0] == 2


def test_cross_source_fuzzy_duplicate_linked_not_merged(db, profile):
    first_id, _ = db.upsert(score_job(make_job(), profile))
    other = make_job(source="adzuna", source_job_id="zz9", url="https://adzuna.in/land/ad/zz9")
    dup_id, new = db.upsert(score_job(other, profile))
    assert not new
    assert db.get(dup_id)["duplicate_of"] == first_id


def test_fuzzy_duplicate_needs_location(db, profile):
    db.upsert(score_job(make_job(location=None, city=None), profile))
    other = make_job(source="adzuna", source_job_id="9", url="https://a.in/9", location=None, city=None)
    _, new = db.upsert(score_job(other, profile))
    assert new  # not enough information to call it a duplicate


def test_closed_detection_and_reopen(db, profile):
    a, _ = db.upsert(score_job(make_job(source_job_id="1", url="https://e.com/1"), profile))
    b, _ = db.upsert(score_job(make_job(source_job_id="2", url="https://e.com/2"), profile))
    assert db.mark_missing_closed("greenhouse", "acme", {a}) == 1
    assert db.get(b)["status"] == "closed"
    db.upsert(score_job(make_job(source_job_id="2", url="https://e.com/2"), profile))
    assert db.get(b)["status"] == "discovered"


def test_user_statuses_survive_upsert(db, profile):
    job_id, _ = db.upsert(score_job(make_job(), profile))
    db.set_status(job_id, "applied_by_user")
    db.upsert(score_job(make_job(), profile))
    assert db.get(job_id)["status"] == "applied_by_user"


def test_expire_past_deadline(db, profile):
    db.upsert(score_job(make_job(closing_date="2020-01-01T00:00:00+00:00"), profile))
    assert db.expire_past_deadlines() == 1


def test_invalid_status_rejected(db, profile):
    job_id, _ = db.upsert(score_job(make_job(), profile))
    try:
        db.set_status(job_id, "applied")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_migrations_are_idempotent(tmp_path, profile):
    path = str(tmp_path / "jobs.db")
    d = Database(path)
    d.upsert(score_job(make_job(), profile))
    d.close()
    d2 = Database(path)  # re-opening must not drop data
    assert d2.schema_version() >= 1
    assert d2.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    d2.close()


def test_row_round_trip(db, profile):
    job_id, _ = db.upsert(score_job(make_job(), profile))
    job = row_to_job(db.get(job_id))
    assert job.match_reasons and isinstance(job.score_breakdown, dict)


def test_db_contains_no_candidate_private_data(db, profile):
    profile.contact = {"email": "someone@example.com"}
    db.upsert(score_job(make_job(salary_min=1_200_000, salary_max=1_500_000, salary_currency="INR"), profile))
    dump = "\n".join(db.conn.iterdump())
    assert "someone@example.com" not in dump
    assert "11" not in "".join(r["match_reasons"] or "" for r in db.conn.execute("SELECT match_reasons FROM jobs"))
