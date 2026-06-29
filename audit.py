"""Structured audit log for Provenance Guard (SQLite).

Every attribution decision and every appeal is captured as a structured row
(planning.md §1 step 5, §5). One table, one row per event:

  - entry_type = "classification"  -> a /submit decision
  - entry_type = "appeal"          -> a /appeal event (references the same content_id)

Signal scores are stored as a JSON blob so the schema doesn't change when we add
the second signal in Milestone 4.
"""

import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "audit_log.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the audit table if it doesn't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type      TEXT    NOT NULL,   -- 'classification' | 'appeal'
                content_id      TEXT    NOT NULL,
                creator_id      TEXT,
                timestamp       TEXT    NOT NULL,   -- ISO 8601 UTC
                attribution     TEXT,               -- likely_ai | likely_human | uncertain
                confidence      REAL,
                ai_probability  REAL,
                signals_json    TEXT,               -- {"llm_score":..,"stylo_score":..}
                status          TEXT,               -- classified | under_review
                appeal_reasoning TEXT
            )
            """
        )


def _now_iso():
    # e.g. 2025-04-01T14:32:10.123Z
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_classification(content_id, creator_id, attribution, confidence,
                       ai_probability, signals, status="classified"):
    """Write one classification row. `signals` is a dict, e.g.
    {"llm_score": 0.81, "stylo_score": 0.64}."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log
                (entry_type, content_id, creator_id, timestamp, attribution,
                 confidence, ai_probability, signals_json, status, appeal_reasoning)
            VALUES ('classification', ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (content_id, creator_id, _now_iso(), attribution,
             confidence, ai_probability, json.dumps(signals), status),
        )


def get_classification(content_id):
    """Return the most recent classification row for a content_id as a dict, or None."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM audit_log
            WHERE content_id = ? AND entry_type = 'classification'
            ORDER BY id DESC LIMIT 1
            """,
            (content_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_status(content_id, status):
    """Update the status on every row for a content_id (used by appeals)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE audit_log SET status = ? WHERE content_id = ?",
            (status, content_id),
        )


def log_appeal(original, creator_reasoning):
    """Write an appeal row that snapshots the original decision alongside the
    creator's reasoning. `original` is the dict from get_classification()."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log
                (entry_type, content_id, creator_id, timestamp, attribution,
                 confidence, ai_probability, signals_json, status, appeal_reasoning)
            VALUES ('appeal', ?, ?, ?, ?, ?, ?, ?, 'under_review', ?)
            """,
            (
                original["content_id"],
                original.get("creator_id"),
                _now_iso(),
                original.get("attribution"),
                original.get("confidence"),
                original.get("ai_probability"),
                json.dumps(original.get("signals", {})),
                creator_reasoning,
            ),
        )


def get_log(limit=50):
    """Return the most recent `limit` audit rows (newest first) as a list of dicts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row):
    """Turn a sqlite Row into a clean JSON-friendly dict, expanding signals_json."""
    d = dict(row)
    signals_json = d.pop("signals_json", None)
    d["signals"] = json.loads(signals_json) if signals_json else {}
    # Drop null appeal_reasoning on classification rows for a tidier payload.
    if d.get("appeal_reasoning") is None:
        d.pop("appeal_reasoning", None)
    return d
