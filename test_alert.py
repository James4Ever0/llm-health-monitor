"""Test alert firing without touching the DB or frontend.

Imports the notification hooks from alerts.py and sends a fake alert.
No database writes, no HTTP API calls to the dashboard.
"""

import asyncio

from alerts import send_email_alert, send_slack_alert, send_pagerduty_alert, send_ntfy_alert


async def main():
    endpoint_name = "Test Endpoint"
    alert_type = "timeout"
    message = "This is a fake alert for testing integrations."

    print(f"Firing fake alert: {endpoint_name} / {alert_type}")

    # Run all notification hooks concurrently
    await asyncio.gather(
        send_email_alert(endpoint_name, alert_type, message),
        send_slack_alert(endpoint_name, alert_type, message),
        send_pagerduty_alert(endpoint_name, alert_type, message),
        send_ntfy_alert(endpoint_name, alert_type, message),
    )

    print("Done. If ntfy is configured, you should see a notification on your channel.")


if __name__ == "__main__":
    asyncio.run(main())
