"""Alert hooks — scaffold with empty integrations + real ntfy.sh hook.

All hooks are async so the checker loop never blocks.
"""

import asyncio
import logging
from typing import Optional

import httpx

import db
from config import load_config

logger = logging.getLogger("llm_monitor.alerts")

# Load and validate config once at import time
_CONFIG = load_config()
_NTFY_TOPIC = _CONFIG.alerts.ntfy_topic


# ---------------------------------------------------------------------------
# Placeholder integrations — fill in with your real credentials / webhooks
# ---------------------------------------------------------------------------

async def send_email_alert(endpoint_name: str, alert_type: str, message: str) -> None:
    """TODO: wire up SMTP or email provider."""
    logger.info("[EMAIL stub] %s | %s | %s", endpoint_name, alert_type, message)


async def send_slack_alert(endpoint_name: str, alert_type: str, message: str) -> None:
    """TODO: wire up Slack incoming webhook."""
    logger.info("[SLACK stub] %s | %s | %s", endpoint_name, alert_type, message)


async def send_pagerduty_alert(endpoint_name: str, alert_type: str, message: str) -> None:
    """TODO: wire up PagerDuty Events API."""
    logger.info("[PAGERDUTY stub] %s | %s | %s", endpoint_name, alert_type, message)


async def send_ntfy_alert(endpoint_name: str, alert_type: str, message: str) -> None:
    """Send alert to ntfy.sh (async, non-blocking)."""
    if not _NTFY_TOPIC:
        logger.debug("[NTFY] no topic configured — skipping")
        return
    body = f"{endpoint_name}: {alert_type}\n{message}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://ntfy.sh/{_NTFY_TOPIC}",
                content=body,
                timeout=10,
            )
            resp.raise_for_status()
        logger.info("[NTFY] alert sent for %s/%s", endpoint_name, alert_type)
    except Exception as exc:
        logger.warning("[NTFY] failed to send alert for %s/%s: %s", endpoint_name, alert_type, exc)


# ---------------------------------------------------------------------------
# Core alert logic used by the checker
# ---------------------------------------------------------------------------

_ALERT_TYPES = ("timeout", "error", "unexpected")


async def trigger_alert(
    endpoint_id: int,
    endpoint_name: str,
    alert_type: str,
    message: str,
    check_id: Optional[int] = None,
) -> None:
    """Called by the checker whenever an endpoint fails a health check.

    Deduplicates: only creates a new DB alert if there is no unresolved
    alert of the same type for this endpoint already.
    """
    if alert_type not in _ALERT_TYPES:
        alert_type = "error"

    active = await db.get_active_alerts()
    for a in active:
        if a["endpoint_id"] == endpoint_id and a["alert_type"] == alert_type:
            logger.debug("Alert already active for %s/%s — skipping", endpoint_name, alert_type)
            return

    # Enrich the alert with recent success rates for context.
    uptime_1h = await db.get_uptime(endpoint_id, hours=1)
    uptime_24h = await db.get_uptime(endpoint_id, hours=24)

    def _fmt(uptime: float | None) -> str:
        if uptime is None:
            return "N/A (no data)"
        return f"{uptime:.1f}%"

    rich_message = (
        f"{message}\n\n"
        f"Success rate (1h): {_fmt(uptime_1h)}\n"
        f"Success rate (24h): {_fmt(uptime_24h)}"
    )

    alert_id = await db.insert_alert(endpoint_id, alert_type, rich_message, check_id)
    logger.warning("Alert #%d triggered: %s/%s — %s", alert_id, endpoint_name, alert_type, rich_message)

    # Fire all alert integrations in the background so slow/down providers
    # never block the checker loop.
    asyncio.create_task(send_email_alert(endpoint_name, alert_type, rich_message))
    asyncio.create_task(send_slack_alert(endpoint_name, alert_type, rich_message))
    asyncio.create_task(send_pagerduty_alert(endpoint_name, alert_type, rich_message))
    asyncio.create_task(send_ntfy_alert(endpoint_name, alert_type, rich_message))


async def resolve_alert(endpoint_id: int, endpoint_name: str) -> None:
    """Called by the checker when an endpoint recovers (latest check is ok)."""
    await db.resolve_alerts(endpoint_id)
    logger.info("Alerts resolved for endpoint: %s", endpoint_name)
