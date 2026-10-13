"""Telegram (primary) and email (optional) notifications.

A job is only marked as notified after a provider confirms delivery.
"""

from __future__ import annotations

import html
import logging
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Optional, Protocol

from .http import FetchError, redact, request
from .matching import CATEGORY_LABELS
from .models import Job

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000  # Telegram's hard limit is 4096 characters


class Notifier(Protocol):
    name: str

    def configured(self) -> bool: ...
    def send(self, text: str) -> bool: ...


def _fmt_salary(job: Job) -> Optional[str]:
    if job.salary_text:
        return job.salary_text
    if job.salary_min or job.salary_max:
        cur = job.salary_currency or ""
        if cur.upper() == "INR":
            f = 12 if job.salary_period == "month" else 1
            rng = " - ".join(f"{v * f / 100_000:.1f}" for v in (job.salary_min, job.salary_max) if v)
            return f"₹{rng} LPA"
        rng = " - ".join(f"{v:,.0f}" for v in (job.salary_min, job.salary_max) if v)
        return f"{cur} {rng}".strip() + (f" per {job.salary_period}" if job.salary_period else "")
    return None


def format_job(job: Job, ai_summary: Optional[str] = None) -> str:
    """Telegram-HTML formatted alert for one job. All source text is escaped."""
    e = lambda s: html.escape(str(s), quote=False)
    label = CATEGORY_LABELS.get(job.match_category or "", "")
    lines = [f"<b>{e(job.title)}</b>", f"🏢 {e(job.company)}"]
    where = job.location or "Location not stated"
    arrangement = "" if job.work_arrangement == "unknown" else f" · {job.work_arrangement}"
    lines.append(f"📍 {e(where)}{e(arrangement)}")
    if job.experience_text:
        lines.append(f"🧭 Experience: {e(job.experience_text)}")
    salary = _fmt_salary(job)
    if salary:
        lines.append(f"💰 {e(salary)}")
    lines.append(f"⭐ {job.score:g}/100 · {e(label)}")
    for reason in job.match_reasons[:3]:
        lines.append(f"✅ {e(reason)}")
    for concern in job.concerns[:2]:
        lines.append(f"⚠️ {e(concern)}")
    if ai_summary:
        lines.append(f"🤖 AI note (unverified): {e(ai_summary[:300])}")
    if job.posted_at:
        lines.append(f"🗓 Posted {e(job.posted_at[:10])} · via {e(job.source)}")
    link = job.apply_url or job.url
    lines.append(f'🔗 <a href="{html.escape(link, quote=True)}">Open application page</a>')
    lines.append(f"<code>id {e(job.id)}</code>")
    return "\n".join(lines)


# Sources whose terms ask for visible attribution when their jobs are shared.
SOURCE_CREDITS = {
    "remotive": ("Remotive", "https://remotive.com"),
    "remoteok": ("Remote OK", "https://remoteok.com"),
    "adzuna": ("Adzuna", "https://www.adzuna.in"),
    "arbeitnow": ("Arbeitnow", "https://www.arbeitnow.com"),
}


# (pattern checked against the title, hashtag). Title only, so tags describe the role itself.
STACK_TAGS = [
    (r"\bios\b|\bswift\b|\biphone\b", "#iOS"),
    (r"\bandroid\b|\bkotlin\b", "#Android"),
    (r"react[\s-]?native", "#ReactNative"),
    (r"\bflutter\b", "#Flutter"),
    (r"\bmobile\b", "#Mobile"),
    (r"\.net\b|\bdotnet\b|\bc#", "#DotNet"),
    (r"\bnode(\.?js)?\b", "#NodeJS"),
    (r"\bjava\b(?!script)", "#Java"),
    (r"\bpython\b|\bdjango\b", "#Python"),
    (r"\bgolang\b|\bgo\b", "#Golang"),
    (r"\bphp\b|\blaravel\b", "#PHP"),
    (r"\bfull[\s-]?stack\b", "#FullStack"),
    (r"\bfront[\s-]?end\b|\breact(?![\s-]?native)\b|\bangular\b|\bvue\b", "#Frontend"),
    (r"\bback[\s-]?end\b", "#Backend"),
    (r"\bdevops\b|\bsre\b|\bcloud\b", "#DevOps"),
    (r"\bdata\b", "#Data"),
    (r"\bml\b|\bai\b|machine learning", "#AI"),
    (r"\bqa\b|\btest|\bsdet\b|automation", "#QA"),
    (r"\bintern(ship)?\b|\bfresher\b|\bgraduate\b", "#Freshers"),
]


def _hashtags(job: Job) -> str:
    title = job.title.lower()
    tags = [tag for pattern, tag in STACK_TAGS if re.search(pattern, title)]
    if not tags:
        tags.append("#SoftwareJobs")
    if job.city:
        tags.append("#" + re.sub(r"\W", "", job.city))
    if job.work_arrangement == "remote":
        tags.append("#Remote")
    if job.country == "India" or job.city:
        tags.append("#IndiaJobs")
    return " ".join(dict.fromkeys(tags))


def format_public_post(job: Job, footer: str = "") -> str:
    """Channel post for a general audience: job facts only, no personal score or notes."""
    e = lambda s: html.escape(str(s), quote=False)
    lines = [f"<b>{e(job.title)}</b>", f"🏢 {e(job.company)}"]
    arrangement = "" if job.work_arrangement == "unknown" else f" · {job.work_arrangement.capitalize()}"
    lines.append(f"📍 {e(job.location or 'Location not stated')}{e(arrangement)}")
    if job.experience_text:
        lines.append(f"🧭 Experience: {e(job.experience_text)}")
    salary = _fmt_salary(job)
    if salary:
        lines.append(f"💰 {e(salary)}")
    if job.employment_type:
        lines.append(f"🕒 {e(job.employment_type)}")
    if job.posted_at:
        lines.append(f"🗓 Posted {e(job.posted_at[:10])}")
    link = job.apply_url or job.url
    lines.append(f'\n👉 <a href="{html.escape(link, quote=True)}">Apply on the employer\'s site</a>')
    credit = SOURCE_CREDITS.get(job.source)
    if credit:
        lines.append(f'Source: <a href="{credit[1]}">{credit[0]}</a>')
    tags = _hashtags(job)
    if tags:
        lines.append(f"\n{tags}")
    if footer:
        lines.append(e(footer))
    return "\n".join(lines)


def post_to_channel(jobs: list[Job], notifier: "TelegramNotifier", footer: str = "",
                    delay: float = 3.0) -> list[str]:
    """Post each job to the channel; returns IDs Telegram confirmed. Spaced to respect rate limits."""
    posted = []
    for i, job in enumerate(jobs):
        if i:
            time.sleep(delay)
        if notifier.send(format_public_post(job, footer)):
            posted.append(job.id)
    return posted


def format_summary(jobs: list[Job], sent: int) -> str:
    rest = jobs[sent:]
    lines = [f"<b>{len(rest)} more matching job(s)</b> (not sent individually):"]
    for job in rest[:25]:
        lines.append(f"• {job.score:g} · {html.escape(job.title)} — {html.escape(job.company)}")
    if len(rest) > 25:
        lines.append(f"…and {len(rest) - 25} more. Run <code>python jobs.py list</code> to see all.")
    return "\n".join(lines)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.configured():
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text[:TELEGRAM_LIMIT], "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        try:
            resp = request("POST", url, json=payload, retries=2)
            ok = resp.json().get("ok") is True
        except (FetchError, ValueError) as exc:
            log.warning("Telegram send failed: %s", redact(str(exc)).replace(self.token or "\0", "***"))
            return False
        if not ok:
            log.warning("Telegram did not confirm delivery")
        return ok


class EmailNotifier:
    """Optional digest email via SMTP (e.g. Gmail with an app password)."""

    name = "email"

    def __init__(self):
        self.host = os.environ.get("SMTP_HOST")
        self.port = int(os.environ.get("SMTP_PORT", "587") or 587)
        self.user = os.environ.get("SMTP_USER")
        self.password = os.environ.get("SMTP_PASSWORD")
        self.to = os.environ.get("EMAIL_TO")

    def configured(self) -> bool:
        return all((self.host, self.user, self.password, self.to))

    def send(self, text: str) -> bool:
        if not self.configured():
            return False
        msg = EmailMessage()
        msg["Subject"] = "New job matches"
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content("This email is best viewed as HTML.")
        msg.add_alternative(f"<html><body><pre style='white-space:pre-wrap;font-family:sans-serif'>{text}</pre></body></html>",
                            subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("Email send failed: %s", type(exc).__name__)
            return False


def available_notifiers() -> list[Notifier]:
    return [n for n in (TelegramNotifier(), EmailNotifier()) if n.configured()]


def notify_jobs(
    jobs: list[Job],
    notifiers: list[Notifier],
    max_individual: int,
    ai_summaries: Optional[dict[str, str]] = None,
) -> tuple[list[str], str]:
    """Send alerts. Returns (ids_confirmed_delivered, channel_names).

    Telegram gets one message per job (up to `max_individual`) and a summary for
    the rest. Email gets a single digest. A job counts as delivered when at
    least one channel confirmed it.
    """
    ai_summaries = ai_summaries or {}
    delivered: set[str] = set()
    channels: list[str] = []
    for notifier in notifiers:
        if notifier.name == "telegram":
            ok_any = False
            for job in jobs[:max_individual]:
                if notifier.send(format_job(job, ai_summaries.get(job.id))):
                    delivered.add(job.id)
                    ok_any = True
            if len(jobs) > max_individual and notifier.send(format_summary(jobs, max_individual)):
                delivered.update(j.id for j in jobs[max_individual:])
                ok_any = True
            if ok_any:
                channels.append(notifier.name)
        else:
            body = "\n\n".join(format_job(j, ai_summaries.get(j.id)) for j in jobs[:50])
            if notifier.send(body):
                delivered.update(j.id for j in jobs[:50])
                channels.append(notifier.name)
    return [j.id for j in jobs if j.id in delivered], ",".join(channels)
