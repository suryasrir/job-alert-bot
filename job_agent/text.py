"""Text helpers: HTML to text, URL canonicalisation, experience and location parsing."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

# Only parameters that are known to be pure tracking noise are removed.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref_src", "trk", "trackingid",
}

INDIA_CITIES = {
    "hyderabad": "Hyderabad", "secunderabad": "Hyderabad",
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru",
    "mumbai": "Mumbai", "navi mumbai": "Mumbai", "pune": "Pune",
    "chennai": "Chennai", "gurugram": "Gurugram", "gurgaon": "Gurugram",
    "noida": "Noida", "new delhi": "Delhi", "delhi": "Delhi",
    "kolkata": "Kolkata", "ahmedabad": "Ahmedabad", "kochi": "Kochi",
    "jaipur": "Jaipur", "coimbatore": "Coimbatore", "chandigarh": "Chandigarh",
    "indore": "Indore", "trivandrum": "Thiruvananthapuram",
    "thiruvananthapuram": "Thiruvananthapuram", "visakhapatnam": "Visakhapatnam",
    "vizag": "Visakhapatnam", "mohali": "Mohali", "vijayawada": "Vijayawada",
}


def html_to_text(raw: Optional[str]) -> str:
    """Convert (possibly entity-escaped) HTML to plain text. Never executes anything."""
    if not raw:
        return ""
    raw = html.unescape(raw)  # Greenhouse double-escapes its HTML
    text = BeautifulSoup(raw, "html.parser").get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def html_to_inline(raw: Optional[str]) -> str:
    """Single-line text for titles, company names and locations."""
    if not raw:
        return ""
    text = BeautifulSoup(html.unescape(raw), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """Lower-case the host, drop fragments and known tracking params, keep everything else."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalize_key(text: Optional[str]) -> str:
    """Loose key for fuzzy duplicate comparison (company/title/location)."""
    text = (text or "").lower()
    text = re.sub(r"\b(pvt|private|ltd|limited|inc|llc|technologies|technology)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_NUM = r"(\d{1,2}(?:\.\d)?)"
_YEARS = r"(?:\+?\s*(?:years?|yrs?))"
EXPERIENCE_PATTERNS = [
    # 3-5 years / 3 to 5 yrs
    re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*" + _YEARS, re.I),
    # 4+ years / minimum 4 years / at least 4 years
    re.compile(r"(?:minimum(?: of)?|at least|min\.?)?\s*" + _NUM + r"\s*\+?\s*" + _YEARS, re.I),
]


def parse_experience(text: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (min_years, max_years, matched_snippet) from a job description.

    Uses the smallest plausible lower bound found near the word "experience",
    falling back to any "N years" mention. Returns (None, None, None) when unknown.
    """
    if not text:
        return None, None, None
    candidates: list[tuple[float, Optional[float], str, bool]] = []
    for line in re.split(r"[\n.;•]", text):
        near_exp = bool(re.search(r"experience|exp\b", line, re.I))
        for m in EXPERIENCE_PATTERNS[0].finditer(line):
            lo, hi = float(m.group(1)), float(m.group(2))
            if 0 <= lo <= 25 and lo <= hi <= 30:
                candidates.append((lo, hi, m.group(0).strip(), near_exp))
        if not any(c[2] in line for c in candidates):
            for m in EXPERIENCE_PATTERNS[1].finditer(line):
                lo = float(m.group(1))
                if 0 < lo <= 25:
                    candidates.append((lo, None, m.group(0).strip(), near_exp))
    if not candidates:
        return None, None, None
    preferred = [c for c in candidates if c[3]] or candidates
    lo, hi, snippet, _ = min(preferred, key=lambda c: c[0])
    return lo, hi, snippet


def detect_city(location: Optional[str]) -> Optional[str]:
    loc = (location or "").lower()
    for key, city in INDIA_CITIES.items():
        if re.search(r"\b" + re.escape(key) + r"\b", loc):
            return city
    return None


def mentions_india(location: Optional[str]) -> bool:
    loc = (location or "").lower()
    return bool(re.search(r"\bindia\b", loc)) or detect_city(loc) is not None


def detect_arrangement(*texts: Optional[str]) -> str:
    joined = " ".join(t for t in texts if t).lower()
    if re.search(r"\bhybrid\b", joined):
        return "hybrid"
    if re.search(r"\bremote\b|work from home|\bwfh\b", joined):
        return "remote"
    if re.search(r"\bon-?site\b|\bin-?office\b|work from office", joined):
        return "onsite"
    return "unknown"


def to_iso(value) -> Optional[str]:
    """Convert epoch seconds/ms or an ISO string to an ISO-8601 UTC string."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_safe_http_url(url: Optional[str]) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and " " not in url
