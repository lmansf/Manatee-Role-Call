"""The alert: one email per run through Resend, only when something new opened (spec §5).

The email lists new incidents and new quarantined observations by source, check, date and
value. It never carries report text. Resend's API takes a JSON POST to /emails with a Bearer
key; the fields match `Emails.SendParams` in resend-python (`from`, `to`, `subject`, `text`).
"""
from __future__ import annotations

import hashlib
from typing import Any

import requests

from roll_call import config
from roll_call.quality import store

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 30


def _incident_line(i: dict[str, Any]) -> str:
    return f"- {i['source']} / {i['check_name']}: {i.get('detail') or 'failed'}"


def _quarantine_line(q: dict[str, Any]) -> str:
    return f"- {q['obs_id']} ({q['check_name']}): {q.get('value')}"


def compose(report) -> tuple[str, str]:
    """Subject and plain-text body for the new items in a report."""
    incidents, quarantine = report.new_incidents, report.new_quarantine
    parts = []
    if incidents:
        parts.append(f"{len(incidents)} new incident{'s' if len(incidents) != 1 else ''}")
    if quarantine:
        parts.append(f"{len(quarantine)} new quarantined observation"
                     f"{'s' if len(quarantine) != 1 else ''}")
    subject = f"Roll Call {report.today}: " + ", ".join(parts)
    lines = [f"Checks for {report.today}.", ""]
    if incidents:
        lines += ["New incidents:", *map(_incident_line, incidents), ""]
    if quarantine:
        lines += ["New quarantined observations (clear them in the notebook):",
                  *map(_quarantine_line, quarantine), ""]
    return subject, "\n".join(lines)


def send_alert(report, con=None, session: requests.Session | None = None) -> bool:
    """Send one email listing the report's new incidents and quarantine. Returns whether it sent.

    Nothing new means no email. After Resend accepts the message, alerted_at is stamped on each
    listed item, using `con` or the connection the report came from. A failed send raises, so
    the daily run shows the failure, and the items stay unalerted.
    """
    if not report.new_incidents and not report.new_quarantine:
        return False
    subject, text = compose(report)
    ids = sorted([i["incident_id"] for i in report.new_incidents]
                 + [q["obs_id"] for q in report.new_quarantine])
    payload = {
        "from": config.env("ALERT_EMAIL_FROM"),
        "to": [config.env("ALERT_EMAIL_TO")],
        "subject": subject,
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {config.env('RESEND_API_KEY')}",
        "Accept": "application/json",
        # A retry of the same alert is sent once.
        "Idempotency-Key": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
    }
    http = session or requests.Session()
    resp = http.post(RESEND_URL, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()

    con = con if con is not None else getattr(report, "con", None)
    if con is not None:
        store.mark_alerted(con, [i["incident_id"] for i in report.new_incidents],
                           [q["obs_id"] for q in report.new_quarantine])
    return True
