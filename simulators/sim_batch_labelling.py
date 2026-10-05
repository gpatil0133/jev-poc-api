"""Simulates NLP filters / coded comments: label every open-text answer of a survey.

Sends the answers through /v1/classify/batch (or /v1/jobs with --job), then sends
them again to show the label-once cache.

    python -m simulators.sim_batch_labelling --survey 61
    python -m simulators.sim_batch_labelling --survey 61 --job

Survey 11 (the plan's example) has no open-text question; 61 and 38 do.
"""
from __future__ import annotations

import time
from typing import Any

from simulators.common import (
    band_counts, base_parser, client, flatten_answers, latency_summary, load_survey,
    open_text_questions, print_summary, write_csv,
)

TASKS = ["comment.sentiment", "comment.response_type", "comment.quality",
         "comment.actionable", "meaning.complaint"]


def _items(survey_no: int) -> list[dict[str, Any]]:
    survey, responses = load_survey(survey_no)
    questions = open_text_questions(survey, responses)
    if not questions:
        raise SystemExit(f"Survey {survey_no} has no open-text answers; try --survey 61")
    items = []
    for question in questions:
        for response in responses:
            text = response.get(question["key"])
            if isinstance(text, str) and text.strip():
                items.append({"id": f"{response['ResponseNum']}:{question['question_id']}",
                              "text": text, "survey_question": question["title"]})
    print(f"Survey {survey_no}: {len(questions)} open-text question(s), {len(items)} answers")
    return items


def _run_batches(http, items: list[dict], chunk: int, rotations: int) -> tuple[list[dict], list[float], int]:
    results, latencies, cache_hits = [], [], 0
    for start in range(0, len(items), chunk):
        body = {"tasks": TASKS, "items": items[start:start + chunk], "rotations": rotations}
        t0 = time.perf_counter()
        resp = http.post("/v1/classify/batch", json=body)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        cache_hits += data["cache_hits"]
    return results, latencies, cache_hits


def _run_job(http, items: list[dict], rotations: int) -> tuple[dict, float]:
    t0 = time.perf_counter()
    resp = http.post("/v1/jobs", json={"tasks": TASKS, "items": items, "rotations": rotations})
    resp.raise_for_status()
    status = resp.json()
    while status["status"] in ("queued", "running"):
        time.sleep(0.25)
        status = http.get(f"/v1/jobs/{status['job_id']}").json()
        print(f"  job {status['job_id']}: {status['processed']}/{status['total']}")
    return status, (time.perf_counter() - t0) * 1000.0


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--survey", type=int, default=61)
    parser.add_argument("--chunk", type=int, default=256, help="items per batch request")
    parser.add_argument("--rotations", type=int, default=1, help="option-order rotations to average")
    parser.add_argument("--job", action="store_true", help="use the async /v1/jobs path")
    args = parser.parse_args()
    items = _items(args.survey)

    with client(args) as http:
        if args.job:
            status, wall_ms = _run_job(http, items, args.rotations)
            print_summary("batch labelling (job)", {
                "status": status["status"], "items": status["total"], "failed_items": status["failed_items"],
                "wall_ms": round(wall_ms, 1), "items_per_s": status["items_per_s"],
                "results_jsonl": status["output_path"]})
            return

        t0 = time.perf_counter()
        results, latencies, _ = _run_batches(http, items, args.chunk, args.rotations)
        wall_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        _, warm_latencies, warm_hits = _run_batches(http, items, args.chunk, args.rotations)
        warm_s = time.perf_counter() - t1

    rows = [{"id": item["id"], "text": item["text"], **flatten_answers(result["answers"])}
            for item, result in zip(items, results)]
    path = write_csv(f"batch_labelling_{args.survey}.csv", rows)
    print_summary("batch labelling", {
        "items": len(items), "tasks_per_item": len(TASKS), "rotations": args.rotations,
        "cold_wall_s": round(wall_s, 2), "cold_items_per_s": round(len(items) / wall_s, 1),
        "cold_request_latency": latency_summary(latencies),
        "warm_wall_s": round(warm_s, 2), "warm_request_latency": latency_summary(warm_latencies),
        "warm_cache_hit_rate": round(warm_hits / len(items), 3),
        "bands": band_counts(rows),
    }, path)


if __name__ == "__main__":
    main()
