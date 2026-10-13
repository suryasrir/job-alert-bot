from datetime import datetime, timezone

from conftest import make_job, recent

from job_agent.matching import explain, score_job
from job_agent.text import parse_experience


def test_strong_ios_match_in_hyderabad(profile):
    job = score_job(make_job(), profile)
    assert job.match_category == "strong"
    assert job.score >= 75
    assert "Swift" in " ".join(job.match_reasons)
    assert "salary" in job.unknowns  # missing salary is unknown, not a mismatch


def test_score_is_bounded(profile):
    job = make_job(
        description="Swift iOS UIKit SwiftUI Objective-C React Native Core Data REST APIs Xcode Firebase Git "
                    "CocoaPods 2+ years experience",
        salary_min=1_200_000, salary_max=2_000_000, salary_currency="INR", salary_period="year",
    )
    scored = score_job(job, profile)
    assert 0 <= scored.score <= 100
    empty = score_job(make_job(title="Chef", description="", location=None, city=None, country=None,
                               posted_at=None, work_arrangement="unknown"), profile)
    assert 0 <= empty.score <= 100


def test_missing_salary_not_penalised(profile):
    no_salary = score_job(make_job(), profile).score
    good_salary = score_job(make_job(salary_min=1_000_000, salary_max=1_400_000,
                                     salary_currency="INR", salary_period="year"), profile).score
    low_salary = score_job(make_job(salary_min=300_000, salary_max=500_000,
                                    salary_currency="INR", salary_period="year"), profile).score
    assert good_salary >= no_salary - 0.5
    assert low_salary < no_salary


def test_experience_gap_penalised_and_categorised(profile):
    ok = score_job(make_job(), profile)
    gap = score_job(make_job(description="Requirements: 8+ years of iOS experience with Swift and UIKit."), profile)
    assert gap.score < ok.score
    assert gap.match_category == "experience_mismatch"
    assert any("8+ years" in c for c in gap.concerns)


def test_small_experience_gap_is_concern_not_mismatch(profile):
    job = score_job(make_job(description="Requirements: 4+ years of experience in Swift, UIKit."), profile)
    assert job.match_category != "experience_mismatch"
    assert any("4+ years" in c for c in job.concerns)


def test_unknown_experience_is_unknown(profile):
    job = score_job(make_job(description="We use Swift and UIKit."), profile)
    assert "experience requirement" in job.unknowns


def test_remote_india_matches_without_hyderabad(profile):
    job = score_job(make_job(location="Remote - India", city=None, work_arrangement="remote"), profile)
    assert job.score_breakdown["location"] == profile.weights["location"]


def test_onsite_is_not_treated_as_remote(profile):
    job = score_job(make_job(location="Bengaluru, India", city="Bengaluru", work_arrangement="onsite"), profile)
    assert job.score_breakdown["location"] < profile.weights["location"]
    assert "remote" not in " ".join(job.match_reasons).lower()


def test_remote_restricted_to_usa_is_capped(profile):
    job = score_job(make_job(location="Remote - USA", city=None, country=None, work_arrangement="remote"), profile)
    assert job.score <= 40
    assert any("restricted" in c for c in job.concerns)


def test_outside_india_onsite_capped(profile):
    job = score_job(make_job(location="London, UK", city=None, country=None, work_arrangement="onsite"), profile)
    assert job.score <= 40


def test_unrelated_title_with_skill_soup_is_low(profile):
    job = score_job(make_job(title="Senior Data Scientist",
                             description="Swift iOS React Native Firebase JavaScript Python", location="Remote - India",
                             work_arrangement="remote"), profile)
    assert job.score <= 25
    assert job.match_category == "low"


def test_android_only_title_scores_zero_title(profile):
    job = score_job(make_job(title="Android Developer", description="Kotlin, Java, Android SDK"), profile)
    assert job.score_breakdown["title"] == 0


def test_react_native_title_matches(profile):
    job = score_job(make_job(title="React Native Developer",
                             description="2+ years experience with React Native, JavaScript, Redux, REST APIs"), profile)
    assert job.score >= 60


def test_related_title_without_exact_phrase(profile):
    job = score_job(make_job(title="Software Engineer II - Apple Platforms"), profile)
    assert job.score_breakdown["title"] > 0


def test_recency(profile):
    fresh = score_job(make_job(posted_at=recent(1)), profile)
    old = score_job(make_job(posted_at=recent(60)), profile)
    assert fresh.score_breakdown["recency"] > old.score_breakdown["recency"]


def test_explain_mentions_concern(profile):
    text = explain(score_job(make_job(description="Requirements: 4+ years of experience in Swift."), profile))
    assert "Potential concern" in text


def test_parse_experience_variants():
    assert parse_experience("3-5 years of experience")[:2] == (3.0, 5.0)
    assert parse_experience("Minimum 4 years experience in iOS")[0] == 4.0
    assert parse_experience("5+ yrs exp")[0] == 5.0
    assert parse_experience("We were founded 20 years ago. You need 2+ years of experience.")[0] == 2.0
    assert parse_experience("no numbers here") == (None, None, None)


def test_custom_weights_still_bounded(profile):
    profile.weights = {"title": 50, "skills": 50, "experience": 0, "location": 0, "salary": 0, "recency": 0}
    job = score_job(make_job(), profile)
    assert 0 <= job.score <= 100


def test_string_and_placeholder_salaries(profile):
    job = make_job(salary_min="1", salary_max="1,500,000", salary_currency="INR", salary_period="year")
    assert job.salary_min is None and job.salary_max == 1_500_000
    scored = score_job(job, profile)
    assert scored.score_breakdown["salary"] > 0
    junk = make_job(salary_min="competitive", salary_max=None, salary_currency="INR")
    assert junk.salary_min is None and junk.salary_currency is None
    assert 0 <= score_job(junk, profile).score <= 100
