"""Measures what one call costs per catalog task: N examples for each task, one
task per call, sent one at a time so nothing queues behind anything else.

    python -m simulators.sim_task_latency --base-url http://192.168.0.171:8010 --label laya
    python -m simulators.sim_task_latency --base-url http://127.0.0.1:8010 --label jev

Writes simulators/out/task_latency_{label}.csv (one row per call) and
task_latency_{label}_summary.csv (one row per task). The timeout is set high on
purpose: the point is the time a call really takes, not how often it beats a budget.
Texts come from the hand-written feature-concept fixtures, so nothing real is sent.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

from simulators.common import base_parser, client, latency_summary, print_summary, write_csv

FIXTURES = Path(__file__).parent / "fixtures"
SURVEY_QUESTION = "What is the main reason for your score?"
COLUMN_TASK = "column.field_type"
# Columns beyond the ones in fixtures/import_columns.csv, to reach 20 examples.
EXTRA_COLUMNS: list[tuple[str, list[str]]] = [
    ("Order Ref", ["ORD-88213", "ORD-88214", "ORD-88215"]),
    ("DOB", ["1988-03-14", "1992-11-02", "1979-07-30"]),
    ("Postcode", ["LS1 4AP", "411001", "530-0001"]),
    ("Country", ["India", "United Kingdom", "Japan"]),
    ("Full Name", ["Asha Verma", "Daniel Okafor", "Mei Tanaka"]),
    ("Line Manager", ["R. Iyer", "S. Whitfield", "K. Sato"]),
    ("Website", ["example.com", "shop.example.org", "example.co.jp"]),
    ("Age", ["34", "41", "27"]),
    ("Last Feedback", ["Delivery was late but support sorted it out", "Great value", "Too many emails"]),
]


def _comments(limit: int) -> list[dict[str, Any]]:
    with (FIXTURES / "feature_concepts" / "comments.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))[:limit]
    return [{"example": r["comment_id"], "text": r["text"], "survey_question": SURVEY_QUESTION,
             "metric_score": float(r["nps"])} for r in rows]


def _columns(limit: int) -> list[dict[str, Any]]:
    with (FIXTURES / "import_columns.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    columns = [(name, [r[name] for r in rows[:3]]) for name in rows[0]] + EXTRA_COLUMNS
    return [{"example": name, "text": name,
             "context": {"column_name": name, "sample_values": ", ".join(samples)}}
            for name, samples in columns[:limit]]


def _call(http, task: str, example: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    body = {k: v for k, v in example.items() if k != "example"}
    body.update(tasks=[task], timeout_ms=timeout_ms)
    row: dict[str, Any] = {"task": task, "example": example["example"], "chars": len(example["text"])}
    t0 = time.perf_counter()
    try:
        resp = http.post("/v1/classify", json=body)
        row["client_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        row["http_status"] = resp.status_code
        data = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:
        row.update(client_ms=round((time.perf_counter() - t0) * 1000.0, 1), http_status="",
                   error=type(exc).__name__)
        data = {}
    answer = (data.get("answers") or {}).get(task) or {}
    row.update(gateway_ms=data.get("latency_ms"), backend_ms=data.get("backend_ms"),
               fallback=data.get("fallback"), fallback_reason=data.get("fallback_reason"),
               cached=answer.get("cached"), model=data.get("model"), label=answer.get("label"),
               confidence=answer.get("confidence"), band=answer.get("band"), text=example["text"])
    return row


def main() -> None:
    parser = base_parser(__doc__.splitlines()[0])
    parser.add_argument("--label", required=True, help="name for the output files, such as laya or jev")
    parser.add_argument("--per-task", type=int, default=20)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    args = parser.parse_args()

    with client(args, timeout=args.timeout_ms / 1000.0 + 10) as http:
        health = http.get("/health").json()
        tasks = [t["id"] for t in http.get("/v1/tasks").json()["tasks"]]
        # One warm-up so connection set-up is not charged to the first task.
        _call(http, tasks[0], {"example": "warm-up", "text": "warm-up call"}, args.timeout_ms)
        rows: list[dict[str, Any]] = []
        for task in tasks:
            examples = _columns(args.per_task) if task == COLUMN_TASK else _comments(args.per_task)
            rows += [_call(http, task, example, args.timeout_ms) for example in examples]

    summary = []
    for task in tasks + ["ALL"]:
        mine = [r for r in rows if task in ("ALL", r["task"])]
        answered = [r for r in mine if r["fallback"] is False and not r["cached"]]
        client_ms = latency_summary([r["client_ms"] for r in answered])
        backend = [r["backend_ms"] for r in answered if r["backend_ms"] is not None]
        summary.append({
            "backend": health.get("backend", "laya"), "task": task, "calls": len(mine),
            "answered": len(answered), "fallbacks": sum(1 for r in mine if r["fallback"] is not False),
            "cached": sum(1 for r in mine if r["cached"]),
            "client_p50_ms": client_ms["p50_ms"], "client_p95_ms": client_ms["p95_ms"],
            "client_min_ms": round(min((r["client_ms"] for r in answered), default=0.0), 1),
            "client_max_ms": client_ms["max_ms"],
            "client_mean_ms": round(sum(r["client_ms"] for r in answered) / len(answered), 1) if answered else 0.0,
            "gateway_p50_ms": latency_summary([r["gateway_ms"] for r in answered])["p50_ms"],
            "backend_p50_ms": latency_summary(backend)["p50_ms"] if backend else "",
        })
    calls_csv = write_csv(f"task_latency_{args.label}.csv", rows)
    summary_csv = write_csv(f"task_latency_{args.label}_summary.csv", summary)
    total = summary[-1]
    print_summary(f"Task latency: {args.label} ({args.base_url})", {
        "backend": total["backend"], "calls": total["calls"], "answered": total["answered"],
        "fallbacks": total["fallbacks"], "client_p50_ms": total["client_p50_ms"],
        "client_p95_ms": total["client_p95_ms"], "summary": summary_csv}, calls_csv)


if __name__ == "__main__":
    main()
