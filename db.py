"""Async SQLite database layer for LLM health monitor."""

import aiosqlite
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional
import json

DB_PATH = "llm_monitor.db"

# Load timezone from config so "last 1h" is relative to the configured zone.
# SQLite CURRENT_TIMESTAMP is always UTC, so we compute the cutoff in the
# user's zone, convert to UTC, then compare as 'YYYY-MM-DD HH:MM:SS'.
try:
    from config import load_config
    _TZ_NAME = load_config().server.timezone
except Exception:
    _TZ_NAME = "UTC"


def _now_in_tz() -> datetime:
    """Return current time in the configured timezone."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(_TZ_NAME)
    except Exception:
        tz = dt_timezone.utc
    return datetime.now(tz)


def _format_for_sqlite(dt: datetime) -> str:
    """Convert a timezone-aware datetime to UTC and format as SQLite stores it."""
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def init_db() -> None:
    """Create tables if they don't exist and apply migrations."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id TEXT UNIQUE,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model TEXT NOT NULL,
                endpoint_type TEXT DEFAULT 'llm',
                enabled INTEGER DEFAULT 1,
                check_override TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                latency_ms REAL,
                status TEXT NOT NULL,
                response_text TEXT,
                request_body TEXT,
                response_body TEXT,
                alert_triggered INTEGER DEFAULT 0,
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id INTEGER,
                endpoint_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL,
                message TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_checks_endpoint_time
            ON checks(endpoint_id, timestamp)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_endpoint_unresolved
            ON alerts(endpoint_id, resolved_at)
            WHERE resolved_at IS NULL
        """)
        # Migrations
        migrations = [
            ("check_override", "TEXT DEFAULT '{}'"),
            ("monitor_id", "TEXT UNIQUE"),
            ("request_body", "TEXT"),
            ("response_body", "TEXT"),
            ("endpoint_type", "TEXT DEFAULT 'llm'"),
        ]
        for col, dtype in migrations:
            try:
                await db.execute(f"ALTER TABLE endpoints ADD COLUMN {col} {dtype};")
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute(f"ALTER TABLE checks ADD COLUMN {col} {dtype};")
            except aiosqlite.OperationalError:
                pass
        await db.commit()


async def upsert_endpoint(
    monitor_id: str,
    name: str,
    base_url: str,
    api_key: str,
    model: str,
    endpoint_type: str = "llm",
    enabled: bool = True,
    check_override: dict | None = None,
) -> int:
    """Insert or update an endpoint keyed by monitor_id, return its id."""
    override_json = json.dumps(check_override or {})
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """UPDATE endpoints
               SET name = ?, base_url = ?, api_key = ?, model = ?, endpoint_type = ?, enabled = ?, check_override = ?
               WHERE monitor_id = ?""",
            (name, base_url, api_key, model, endpoint_type, 1 if enabled else 0, override_json, monitor_id),
        )
        cursor = await db.execute("SELECT id FROM endpoints WHERE monitor_id = ?", (monitor_id,))
        row = await cursor.fetchone()
        if row is None:
            cursor = await db.execute(
                """INSERT INTO endpoints
                   (monitor_id, name, base_url, api_key, model, endpoint_type, enabled, check_override)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (monitor_id, name, base_url, api_key, model, endpoint_type, 1 if enabled else 0, override_json),
            )
            ep_id = cursor.lastrowid
        else:
            ep_id = row["id"]
        await db.commit()
        return ep_id


async def get_enabled_endpoints() -> list[dict]:
    """Return all enabled endpoints with parsed check_override."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, monitor_id, name, base_url, api_key, model, endpoint_type, check_override
               FROM endpoints WHERE enabled = 1"""
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            ep = dict(row)
            try:
                ep["check_override"] = json.loads(ep.get("check_override", "{}"))
            except json.JSONDecodeError:
                ep["check_override"] = {}
            results.append(ep)
        return results


async def get_endpoint_by_monitor_id(monitor_id: str) -> dict | None:
    """Return a single endpoint by its monitor_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, monitor_id, name, base_url, api_key, model, endpoint_type, check_override
               FROM endpoints WHERE monitor_id = ?""",
            (monitor_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        ep = dict(row)
        try:
            ep["check_override"] = json.loads(ep.get("check_override", "{}"))
        except json.JSONDecodeError:
            ep["check_override"] = {}
        return ep


async def insert_check(
    endpoint_id: int,
    latency_ms: Optional[float],
    status: str,
    response_text: Optional[str],
    request_body: Optional[str] = None,
    response_body: Optional[str] = None,
    alert_triggered: bool = False,
) -> int:
    """Insert a check result, return the check id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO checks (endpoint_id, latency_ms, status, response_text, request_body, response_body, alert_triggered)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (endpoint_id, latency_ms, status, response_text, request_body, response_body, 1 if alert_triggered else 0),
        )
        await db.commit()
        return cursor.lastrowid


async def get_latest_check(endpoint_id: int) -> Optional[dict]:
    """Return the most recent check for an endpoint."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM checks
               WHERE endpoint_id = ?
               ORDER BY timestamp DESC
               LIMIT 1""",
            (endpoint_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_history(endpoint_id: int, hours: int = 24) -> list[dict]:
    """Return check history for an endpoint over the last N hours."""
    since = _format_for_sqlite(_now_in_tz() - timedelta(hours=hours))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM checks
               WHERE endpoint_id = ? AND timestamp >= ?
               ORDER BY timestamp ASC""",
            (endpoint_id, since),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_uptime(endpoint_id: int, hours: int = 24) -> float | None:
    """Return uptime percentage (0.0-100.0) for an endpoint over the last N hours.

    Returns None when there are no checks in the requested window.
    """
    since = _format_for_sqlite(_now_in_tz() - timedelta(hours=hours))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as ok
               FROM checks
               WHERE endpoint_id = ? AND timestamp >= ?""",
            (endpoint_id, since),
        )
        row = await cursor.fetchone()
        total, ok = row[0], row[1]
        if not total:
            return None
        if ok is None:
            ok = 0
        return (ok / total) * 100.0


async def insert_alert(
    endpoint_id: int,
    alert_type: str,
    message: str,
    check_id: Optional[int] = None,
) -> int:
    """Insert an alert, return the alert id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO alerts (endpoint_id, alert_type, message, check_id)
               VALUES (?, ?, ?, ?)""",
            (endpoint_id, alert_type, message, check_id),
        )
        await db.commit()
        return cursor.lastrowid


async def resolve_alerts(endpoint_id: int) -> None:
    """Mark all unresolved alerts for an endpoint as resolved."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE alerts
               SET resolved_at = CURRENT_TIMESTAMP
               WHERE endpoint_id = ? AND resolved_at IS NULL""",
            (endpoint_id,),
        )
        await db.commit()


async def get_active_alerts() -> list[dict]:
    """Return all unresolved alerts joined with endpoint names."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT a.*, e.name as endpoint_name, e.monitor_id
               FROM alerts a
               JOIN endpoints e ON a.endpoint_id = e.id
               WHERE a.resolved_at IS NULL
               ORDER BY a.sent_at DESC"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_recent_alerts(limit: int = 50) -> list[dict]:
    """Return the most recent alerts (resolved or not)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT a.*, e.name as endpoint_name, e.monitor_id
               FROM alerts a
               JOIN endpoints e ON a.endpoint_id = e.id
               ORDER BY a.sent_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_recent_checks(limit: int = 200) -> list[dict]:
    """Return the most recent checks joined with endpoint details for debug logging."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT
                   c.id,
                   c.timestamp,
                   c.latency_ms,
                   c.status,
                   c.response_text,
                   c.request_body,
                   c.response_body,
                   e.id as endpoint_id,
                   e.monitor_id,
                   e.name as endpoint_name,
                   e.endpoint_type,
                   e.base_url,
                   e.model,
                   e.check_override
               FROM checks c
               JOIN endpoints e ON c.endpoint_id = e.id
               ORDER BY c.timestamp DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            rec = dict(row)
            try:
                rec["check_override"] = json.loads(rec.get("check_override", "{}"))
            except json.JSONDecodeError:
                rec["check_override"] = {}
            results.append(rec)
        return results
