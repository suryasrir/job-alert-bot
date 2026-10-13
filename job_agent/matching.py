"""Transparent, rule-based job scoring (0-100) with per-component explanations.

Unknown information is treated as unknown: it earns partial, neutral credit and is
listed in `job.unknowns`, never counted as a confirmed match.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from .config import Profile
from .models import Job
from .text import detect_city, mentions_india, parse_experience

# Title tiers: (pattern, fraction of the title weight, label)
TITLE_TIERS = [
    (r"\bios\b|\bswift\b|\biphone\b|\bapple platforms?\b", 1.0, "iOS/Swift title"),
    (r"\breact[\s-]?native\b", 0.9, "React Native title"),
    (r"\bmobile\b.*\b(sdk|framework)\b|\b(sdk|framework)\b.*\bmobile\b", 0.85, "mobile SDK title"),
    (r"\bmobile\b|\bapp developer\b|\bapplication developer\b", 0.7, "mobile title"),
]
# Titles that are clearly a different discipline even if they mention mobile/iOS.
EXCLUDE_TITLE = re.compile(
    r"\b(android|flutter|kotlin|designer|design|qa|sdet|test(ing)?|tester|sales|marketing|recruit\w*|"
    r"product manager|program manager|support|account|intern|analyst|writer|trainer)\b", re.I)
GENERIC_ENG_TITLE = re.compile(r"\b(software|frontend|front-end|full[\s-]?stack)\b.*\b(engineer|developer)\b", re.I)

# Technologies that indicate a different primary stack when required.
OTHER_STACK = ["kotlin", "android", "flutter", "dart", "java", "xamarin", "ionic", "unity", "c++"]

# Public channel: hands-on software development roles of any stack (iOS, Android, React Native,
# .NET, Node.js, Java, Python, full stack, frontend, backend, ...). Independent of the personal profile.
_TECH = (r"software|application|app|apps|mobile|web|front[\s-]?end|back[\s-]?end|full[\s-]?stack|platform|"
         r"ios|android|swift|kotlin|flutter|react|react[\s-]?native|angular|vue|node(?:\.?js)?|"
         r"\.net|dotnet|c#|java|python|django|golang|go|ruby|rails|php|laravel|scala|rust|c\+\+|"
         r"devops|cloud|sre|data|ml|ai|embedded|firmware|game|unity|salesforce|sap|qa|test|automation|sdet")
PUBLIC_TITLE = re.compile(
    r"\b(developer|programmer|sde|swe|sdet)\b|"
    r"(?<![a-z])(" + _TECH + r")(?![a-z])[\w\s/.+#-]{0,25}?\b(engineer|developer|dev|architect|intern|trainee)\b",
    re.I)
PUBLIC_EXCLUDE = re.compile(
    r"\b(manager|director|head|vp|vice president|chief|cto|sales|marketing|designer|recruit\w*|"
    r"account|analyst|customer support|support engineer|business development|consultant|"
    r"hardware|mechanical|electrical|civil|chemical|mechatronics)\b", re.I)


def public_location_ok(job: Job) -> bool:
    """India-based, or remote and open to India (or eligibility not restricted)."""
    loc = (job.location or "").lower()
    in_india = job.country == "India" or mentions_india(loc)
    if job.work_arrangement == "remote":
        stripped = re.sub(r"[()\s-]|remote", "", loc)
        return in_india or not stripped or bool(REMOTE_INDIA.search(loc) or REMOTE_REGION.search(loc))
    return in_india

SENIOR_TITLE =re.compile(r"\b(senior|sr\.?|lead|staff|principal|architect|manager|head)\b", re.I)
REQUIRED_SECTION = re.compile(r"(requirements?|qualifications?|must have|what you.ll need|you have|required)", re.I)
PREFERRED_SECTION = re.compile(r"(nice to have|preferred|bonus|good to have|plus)", re.I)

REMOTE_INDIA = re.compile(r"worldwide|anywhere|global|india", re.I)
REMOTE_REGION = re.compile(r"\bapac\b|\basia\b|south asia", re.I)


def _contains(text: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text) is not None


def _skill_terms(profile: Profile, skill: str) -> list[str]:
    return [s.lower() for s in profile.skill_synonyms.get(skill, [skill])]


def _split_required(description: str) -> tuple[str, str]:
    """Best-effort split into (required_text, preferred_text). Falls back to (all, '')."""
    lines = description.splitlines()
    required, preferred, mode = [], [], None
    for line in lines:
        stripped = line.strip()
        if len(stripped) < 60 and PREFERRED_SECTION.search(stripped):
            mode = "pref"
        elif len(stripped) < 60 and REQUIRED_SECTION.search(stripped):
            mode = "req"
        (preferred if mode == "pref" else required).append(line)
    return "\n".join(required), "\n".join(preferred)


def score_title(job: Job, profile: Profile) -> tuple[float, Optional[str]]:
    title = job.title.lower()
    for preferred in profile.preferred_titles:
        if re.sub(r"[^a-z ]", " ", preferred.lower()).split() == re.sub(r"[^a-z ]", " ", title).split():
            return 1.0, f"title matches '{preferred}'"
    if EXCLUDE_TITLE.search(title) and not re.search(r"\bios\b|\bswift\b|react[\s-]?native", title):
        return 0.0, None
    if re.search(r"\bandroid\b", title) and not re.search(r"\bios\b|react[\s-]?native|\bmobile\b", title):
        return 0.0, None
    for pattern, fraction, label in TITLE_TIERS:
        if re.search(pattern, title, re.I):
            if EXCLUDE_TITLE.search(title):
                fraction *= 0.6  # e.g. "iOS QA Engineer"
            return fraction, label
    if GENERIC_ENG_TITLE.search(title) and re.search(r"\bios\b|\bswift\b|react[\s-]?native", job.description.lower()):
        return 0.4, "general engineering title with iOS/React Native in the description"
    return 0.0, None


def score_skills(job: Job, profile: Profile) -> tuple[float, list[str], list[str]]:
    """Return (fraction, matched_skills, other_stack_required)."""
    text = f"{job.title}\n{job.description}\n{' '.join(job.required_skills)}".lower()
    required_text, _ = _split_required(text)
    matched = []
    weight = 0.0
    for skill in profile.skills:
        terms = _skill_terms(profile, skill)
        if any(_contains(text, t) for t in terms):
            matched.append(skill)
            weight += 1.0 if any(_contains(required_text, t) for t in terms) else 0.6
    core_hit = any(s in matched for s in ("Swift", "iOS", "React Native"))
    fraction = min(1.0, weight / 6.0)
    if not core_hit:
        fraction *= 0.4
    other = [t for t in OTHER_STACK if _contains(required_text, t)]
    # A role that requires a different primary stack and barely mentions ours.
    if other and not core_hit:
        fraction *= 0.5
    return fraction, matched, other


def score_experience(job: Job, profile: Profile) -> tuple[Optional[float], Optional[str], float]:
    """Return (fraction or None if unknown, concern, gap_years)."""
    lo, hi, snippet = parse_experience(job.description)
    if job.experience_text is None and snippet:
        job.experience_text = snippet
    if lo is None:
        if SENIOR_TITLE.search(job.title):
            return None, "Senior-level title; experience requirement not stated", 0.0
        return None, None, 0.0
    have = profile.total_experience_years
    gap = lo - have
    if gap <= 0:
        if hi is not None and hi + 3 < have:
            return 0.6, f"Role asks {snippet}; may be junior for you", 0.0
        return 1.0, None, 0.0
    if gap <= 1:
        return 0.6, f"Employer requests {snippet}; you have about {have:g} years", gap
    if gap <= 2:
        return 0.25, f"Employer requests {snippet}; about {gap:g} years short", gap
    return 0.0, f"Experience gap: employer requests {snippet}; you have about {have:g} years", gap


def score_location(job: Job, profile: Profile) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Return (fraction or None if unknown, reason, concern)."""
    loc = (job.location or "").lower()
    city = job.city or detect_city(loc)
    in_india = job.country == "India" or mentions_india(loc)
    preferred = [p.lower() for p in profile.preferred_locations]
    relocate = [p.lower() for p in profile.willing_to_relocate_to]

    if job.work_arrangement == "remote":
        if not profile.remote_ok:
            return 0.3, None, "Remote role"
        if loc in ("", "remote"):
            return 0.8, "remote", "Remote; eligible countries not stated"
        if in_india or REMOTE_INDIA.search(loc):
            return 1.0, "remote (India-eligible)", None
        if REMOTE_REGION.search(loc):
            return 0.7, "remote (APAC)", "Remote within a region; confirm India is eligible"
        return 0.1, None, f"Remote but restricted to: {job.location}"
    if city and (city.lower() in preferred or city.lower() in relocate):
        return 1.0, f"{city}" + (f" ({job.work_arrangement})" if job.work_arrangement != "unknown" else ""), None
    if in_india:
        if profile.open_to_other_india_locations:
            return 0.65, f"India ({city or job.location})", f"Located in {city or job.location}, not Hyderabad"
        return 0.2, None, f"Located in {city or job.location}"
    if not loc:
        return None, None, None
    return 0.0, None, f"Located outside India: {job.location}"


def _salary_lpa(job: Job) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Return (min, max) in lakhs per annum when the salary is in INR, else None."""
    if (job.salary_currency or "").upper() != "INR" or not (job.salary_min or job.salary_max):
        return None
    factor = 12 if job.salary_period == "month" else 1
    conv = lambda v: (v * factor) / 100_000 if v else None
    return conv(job.salary_min), conv(job.salary_max)


def score_salary(job: Job, profile: Profile) -> tuple[Optional[float], Optional[str], Optional[str]]:
    lpa = _salary_lpa(job)
    if lpa is None or profile.expected_ctc_lpa is None:
        return None, None, None
    lo, hi = lpa
    top = hi or lo
    if top >= profile.expected_ctc_lpa:
        return 1.0, "salary range meets your expectation", None
    if profile.current_ctc_lpa and top > profile.current_ctc_lpa * 1.2:
        return 0.5, None, "Salary range is below your expected CTC"
    return 0.0, None, "Salary range is well below your expected CTC"


def score_recency(job: Job, now: Optional[datetime] = None) -> Optional[float]:
    if not job.posted_at:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        days = (now - datetime.fromisoformat(job.posted_at)).total_seconds() / 86400
    except ValueError:
        return None
    for limit, value in ((3, 1.0), (7, 0.8), (14, 0.6), (30, 0.2)):
        if days <= limit:
            return value
    return 0.0


def score_job(job: Job, profile: Profile, now: Optional[datetime] = None) -> Job:
    """Score `job` in place and return it."""
    w = profile.weights
    reasons: list[str] = []
    concerns: list[str] = []
    unknowns: list[str] = []
    parts: dict[str, float] = {}

    t_frac, t_reason = score_title(job, profile)
    parts["title"] = t_frac * w["title"]
    if t_reason:
        reasons.append(t_reason)

    s_frac, matched, other = score_skills(job, profile)
    parts["skills"] = s_frac * w["skills"]
    if matched:
        reasons.append("skills: " + ", ".join(matched[:6]))
    if other:
        concerns.append("Also requires: " + ", ".join(other))

    e_frac, e_concern, gap = score_experience(job, profile)
    if e_frac is None:
        unknowns.append("experience requirement")
        e_frac = 0.5
    parts["experience"] = e_frac * w["experience"]
    if e_concern:
        concerns.append(e_concern)

    l_frac, l_reason, l_concern = score_location(job, profile)

    if l_frac is None:
        unknowns.append("location")
        l_frac = 0.4
    parts["location"] = l_frac * w["location"]
    if l_reason:
        reasons.append(l_reason)
    if l_concern:
        concerns.append(l_concern)

    r_frac = score_recency(job, now)
    if r_frac is None:
        unknowns.append("posting date")
        r_frac = 0.4
    parts["recency"] = r_frac * w["recency"]

    sal_frac, sal_reason, sal_concern = score_salary(job, profile)
    other_weight = sum(v for k, v in w.items() if k != "salary")
    if sal_frac is None:
        # Missing salary is neutral: rescale the other components to the full range.
        unknowns.append("salary")
        subtotal = sum(parts.values())
        total = subtotal / other_weight * sum(w.values()) if other_weight else 0.0
        parts["salary"] = 0.0
    else:
        parts["salary"] = sal_frac * w["salary"]
        total = sum(parts.values())
        if sal_reason:
            reasons.append(sal_reason)
        if sal_concern:
            concerns.append(sal_concern)

    # Normalise to 0-100 regardless of configured weight totals.
    total = max(0.0, min(100.0, total / sum(w.values()) * 100))
    # Caps: skills listed in a description cannot make an unrelated role relevant,
    # and a role the candidate cannot take (wrong country) cannot rank as a match.
    if t_frac == 0:
        total = min(total, 25.0)
    elif t_frac <= 0.4:
        total = min(total, 55.0)
    if l_frac <= 0.1:
        total = min(total, 40.0)

    th = profile.thresholds
    if gap > 2:
        category = "experience_mismatch"
    elif total >= th["strong"]:
        category = "strong"
    elif total >= th["good"]:
        category = "good"
    elif total >= th["possible"]:
        category = "possible"
    else:
        category = "low"

    # Public channel audience: any software developer who can work in India.
    job.public_ok = bool(
        PUBLIC_TITLE.search(job.title) and not PUBLIC_EXCLUDE.search(job.title) and public_location_ok(job)
    )

    job.score = round(total, 1)
    job.score_breakdown = {k: round(v, 1) for k, v in parts.items()}
    job.match_category = category
    job.match_reasons = reasons
    job.concerns = concerns
    job.unknowns = unknowns
    return job


CATEGORY_LABELS = {
    "strong": "Strong match",
    "good": "Good match",
    "possible": "Possible match",
    "low": "Low relevance",
    "experience_mismatch": "Experience mismatch",
}


def explain(job: Job) -> str:
    label = CATEGORY_LABELS.get(job.match_category or "", "Unscored")
    text = f"{label} ({job.score:g}/100): " + ("; ".join(job.match_reasons) or "few matching signals")
    if job.concerns:
        text += ". Potential concern: " + "; ".join(job.concerns)
    if job.unknowns:
        text += ". Unknown: " + ", ".join(job.unknowns)
    return text + "."
