"""Shared helpers for the simulators: args, survey loading, latency summary, CSV output."""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "survey-chat" / "raw_data" / "corpno_77245"
OUT_DIR = Path(__file__).parent / "out"
DEFAULT_TENANT = "77245"
# An answer column is treated as open text when its answers average this many words.
OPEN_TEXT_MIN_AVG_WORDS = 3.0


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010", help="gateway URL")
    parser.add_argument("--token", default=DEFAULT_TENANT,
                        help="Bearer value: a JWT, or a corp_no when the gateway runs with DEV_AUTH_BYPASS")
    return parser


def client(args: argparse.Namespace, timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(base_url=args.base_url, timeout=timeout,
                        headers={"Authorization": f"Bearer {args.token}"})


def async_client(args: argparse.Namespace, timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=args.base_url, timeout=timeout,
                             headers={"Authorization": f"Bearer {args.token}"},
                             limits=httpx.Limits(max_connections=200))


def clean_text(text: Optional[str]) -> str:
    """Survey titles are stored as HTML."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def load_survey(survey_no: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Structure and responses of one raw_data export."""
    structure = json.loads((RAW_DATA_DIR / f"{survey_no}_SurveyStructure.json").read_text(encoding="utf-8-sig"))
    export = json.loads((RAW_DATA_DIR / f"Export_{survey_no}_77245.json").read_text(encoding="utf-8-sig"))
    return structure["SurveyData"][0], export.get("Data") or []


def open_text_questions(survey: dict[str, Any], responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Text questions whose answers look like comments (not dates or single words)."""
    found = []
    for question in survey["questionData"]:
        if question.get("questionType") != "T":
            continue
        key = f"qno{question['questionID']}"
        answers = [r[key] for r in responses if isinstance(r.get(key), str) and r[key].strip()]
        if answers and sum(len(a.split()) for a in answers) / len(answers) >= OPEN_TEXT_MIN_AVG_WORDS:
            found.append({"question_id": str(question["questionID"]), "key": key,
                          "title": clean_text(question["questionTitle"]), "answers": len(answers)})
    return found


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(pct / 100.0 * len(ordered)) - 1))]


def latency_summary(latencies_ms: list[float]) -> dict[str, float]:
    return {"n": len(latencies_ms), "p50_ms": round(percentile(latencies_ms, 50), 1),
            "p95_ms": round(percentile(latencies_ms, 95), 1),
            "max_ms": round(max(latencies_ms), 1) if latencies_ms else 0.0}


def band_counts(rows: list[dict[str, Any]], suffix: str = ".band") -> dict[str, dict[str, int]]:
    """Per-question band counts from flattened CSV rows."""
    counts: dict[str, Counter] = {}
    for row in rows:
        for key, value in row.items():
            if key.endswith(suffix):
                counts.setdefault(key[:-len(suffix)], Counter())[value] += 1
    return {qid: dict(c) for qid, c in counts.items()}


def flatten_answers(answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for qid, answer in answers.items():
        flat[f"{qid}.label"] = answer.get("label")
        flat[f"{qid}.confidence"] = answer.get("confidence")
        flat[f"{qid}.band"] = answer.get("band")
    return flat


def write_csv(name: str, rows: list[dict[str, Any]]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def print_summary(title: str, summary: dict[str, Any], csv_path: Optional[Path] = None) -> None:
    print(f"\n=== {title} ===")
    for key, value in summary.items():
        print(f"  {key:<26} {value}")
    if csv_path:
        print(f"  {'output':<26} {csv_path}")
    print("  note: mechanics, latency and confidence spread only. No accuracy is measured.")
