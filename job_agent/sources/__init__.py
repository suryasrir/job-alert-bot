"""Source adapter registry. To add a source, subclass SourceAdapter and register it here."""

from .aggregators import AdzunaSource, ArbeitnowSource, RemoteOKSource, RemotiveSource
from .ats import AshbySource, GreenhouseSource, LeverSource, SmartRecruitersSource
from .base import FetchResult, SourceAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    cls.name: cls
    for cls in (
        GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource,
        RemotiveSource, RemoteOKSource, ArbeitnowSource, AdzunaSource,
    )
}

# Portals without a public, permitted API. The bot links to their search pages
# instead of scraping them (see `python jobs.py links`).
MANUAL_SEARCH_PORTALS = {
    "Naukri": "https://www.naukri.com/ios-developer-jobs-in-hyderabad?experience=3",
    "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=iOS%20Developer&location=Hyderabad%2C%20Telangana%2C%20India&f_TPR=r86400",
    "Indeed India": "https://in.indeed.com/jobs?q=ios+developer&l=Hyderabad%2C+Telangana&fromage=3",
    "Foundit": "https://www.foundit.in/srp/results?query=ios%20developer&locations=Hyderabad",
    "Wellfound": "https://wellfound.com/role/l/ios-developer/india",
    "Cutshort": "https://cutshort.io/jobs/ios-developer-jobs-in-hyderabad",
    "Instahyre": "https://www.instahyre.com/ios-developer-jobs-in-hyderabad/",
    "Hirist": "https://www.hirist.tech/k/ios-developer-jobs",
}

__all__ = ["REGISTRY", "MANUAL_SEARCH_PORTALS", "FetchResult", "SourceAdapter"]
