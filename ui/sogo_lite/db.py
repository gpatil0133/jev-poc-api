"""SQLite storage for the mock (plan section 6). One file, one connection per thread."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional

from sogo_lite.config import get_settings

SCHEMA = """
-- Baseline tables (6.1)
CREATE TABLE IF NOT EXISTS project(
    survey_no INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
    anonymous INTEGER NOT NULL DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS page(
    id INTEGER PRIMARY KEY, survey_no INTEGER NOT NULL, ord INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS question(
    id INTEGER PRIMARY KEY, survey_no INTEGER NOT NULL, page_id INTEGER NOT NULL,
    qid TEXT NOT NULL, ord INTEGER NOT NULL, type TEXT NOT NULL, metric_kind TEXT,
    wording TEXT NOT NULL, required TEXT NOT NULL DEFAULT 'none',
    is_followup INTEGER NOT NULL DEFAULT 0, parent_qid TEXT,
    followup_min REAL, followup_max REAL, max_points REAL NOT NULL DEFAULT 0,
    checked_sig TEXT, UNIQUE(survey_no, qid));
CREATE TABLE IF NOT EXISTS answer_option(
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, text TEXT NOT NULL,
    ord INTEGER NOT NULL, points REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tag_category(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS tag(
    id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL, name TEXT NOT NULL,
    UNIQUE(category_id, name));
CREATE TABLE IF NOT EXISTS question_tag(
    question_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY(question_id, tag_id));
CREATE TABLE IF NOT EXISTS option_tag(
    option_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY(option_id, tag_id));
CREATE TABLE IF NOT EXISTS logic_rule(
    id INTEGER PRIMARY KEY, survey_no INTEGER NOT NULL, source_qid TEXT NOT NULL,
    op TEXT NOT NULL, operand TEXT, action TEXT NOT NULL, target TEXT NOT NULL, min_prob REAL);
CREATE TABLE IF NOT EXISTS alert_rule(
    id INTEGER PRIMARY KEY, survey_no INTEGER NOT NULL, name TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '[]', actions TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS response(
    id INTEGER PRIMARY KEY, survey_no INTEGER NOT NULL, started_at TEXT, submitted_at TEXT,
    offline INTEGER NOT NULL DEFAULT 0, respondent TEXT, page_ord INTEGER,
    shown TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'live');
CREATE TABLE IF NOT EXISTS response_answer(
    response_id INTEGER NOT NULL, qid TEXT NOT NULL, option_ids TEXT NOT NULL DEFAULT '[]',
    text TEXT, number REAL, PRIMARY KEY(response_id, qid));
CREATE TABLE IF NOT EXISTS post_populate(
    response_id INTEGER NOT NULL, qid TEXT NOT NULL, score REAL, feedback TEXT,
    status TEXT NOT NULL DEFAULT 'draft', source TEXT NOT NULL DEFAULT 'grader',
    detail TEXT, PRIMARY KEY(response_id, qid));
-- The three local sinks share one table; `sink` is email | webhook | salesforce.
CREATE TABLE IF NOT EXISTS sink_log(
    id INTEGER PRIMARY KEY, sink TEXT NOT NULL, survey_no INTEGER, rule_id INTEGER,
    response_id INTEGER, payload TEXT NOT NULL, created_at TEXT NOT NULL);

-- Tables added for the scopes (6.2)
CREATE TABLE IF NOT EXISTS class_set(
    survey_no INTEGER NOT NULL, qid TEXT NOT NULL, tasks TEXT NOT NULL DEFAULT '[]',
    custom TEXT NOT NULL DEFAULT '[]', act REAL, suggest REAL, model TEXT,
    question_hash TEXT, budget TEXT, synced_at TEXT, dirty INTEGER NOT NULL DEFAULT 1,
    sync_error TEXT, PRIMARY KEY(survey_no, qid));
CREATE TABLE IF NOT EXISTS classification_result(
    response_id INTEGER NOT NULL, qid TEXT NOT NULL, question_hash TEXT NOT NULL,
    answers TEXT NOT NULL, log_id INTEGER, created_at TEXT NOT NULL,
    PRIMARY KEY(response_id, qid));
CREATE TABLE IF NOT EXISTS suggestion(
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, survey_no INTEGER NOT NULL,
    target_type TEXT NOT NULL, target_id INTEGER NOT NULL, signature TEXT,
    proposed TEXT NOT NULL, confidence REAL, band TEXT,
    status TEXT NOT NULL DEFAULT 'pending', detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS key_point_set(
    survey_no INTEGER NOT NULL, qid TEXT NOT NULL, model_answer TEXT,
    key_points TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(survey_no, qid));
CREATE TABLE IF NOT EXISTS gateway_call_log(
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, module TEXT NOT NULL, trigger TEXT,
    method TEXT NOT NULL, endpoint TEXT NOT NULL, kind TEXT, survey_no INTEGER,
    response_id INTEGER, request TEXT, response TEXT, http_status INTEGER,
    client_ms REAL, latency_ms REAL, backend_ms REAL, fallback INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT, fault TEXT, question_hash TEXT, cached INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0, outcome TEXT);
CREATE TABLE IF NOT EXISTS pii_flag(
    response_id INTEGER NOT NULL, qid TEXT NOT NULL, kind TEXT NOT NULL, confidence REAL,
    band TEXT, log_id INTEGER, created_at TEXT NOT NULL, PRIMARY KEY(response_id, qid));
CREATE TABLE IF NOT EXISTS setting(name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS scenario_run(
    id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, results TEXT);
CREATE TABLE IF NOT EXISTS eval_run(
    id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, config TEXT, results TEXT, error TEXT);
"""

_local = threading.local()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        path = get_settings().db_file
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


# Columns added after the first cut: (table, column, definition).
ADDED_COLUMNS = [("question", "pii_kind", "TEXT")]


def init() -> None:
    conn().executescript(SCHEMA)
    for table, column, definition in ADDED_COLUMNS:
        if column not in {r["name"] for r in rows(f"PRAGMA table_info({table})")}:
            run(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, *args: Any) -> Optional[dict[str, Any]]:
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def val(sql: str, *args: Any) -> Any:
    r = conn().execute(sql, args).fetchone()
    return r[0] if r else None


def run(sql: str, *args: Any) -> int:
    """Execute a write; returns the last inserted row id."""
    return conn().execute(sql, args).lastrowid


def loads(text: Optional[str], default: Any = None) -> Any:
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ── settings (module toggles, fault switches, cached catalog) ───────────────

def get_setting(name: str, default: str = "") -> str:
    value = val("SELECT value FROM setting WHERE name=?", name)
    return default if value is None else value


def set_setting(name: str, value: str) -> None:
    run("INSERT INTO setting(name, value) VALUES(?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value", name, value)
