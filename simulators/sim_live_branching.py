"""Simulates QDL/ADL/branching and the live nudge: author-defined classes on a
question, single calls from the survey page with a hard budget.

Respondents press Next at a set arrival rate (open loop: a slow or failed call
does not hold back the next respondent). Reports how often the page would take
the default path: the gateway fell back (timeout, busy or Laya down) or the
answer banded `none`.

    python -m simulators.sim_live_branching --rate 10 --calls 300
    python -m simulators.sim_live_branching --rate 10 --burst 8     # 8 clicks at the same instant

Each call's text gets a short numeric suffix so the cache does not answer it;
pass --allow-cache to replay the fixture as-is and see the cached path instead.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from simulators.common import (
    async_client, base_parser, client, flatten_answers, latency_summary, print_summary, write_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_branching.json"


def _save_bindings(args, fixture: dict[str, Any]) -> None:
    with client(args) as http:
        for binding in fixture["bindings"]:
            resp = http.put(f"/v1/bindings/{binding['survey_no']}/{binding['question_id']}", json=binding)
            resp.raise_for_status()
            report = resp.json()["budget"]
            worst = max(report["lines"], key=lambda line: line["option_tokens"])
            print(f"Binding {binding['question_id']} saved: {report['wire_questions']} questions, "
                  f"largest option set ~{worst['option_tokens']}/{worst['budget']} tokens ({worst['id']})")


async def _call(http, survey_no: int, question_id: str, text: str,
                branch_on: str, timeout_ms: int) -> dict[str, Any]:
    body = {"text": text, "survey_no": survey_no, "question_id": question_id, "timeout_ms": timeout_ms}
    t0 = time.perf_counter()
    resp = await http.post("/v1/classify", json=body)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    resp.raise_for_status()
    data = resp.json()
    branch = data["answers"][branch_on]
    default_path = data["fallback"] or branch["band"] == "none"
    return {"question_id": question_id, "text": text, "latency_ms": round(latency_ms, 1),
            "gateway_ms": data["latency_ms"], "backend_ms": data["backend_ms"],
            "fallback": data["fallback"], "fallback_reason": data["fallback_reason"],
            "branch_on": branch_on, "branch_label": branch["label"], "branch_band": branch["band"],
            "path": "default" if default_path else f"branch:{branch['label']}",
            "cached": all(a["cached"] for a in data["answers"].values()),
            **flatten_answers(data["answers"])}


async def _run(args, fixture: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = [(qid, text) for qid, texts in fixture["answers"].items() for text in texts]
    interval = args.burst / args.rate
    # Unique per run as well as per call, so an earlier run does not warm the cache.
    run_id = int(time.time()) % 100000
    async with async_client(args) as http:
        tasks = []
        start = time.perf_counter()
        for i in range(args.calls):
            if i % args.burst == 0:
                await asyncio.sleep(max(0.0, start + (i // args.burst) * interval - time.perf_counter()))
            question_id, text = pairs[i % len(pairs)]
            if not args.allow_cache:
                text = f"{text} ({run_id}-{i})"
            tasks.append(asyncio.create_task(_call(
                http, fixture["survey_no"], question_id, text,
                fixture["branch_on"][question_id], args.timeout_ms)))
        return list(await asyncio.gather(*tasks))


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--calls", type=int, default=300)
    parser.add_argument("--rate", type=float, default=10.0, help="respondents pressing Next per second")
    parser.add_argument("--burst", type=int, default=1, help="clicks that land at the same instant")
    parser.add_argument("--timeout-ms", type=int, default=300, help="live budget per call")
    parser.add_argument("--allow-cache", action="store_true")
    args = parser.parse_args()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    _save_bindings(args, fixture)
    t0 = time.perf_counter()
    rows = asyncio.run(_run(args, fixture))
    wall_s = time.perf_counter() - t0

    latency = latency_summary([row["latency_ms"] for row in rows])
    fallbacks = Counter(row["fallback_reason"] for row in rows if row["fallback"])
    default_paths = sum(1 for row in rows if row["path"] == "default")
    path = write_csv("live_branching.csv", rows)
    print_summary("live branching", {
        "calls": len(rows), "arrival_rate_per_s": args.rate, "burst": args.burst,
        "budget_ms": args.timeout_ms, "wall_s": round(wall_s, 2),
        "latency": latency,
        "p95_inside_budget": latency["p95_ms"] <= args.timeout_ms,
        "fallbacks": dict(fallbacks) or 0,
        "fallback_rate": round(sum(fallbacks.values()) / len(rows), 3),
        "default_path_rate": round(default_paths / len(rows), 3),
        "cached_calls": sum(1 for row in rows if row["cached"]),
        "paths": dict(Counter(row["path"] for row in rows).most_common(8)),
    }, path)


if __name__ == "__main__":
    main()
