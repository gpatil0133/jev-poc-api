"""Simulates Rules & Alerts at submit: responses arrive one by one, each open-text
answer is checked for alert meanings, and an alert "fires" when a meaning bands `act`.

    python -m simulators.sim_submit_alerts --survey 61 --rate 10
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from simulators.common import (
    async_client, base_parser, flatten_answers, latency_summary, load_survey,
    open_text_questions, print_summary, write_csv,
)

MEANINGS = ["meaning.may_leave", "meaning.safety_concern", "meaning.complaint", "meaning.callback_request"]


async def _submit(http, response_num: str, question: dict, text: str, timeout_ms: int,
                  act: float) -> dict[str, Any]:
    body = {"text": text, "survey_question": question["title"], "tasks": MEANINGS,
            "timeout_ms": timeout_ms, "thresholds": {"act": act}}
    t0 = time.perf_counter()
    resp = await http.post("/v1/classify", json=body)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    resp.raise_for_status()
    data = resp.json()
    fired = [qid for qid, answer in data["answers"].items() if answer["band"] == "act"]
    return {"response": response_num, "question_id": question["question_id"], "text": text,
            "latency_ms": round(latency_ms, 1), "gateway_ms": data["latency_ms"],
            "fallback": data["fallback"], "alerts_fired": ";".join(fired),
            **flatten_answers(data["answers"])}


async def _run(args) -> list[dict[str, Any]]:
    survey, responses = load_survey(args.survey)
    questions = open_text_questions(survey, responses)
    if not questions:
        raise SystemExit(f"Survey {args.survey} has no open-text answers; try --survey 61")
    responses = responses[:args.limit] if args.limit else responses
    print(f"Replaying {len(responses)} responses at {args.rate}/s")
    interval = 1.0 / args.rate
    tasks = []
    async with async_client(args) as http:
        start = time.perf_counter()
        for i, response in enumerate(responses):
            # Arrivals keep their schedule whether or not earlier checks have finished.
            await asyncio.sleep(max(0.0, start + i * interval - time.perf_counter()))
            for question in questions:
                text = response.get(question["key"])
                if isinstance(text, str) and text.strip():
                    tasks.append(asyncio.create_task(_submit(
                        http, str(response["ResponseNum"]), question, text, args.timeout_ms, args.act)))
        return list(await asyncio.gather(*tasks))


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--survey", type=int, default=61)
    parser.add_argument("--rate", type=float, default=10.0, help="responses per second")
    parser.add_argument("--limit", type=int, default=0, help="replay only the first N responses")
    parser.add_argument("--timeout-ms", type=int, default=1000, help="budget for the check at submit")
    parser.add_argument("--act", type=float, default=0.90, help="alert threshold on P(yes)")
    args = parser.parse_args()

    t0 = time.perf_counter()
    rows = asyncio.run(_run(args))
    wall_s = time.perf_counter() - t0
    fired = Counter(m for row in rows for m in row["alerts_fired"].split(";") if m)
    path = write_csv(f"submit_alerts_{args.survey}.csv", rows)
    print_summary("submit alerts", {
        "checks": len(rows), "meanings_per_check": len(MEANINGS), "arrival_rate_per_s": args.rate,
        "wall_s": round(wall_s, 2),
        "latency": latency_summary([row["latency_ms"] for row in rows]),
        "fallbacks": sum(1 for row in rows if row["fallback"]),
        "alert_threshold": args.act,
        "alerts_fired": dict(fired) or "none",
        "checks_with_an_alert": sum(1 for row in rows if row["alerts_fired"]),
    }, path)


if __name__ == "__main__":
    main()
