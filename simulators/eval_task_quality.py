"""Scores a backend's answers against the hand-labelled set in task_eval_set.py:
10 examples per task, one call each, through the gateway.

    python -m simulators.eval_task_quality --base-url http://127.0.0.1:8010 --label jev

Writes simulators/out/task_quality_{label}.csv (one row per example) and
task_quality_{label}_summary.csv (one row per task).

What the platform does with an answer depends on its band, so each row is also
sorted into an outcome:
  acted_right    band act, answer correct          (applied automatically, and right)
  acted_wrong    band act, answer wrong            (applied automatically, and wrong: the costly case)
  offered_right  band suggest, answer correct      (shown for a person to accept)
  offered_wrong  band suggest, answer wrong
  quiet_right    band none, and nothing was due    (correctly left alone)
  missed         band none, but something was due  (today's behaviour continues)
"""
from __future__ import annotations

import re
import time
from typing import Any

from simulators.common import base_parser, client, latency_summary, print_summary, write_csv
from simulators.task_eval_set import TASKS

OUTCOMES = ["acted_right", "acted_wrong", "offered_right", "offered_wrong", "quiet_right", "missed"]
# Nothing is due for these expected answers: staying quiet is the right result.
NOTHING_DUE = {"no", "other", ""}


def _label_slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", label.strip()).strip("_") or "label"


def _outcome(band: str, correct: bool, due: bool) -> str:
    if band == "act":
        return "acted_right" if correct else "acted_wrong"
    if band == "suggest":
        return "offered_right" if correct else "offered_wrong"
    return "missed" if due else "quiet_right"


def _score(task: dict[str, Any], example: dict[str, Any], answers: dict[str, dict]) -> dict[str, Any]:
    answer_id = task["spec"]["id"] if task["spec"] else task["id"]
    if task["kind"] == "labels":
        expected = sorted(example["expected"])
        per_label = {label: answers.get(f"{answer_id}.{_label_slug(label)}") or {} for label in task["spec"]["classes"]}
        predicted = sorted(label for label, a in per_label.items() if a.get("label") == "yes")
        bands = [a.get("band", "none") for label, a in per_label.items() if a.get("label") == "yes"]
        band = "act" if "act" in bands else "suggest" if "suggest" in bands else "none"
        right = sum(1 for label, a in per_label.items() if (a.get("label") == "yes") == (label in expected))
        return {"expected": "; ".join(expected) or "(none)", "predicted": "; ".join(predicted) or "(none)",
                "correct": predicted == expected, "band": band, "due": bool(expected),
                "confidence": round(min((a.get("confidence") or 0.0) for a in per_label.values()), 3),
                "label_decisions_right": right, "label_decisions": len(per_label)}
    answer = answers.get(answer_id) or {}
    expected = example["expected"]
    predicted = answer.get("label")
    return {"expected": expected, "predicted": predicted, "correct": predicted == expected,
            "band": answer.get("band", "none"), "due": expected not in NOTHING_DUE,
            "confidence": answer.get("confidence"),
            "p_expected": (answer.get("probabilities") or {}).get(expected)}


def _call(http, task: dict[str, Any], example: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    body: dict[str, Any] = {"text": example["text"], "timeout_ms": timeout_ms}
    if task["spec"]:
        body["questions"] = [task["spec"]]
    else:
        body["tasks"] = [task["id"]]
    for key in ("metric_score", "context"):
        if key in example:
            body[key] = example[key]
    if task["survey_question"]:
        body["survey_question"] = task["survey_question"]
    row: dict[str, Any] = {"group": task["group"], "task": task["id"], "tricky": bool(example.get("tricky"))}
    t0 = time.perf_counter()
    resp = http.post("/v1/classify", json=body)
    row["client_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    data = resp.json() if resp.status_code == 200 else {}
    row.update(http_status=resp.status_code, fallback=data.get("fallback", True),
               fallback_reason=data.get("fallback_reason") or (None if resp.status_code == 200 else resp.text[:120]),
               model=data.get("model"))
    scored = _score(task, example, data.get("answers") or {})
    row.update(scored)
    row["outcome"] = "no_answer" if row["fallback"] else _outcome(scored["band"], scored["correct"], scored["due"])
    row["text"] = example["text"]
    return row


def _summarise(task: dict[str, Any], rows: list[dict[str, Any]], backend: str) -> dict[str, Any]:
    n = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    plain = [r for r in rows if not r["tricky"]]
    tricky = [r for r in rows if r["tricky"]]
    acted = [r for r in rows if r["band"] == "act" and not r["fallback"]]
    summary: dict[str, Any] = {
        "backend": backend, "group": task["group"], "task": task["id"], "kind": task["kind"], "use": task["use"],
        "examples": n, "correct": correct, "accuracy_pct": round(100.0 * correct / n),
        "plain_correct": f"{sum(1 for r in plain if r['correct'])}/{len(plain)}",
        "tricky_correct": f"{sum(1 for r in tricky if r['correct'])}/{len(tricky)}",
        "acted": len(acted), "acted_right": sum(1 for r in acted if r["correct"]),
    }
    summary.update({name: sum(1 for r in rows if r["outcome"] == name) for name in OUTCOMES + ["no_answer"]})
    right_conf = [r["confidence"] for r in rows if r["correct"] and r["confidence"] is not None]
    wrong_conf = [r["confidence"] for r in rows if not r["correct"] and r["confidence"] is not None]
    summary["mean_confidence_right"] = round(sum(right_conf) / len(right_conf), 2) if right_conf else ""
    summary["mean_confidence_wrong"] = round(sum(wrong_conf) / len(wrong_conf), 2) if wrong_conf else ""
    summary["client_p50_ms"] = latency_summary([r["client_ms"] for r in rows])["p50_ms"]
    return summary


def main() -> None:
    parser = base_parser(__doc__.splitlines()[0])
    parser.add_argument("--label", required=True, help="name for the output files, such as jev or laya")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    with client(args, timeout=args.timeout_ms / 1000.0 + 10) as http:
        backend = http.get("/health").json().get("backend", "laya")
        for task in TASKS:
            mine = [_call(http, task, example, args.timeout_ms) for example in task["examples"]]
            rows += mine
            summary.append(_summarise(task, mine, backend))
    calls_csv = write_csv(f"task_quality_{args.label}.csv", rows)
    summary_csv = write_csv(f"task_quality_{args.label}_summary.csv", summary)
    total = len(rows)
    print_summary(f"Task quality: {args.label} ({args.base_url})", {
        "backend": backend, "examples": total, "correct": sum(1 for r in rows if r["correct"]),
        "no_answer": sum(1 for r in rows if r["fallback"]),
        "acted_wrong": sum(1 for r in rows if r["outcome"] == "acted_wrong"), "summary": summary_csv}, calls_csv)


if __name__ == "__main__":
    main()
