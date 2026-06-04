"""Daily email digest: top signals + recent items, rendered to HTML over SMTP."""
from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select

from app.config import config
from app.models import Digest, Item, Signal

logger = logging.getLogger(__name__)


def build_digest(session, hours: int = 24) -> dict:
    """Assemble the digest payload: top signals + newest items in the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    signals = (
        session.execute(
            select(Signal)
            .where(Signal.created_at >= cutoff)
            .order_by(Signal.confidence.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    items = (
        session.execute(
            select(Item)
            .where(Item.ingested_at >= cutoff)
            .order_by(Item.ingested_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "signals": [
            {
                "summary": s.summary,
                "type": s.signal_type,
                "direction": s.direction,
                "confidence": s.confidence,
                "speculative": s.speculative,
                "entities": json.loads(s.entities_json or "{}"),
                "url": s.item.url if s.item else "",
            }
            for s in signals
        ],
        "items": [
            {"title": i.title, "url": i.url, "source": i.source.name if i.source else ""}
            for i in items
        ],
    }


def render_html(payload: dict) -> str:
    rows = []
    for s in payload["signals"]:
        flag = " ⚠️" if s["speculative"] else ""
        tickers = ", ".join(s["entities"].get("tickers", []))
        rows.append(
            f'<li><b>[{s["type"]}/{s["direction"]}]</b>{flag} '
            f'{s["summary"]} '
            f'<span style="color:#888">({s["confidence"]:.0%}{" · " + tickers if tickers else ""})</span> '
            f'<a href="{s["url"]}">link</a></li>'
        )
    item_rows = [
        f'<li><a href="{i["url"]}">{i["title"]}</a> <span style="color:#888">— {i["source"]}</span></li>'
        for i in payload["items"]
    ]
    return (
        "<html><body style='font-family:system-ui,Arial,sans-serif'>"
        "<h2>Research Intelligence Digest</h2>"
        f"<p style='color:#888'>Generated {payload['generated']}</p>"
        "<h3>Top Signals</h3><ul>" + ("".join(rows) or "<li>No signals.</li>") + "</ul>"
        "<h3>New Items</h3><ul>" + ("".join(item_rows) or "<li>No items.</li>") + "</ul>"
        "</body></html>"
    )


def send_email(html: str, subject: str) -> bool:
    if not (config.SMTP_USER and config.SMTP_PASS and config.DIGEST_EMAIL):
        logger.warning("SMTP not configured; skipping email send")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = config.DIGEST_EMAIL
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception:
        logger.exception("Digest email send failed")
        return False


def send_daily_digest(session) -> dict:
    """Build, persist, and email the digest. Returns the payload."""
    payload = build_digest(session)
    html = render_html(payload)
    record = Digest(content_json=json.dumps(payload))
    session.add(record)
    session.flush()
    if send_email(html, f"Research Digest — {datetime.now(timezone.utc):%Y-%m-%d}"):
        record.sent_at = datetime.now(timezone.utc)
    return payload
