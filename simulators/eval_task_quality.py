"""Scores a backend's answers against the labelled sets in task_eval_set.py: 1,000
texts, one call each through the gateway, every applicable question in that call.

    python -m simulators.eval_task_quality --base-url http://127.0.0.1:8010 --label jev

The Sogo-lite mock runs the same code from its Evaluation page (ui/sogo_lite/evaluation.py).

Run the gateway with CACHE_MAX_ENTRIES=0 so no answer is served from its cache:
latency and token counts are then real, and --split-check compares fresh answers.

Writes to simulators/out/:
  task_quality_{label}.csv              one row per scored decision
  task_quality_{label}_summary.csv      one row per question and tier
  task_quality_{label}_calls.csv        one row per call: latency, tokens
  task_quality_{label}_sets.csv         one row per set: latency, tokens, cost
  task_quality_{label}_split_check.csv  with --split-check: questions asked alone vs together

What the platform does with an answer depends on its band, so each decision is also
sorted into an outcome:
  acted_right    band act, answer correct          (applied automatically, and right)
  acted_wrong    band act, answer wrong            (applied automatically, and wrong: the costly case)
  offered_right  band suggest, answer correct      (shown for a person to accept)
  offered_wrong  band suggest, answer wrong
  quiet_right    band none, and nothing was due    (correctly left alone)
  missed         band none, but something was due  (today's behaviour continues)
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Callable, Optional

from simulators.common import base_parser, client, latency_summary, write_csv
from simulators.task_eval_set import NOTHING_DUE, SETS, TIERS, Check, Plan, grade_from_points, rows

OUTCOMES = ["acted_right", "acted_wrong", "offered_right", "offered_wrong", "quiet_right", "missed"]
REQUEST_FIELDS = ("tasks", "questions", "survey_question", "metric_score", "context")
PROGRESS_EVERY = 100


def _outcome(band: str, correct: bool, due: bool) -> str:
    if band == "act":
        return "acted_right" if correct else "acted_wrong"
    if band == "suggest":
        return "offered_right" if correct else "offered_wrong"
    return "missed" if due else "quiet_right"


def _post(http, text: str, fields: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """One classify call. Never raises: a failed call comes back as a fallback."""
    body = {"text": text, "timeout_ms": timeout_ms, **{k: fields[k] for k in REQUEST_FIELDS if k in fields}}
    t0 = time.perf_counter()
    try:
        resp = http.post("/v1/classify", json=body)
        status, data = resp.status_code, (resp.json() if resp.status_code == 200 else {})
        error = None if status == 200 else resp.text[:160]
    except Exception as exc:
        status, data, error = 0, {}, f"{type(exc).__name__}: {exc}"[:160]
    answers = data.get("answers") or {}
    usage = data.get("usage") or {}
    return {"client_ms": round((time.perf_counter() - t0) * 1000.0, 1), "http_status": status,
            "fallback": data.get("fallback", True), "fallback_reason": data.get("fallback_reason") or error,
            "gateway_ms": data.get("latency_ms"), "model": data.get("model"), "answers": answers,
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_answers": sum(1 for a in answers.values() if a.get("cached"))}


def _decide(check: Check, answers: dict[str, dict], fallback: bool) -> dict[str, Any]:
    answer = answers.get(check["answer_id"]) or {}
    predicted = answer.get("label")
    correct = predicted in check["accept"]
    due = check["expected"] not in NOTHING_DUE
    band = answer.get("band", "none")
    return {"question": check["question"], "item": check["item"], "expected": check["expected"],
            "predicted": predicted, "correct": correct, "due": due, "band": band,
            "confidence": answer.get("confidence"),
            "p_expected": (answer.get("probabilities") or {}).get(check["expected"]),
            "outcome": "no_answer" if fallback or predicted is None else _outcome(band, correct, due)}


def _derived_grades(row: dict[str, Any], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quiz only: the grade each key-point wording implies, next to the model's own grade.
    Key points cannot tell a wrong answer from a non-answer, so those two count as one."""
    out = []
    wanted = row["expected"]["grade"].replace("not_an_answer", "incorrect")
    for source in ("quiz_key_points", "quiz_key_points_strict"):
        mine = [d for d in decisions if d["question"] == source]
        if not mine or any(d["outcome"] == "no_answer" for d in mine):
            continue
        implied = grade_from_points(sum(1 for d in mine if d["predicted"] == "yes"), len(mine))
        out.append({"question": source.replace("quiz_key_points", "quiz_grade_from_points"), "item": "",
                    "expected": wanted, "predicted": implied, "correct": implied == wanted,
                    "due": wanted != "incorrect", "band": "", "confidence": None, "p_expected": None,
                    "outcome": "derived"})
    return out


def _run_row(http, eval_set: dict[str, Any], row: dict[str, Any], plan: Plan,
             timeout_ms: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = _post(http, row["text"], plan, timeout_ms)
    answers = result.pop("answers")
    decisions = [_decide(check, answers, result["fallback"]) for check in plan["checks"]]
    if eval_set["id"] == "quiz":
        decisions += _derived_grades(row, decisions)
    base = {"set": eval_set["id"], "scope": eval_set["scope"], "id": row["id"], "tier": row["tier"]}
    call = {**base, **result, "questions": len(plan["checks"]),
            "all_correct": all(d["correct"] for d in decisions if d["outcome"] != "derived"), "text": row["text"]}
    return call, [{**base, **d, "note": row.get("note", ""), "text": row["text"]} for d in decisions]


def _split_check(http, row: dict[str, Any], plan: Plan, together: list[dict[str, Any]],
                 timeout_ms: int) -> list[dict[str, Any]]:
    """Ask each part of a combined call on its own and compare with the combined answers."""
    out = []
    for part, fields in plan["parts"].items():
        alone = _post(http, row["text"], fields, timeout_ms)
        part_ids = {q["id"] for q in fields["questions"]}
        for check in plan["checks"]:
            if check["answer_id"].split(".")[0] not in part_ids:
                continue
            solo = _decide(check, alone["answers"], alone["fallback"])
            joint = next(d for d in together if d["question"] == check["question"] and d["item"] == check["item"])
            gap = (abs(solo["p_expected"] - joint["p_expected"])
                   if solo["p_expected"] is not None and joint["p_expected"] is not None else None)
            out.append({"id": row["id"], "part": part, "question": check["question"], "item": check["item"],
                        "expected": check["expected"], "together": joint["predicted"], "alone": solo["predicted"],
                        "same_label": solo["predicted"] == joint["predicted"],
                        "together_p_expected": joint["p_expected"], "alone_p_expected": solo["p_expected"],
                        "p_gap": None if gap is None else round(gap, 4), "alone_client_ms": alone["client_ms"],
                        "alone_input_tokens": alone["input_tokens"],
                        "cached": bool(alone["cached_answers"])})
    return out


# ── summaries ───────────────────────────────────────────────────────────────

def _pct(part: int, whole: int) -> Any:
    return round(100.0 * part / whole, 1) if whole else ""


def _margin(correct: int, n: int) -> Any:
    """Half-width of a 95% Wilson interval, in points: how far the accuracy could be off."""
    if not n:
        return ""
    z, p = 1.96, correct / n
    return round(100.0 * z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n), 1)


def _question_summary(eval_set: dict[str, Any], question: str, tier: str,
                      mine: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(mine)
    correct = sum(1 for d in mine if d["correct"])
    due = [d for d in mine if d["due"]]
    quiet = [d for d in mine if not d["due"]]
    texts: dict[str, bool] = {}
    for d in mine:
        texts[d["id"]] = texts.get(d["id"], True) and d["correct"]
    acted = [d for d in mine if d["band"] == "act" and d["outcome"] not in ("no_answer", "derived")]
    summary: dict[str, Any] = {
        "set": eval_set["id"], "scope": eval_set["scope"], "question": question, "tier": tier,
        "decisions": n, "correct": correct, "accuracy_pct": _pct(correct, n), "margin_pts": _margin(correct, n),
        "texts": len(texts), "texts_all_right": sum(texts.values()),
        "due": len(due), "due_caught": sum(1 for d in due if d["correct"]),
        "caught_pct": _pct(sum(1 for d in due if d["correct"]), len(due)),
        "not_due": len(quiet), "false_alarms": sum(1 for d in quiet if not d["correct"]),
        "acted": len(acted), "acted_right": sum(1 for d in acted if d["correct"]),
    }
    summary.update({name: sum(1 for d in mine if d["outcome"] == name) for name in OUTCOMES + ["no_answer"]})
    for name, pick in (("right", True), ("wrong", False)):
        conf = [d["confidence"] for d in mine if d["correct"] is pick and d["confidence"] is not None]
        summary[f"mean_confidence_{name}"] = round(sum(conf) / len(conf), 2) if conf else ""
    return summary


def _summarise(eval_set: dict[str, Any], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    questions = list(dict.fromkeys(d["question"] for d in decisions))
    for question in questions:
        mine = [d for d in decisions if d["question"] == question]
        out.append(_question_summary(eval_set, question, "all", mine))
        out += [_question_summary(eval_set, question, tier, [d for d in mine if d["tier"] == tier])
                for tier in TIERS if any(d["tier"] == tier for d in mine)]
    return out


def _set_summary(eval_set: dict[str, Any], calls: list[dict[str, Any]], backend: str,
                 usd_per_mtok: Optional[float]) -> dict[str, Any]:
    answered = [c for c in calls if not c["fallback"]]
    tokens = [c["input_tokens"] for c in answered if c["input_tokens"] is not None]
    summary = {"backend": backend, "set": eval_set["id"], "scope": eval_set["scope"], "use": eval_set["use"],
               "calls": len(calls), "no_answer": len(calls) - len(answered),
               "texts_all_right": sum(1 for c in calls if c["all_correct"]),
               "questions_per_call": round(sum(c["questions"] for c in calls) / len(calls), 1),
               **{f"client_{k}": v for k, v in latency_summary([c["client_ms"] for c in answered]).items()
                  if k != "n"},
               "input_tokens_mean": round(sum(tokens) / len(tokens)) if tokens else "",
               "input_tokens_total": sum(tokens) if tokens else "",
               "output_tokens_total": sum(c["output_tokens"] or 0 for c in answered) if tokens else ""}
    if usd_per_mtok is not None and tokens:
        summary["input_usd_total"] = round(sum(tokens) * usd_per_mtok / 1e6, 4)
        summary["input_usd_per_1000_calls"] = round(sum(tokens) / len(tokens) * 1000 * usd_per_mtok / 1e6, 4)
    return summary


# ── running ─────────────────────────────────────────────────────────────────

# Output files, by the key of the result they hold.
OUTPUTS = {"decisions": "", "summary": "_summary", "calls": "_calls", "sets": "_sets", "split": "_split_check"}


def sample(data: list[dict[str, Any]], count: Optional[int]) -> list[dict[str, Any]]:
    """`count` texts spread over a set, or all of it when `count` is None. Each tier
    keeps its share and the picks are evenly spaced within a tier, so a small run is
    not all easy texts. The same count always picks the same texts."""
    if count is None or count >= len(data):
        return list(data)
    if count <= 0:
        return []
    by_tier = {tier: [i for i, row in enumerate(data) if row["tier"] == tier] for tier in TIERS}
    share = {tier: count * len(indices) / len(data) for tier, indices in by_tier.items()}
    take = {tier: int(value) for tier, value in share.items()}
    for tier in sorted(share, key=lambda t: share[t] - take[t], reverse=True)[:count - sum(take.values())]:
        take[tier] += 1
    picked = sorted(indices[k * len(indices) // take[tier]]
                    for tier, indices in by_tier.items() for k in range(take[tier]))
    return [data[i] for i in picked]


def new_result() -> dict[str, Any]:
    return {"backend": "unknown", "stopped": False, "decisions": [], "summary": [], "calls": [], "sets": [],
            "split": []}


def run(http, chosen: list[tuple[dict[str, Any], list[dict[str, Any]]]], result: dict[str, Any], *,
        timeout_ms: int = 20000, split_check: int = 0, usd_per_mtok: Optional[float] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
        stop: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Run the chosen (set, texts) pairs and fill `result` as it goes, so a caller
    that is interrupted still holds the rows collected so far. `progress` is called
    after every call with (done, total, set id); `stop` is asked before every call."""
    total = sum(len(data) for _, data in chosen)
    done = 0
    result["backend"] = http.get("/health").json().get("backend", "laya")
    for eval_set, data in chosen:
        every = max(1, len(data) // split_check) if split_check and eval_set["id"] == "quiz" else 0
        set_calls: list[dict[str, Any]] = []
        set_decisions: list[dict[str, Any]] = []
        for index, row in enumerate(data):
            if stop is not None and stop():
                result["stopped"] = True
                break
            plan = eval_set["plan"](row)
            call, mine = _run_row(http, eval_set, row, plan, timeout_ms)
            set_calls.append(call)
            set_decisions += mine
            result["calls"].append(call)
            result["decisions"] += mine
            if every and index % every == 0 and len({s["id"] for s in result["split"]}) < split_check:
                result["split"] += _split_check(http, row, plan, mine, timeout_ms)
            done += 1
            if progress is not None:
                progress(done, total, eval_set["id"])
        if set_calls:
            result["summary"] += _summarise(eval_set, set_decisions)
            result["sets"].append(_set_summary(eval_set, set_calls, result["backend"], usd_per_mtok))
        if result["stopped"]:
            break
    return result


def headline(result: dict[str, Any]) -> dict[str, Any]:
    """The few figures that describe a whole run."""
    calls = result["calls"]
    answered = [c for c in calls if not c["fallback"]]
    scored = [d for d in result["decisions"] if d["outcome"] != "derived"]
    correct = sum(1 for d in scored if d["correct"])
    tokens = [c["input_tokens"] for c in answered if c["input_tokens"] is not None]
    latency = latency_summary([c["client_ms"] for c in answered])
    split = result["split"]
    return {"backend": result["backend"], "stopped": result["stopped"], "calls": len(calls),
            "no_answer": len(calls) - len(answered),
            "cached_answers": sum(c["cached_answers"] for c in calls),
            "texts_all_right": sum(1 for c in calls if c["all_correct"]),
            "decisions": len(scored), "correct": correct, "accuracy_pct": _pct(correct, len(scored)),
            "acted": sum(1 for d in scored if d["outcome"] in ("acted_right", "acted_wrong")),
            "acted_wrong": sum(1 for d in scored if d["outcome"] == "acted_wrong"),
            "p50_ms": latency["p50_ms"], "p95_ms": latency["p95_ms"], "max_ms": latency["max_ms"],
            "input_tokens_total": sum(tokens), "input_tokens_mean": round(sum(tokens) / len(tokens)) if tokens else 0,
            "split_decisions": len(split), "split_same": sum(1 for s in split if s["same_label"]),
            "split_cached": any(s["cached"] for s in split)}


def write_outputs(result: dict[str, Any], label: str, out_dir: Optional[Path] = None) -> list[Path]:
    return [write_csv(f"task_quality_{label}{suffix}.csv", result[key], out_dir)
            for key, suffix in OUTPUTS.items() if result[key]]


def main() -> None:
    parser = base_parser(__doc__.splitlines()[0])
    parser.add_argument("--label", required=True, help="name for the output files, such as jev or laya")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--sets", default="", help="comma-separated set ids; default is every enabled set")
    parser.add_argument("--include-disabled", action="store_true", help="also run the parked sets")
    parser.add_argument("--limit", type=int, default=0,
                        help="N texts of each set, spread over the tiers; default is every text")
    parser.add_argument("--split-check", type=int, default=0,
                        help="for N quiz texts, also ask each part alone and compare with the combined call")
    parser.add_argument("--usd-per-mtok", type=float, default=None,
                        help="price per million input tokens, to turn token counts into cost")
    args = parser.parse_args()

    wanted = {s.strip() for s in args.sets.split(",") if s.strip()}
    chosen = [(s, sample(rows(s["id"]), args.limit or None)) for s in SETS
              if (s["id"] in wanted if wanted else s["enabled"] or args.include_disabled)]

    def _progress(done: int, total: int, set_id: str) -> None:
        if done % PROGRESS_EVERY == 0:
            print(f"  {done} of {total} calls done ({set_id})", flush=True)

    result = new_result()
    try:
        with client(args, timeout=args.timeout_ms / 1000.0 + 10) as http:
            run(http, chosen, result, timeout_ms=args.timeout_ms, split_check=args.split_check,
                usd_per_mtok=args.usd_per_mtok, progress=_progress)
    finally:
        # Whatever was collected is written, so a run that dies part-way still leaves its rows.
        paths = write_outputs(result, args.label)

    top = headline(result)
    print(f"\n=== Task quality: {args.label} ({args.base_url}, backend {top['backend']}) ===")
    print(f"  calls {top['calls']}, no answer {top['no_answer']}, "
          f"answers served from the gateway cache {top['cached_answers']}")
    print(f"  decisions {top['decisions']}, correct {top['correct']}, acted wrong {top['acted_wrong']}")
    print(f"  client latency p50 {top['p50_ms']} ms, p95 {top['p95_ms']} ms, max {top['max_ms']} ms")
    for line in result["summary"]:
        if line["tier"] == "all":
            print(f"  {line['set']:<12} {line['question']:<34} {line['correct']:>4}/{line['decisions']:<4} "
                  f"{line['accuracy_pct']:>5}%  +/-{line['margin_pts']}")
    if top["split_decisions"]:
        print(f"  split check: {top['split_same']}/{top['split_decisions']} decisions the same alone and together"
              + ("; WARNING: some answers came from the cache, run the gateway with CACHE_MAX_ENTRIES=0"
                 if top["split_cached"] else ""))
    for path in paths:
        print(f"  output  {path}")


if __name__ == "__main__":
    main()
