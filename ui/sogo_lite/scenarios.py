"""Scenario runner (plan section 10.3).

Replays the scope page's examples through the real event path and reports pass or
fail per acceptance check, once per column: normal, gateway down, slow gateway and
forced low confidence.

A failed check under "normal" means the scope does not work as described. A failed
check under a fault column means the fallback is wrong, which is a platform
problem, not a model problem.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sogo_lite import create_ai, db, engine, events, gateway, modules, seed, suggestions
from sogo_lite.config import get_settings
from sogo_lite.modules import quiz_scoring, shared_classes, tag_suggest

logger = logging.getLogger(__name__)

SLOW_MS = 2500      # above the respondent, submit and author timeouts; below the batch one
COLUMNS: list[tuple[str, str, dict[str, Any]]] = [
    ("normal", "Normal", {}),
    ("down", "Gateway down", {"down": "all"}),
    ("slow", f"Slow gateway ({SLOW_MS} ms)", {"slow": "all", "slow_ms": SLOW_MS}),
    ("lowconf", "Low confidence", {"lowconf": "all"}),
]
JOB_ITEMS = 120
JOB_WAIT_S = 90

LOVE_IT = "Love it, but if prices go up again I'm switching."
SOFA = "The sofa arrived two weeks late and the driver left it outside in the rain."
NURSE = "The nurse was rude and nobody called me back."
PRAISE = "Everyone was kind and the visit was quick."
SLIPPERY = "The loading bay floor is slippery and someone is going to get hurt."
PRICE_ANSWERS = ["too expensive", "costs went up", "not worth the money"]
MEANING_RULES = {"Promoter may leave", "Safety concern", "Wants a callback"}
INCOME = "What is your annual household income?"
BRANCH = "Which branch did you visit?"
REASON = "What is the main reason for your score?"
ORDER_NO = "Please enter your order number"
PROMPTS = [("See how staff feel about the new hybrid-work policy", "EX Project"),
           ("Feedback after a support call, with NPS", "CX Project"),
           ("Food-safety quiz with a pass mark", "Assessment"),
           ("Quick vote on the offsite date", "Poll")]
QUIZ_ANSWERS = {"correct": "Bottom shelf, under cooked food, so juices can't drip on it.",
                "partly": "Put it at the bottom so it doesn't drip on things.",
                "incorrect": "Keep it covered."}

Outcome = tuple[str, str]       # ("pass" | "fail" | "na", detail)
Ctx = dict[str, Any]

STATE: dict[str, Any] = {"running": False, "done": 0, "total": 0, "current": "", "run_id": None}
_lock = threading.Lock()


@dataclass
class Scenario:
    id: str
    scope: str
    title: str
    fn: Callable[[str, Ctx], Outcome]


def _check(passed: bool, detail: str) -> Outcome:
    return ("pass" if passed else "fail"), detail


def _works(col: str, kind: str) -> bool:
    """Should the gateway path succeed in this column? `kind` is what the scope calls."""
    if kind == "classify":
        return col == "normal"
    if kind == "binding":       # no answers to rewrite, so low confidence does not touch it
        return col in ("normal", "lowconf")
    if kind == "batch":         # the 30 s batch timeout outlasts the slow-gateway delay
        return col in ("normal", "slow")
    return True


# ── lookups ─────────────────────────────────────────────────────────────────

def _calls(response_id: int, endpoint: Optional[str] = None) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM gateway_call_log WHERE response_id=? ORDER BY id", response_id)
    return [c for c in found if endpoint is None or c["endpoint"] == endpoint]


def _last_log_id() -> int:
    return db.val("SELECT COALESCE(MAX(id), 0) FROM gateway_call_log")


def _fired(response_id: int, sink: str = "email") -> set[str]:
    return {r["name"] for r in db.rows(
        "SELECT a.name FROM sink_log s JOIN alert_rule a ON a.id=s.rule_id WHERE s.response_id=? AND s.sink=?",
        response_id, sink)}


def _submitted(response_id: int) -> bool:
    return bool(engine.response(response_id)["submitted_at"])


def _retail(score: float, comment: str, **kwargs: Any) -> int:
    return engine.take(seed.RETAIL, {"q1": score, "q2": comment, "q3": "Too expensive", "q4": "Late",
                                     "q5": "Downtown"}, **kwargs)


def _clinic(score: float, comment: str) -> int:
    return engine.take(seed.CLINIC, {"q1": score, "q2": comment, "q3": "An apology would be a start.",
                                     "q4": "Not sure"})


def _scratch(ctx: Ctx) -> int:
    """A throwaway project for the builder scopes, one per column."""
    if "scratch" not in ctx:
        ctx["scratch"] = engine.create_project("Scenario scratch", "CX Project")
    return ctx["scratch"]


def _save_question(ctx: Ctx, qtype: str, wording: str, **kwargs: Any) -> dict[str, Any]:
    """Add a question and raise `question.saved`, as the Design page does."""
    survey_no = _scratch(ctx)
    question_id = engine.add_question(survey_no, engine.pages(survey_no)[0]["id"], qtype, wording, **kwargs)
    events.emit("question.saved", question=engine.question(question_id))
    return engine.question(question_id)


def _pending(question_id: int, kind: str) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM suggestion WHERE target_type='question' AND target_id=? AND kind=?"
                   " AND status='pending'", question_id, kind)


# ── scope 1: shared classes ─────────────────────────────────────────────────

def s1_roundtrip(col: str, ctx: Ctx) -> Outcome:
    saved, message = shared_classes.sync(seed.CLINIC, "q2", trigger="scenario: save class set")
    try:
        if _works(col, "binding"):
            same, verdict = shared_classes.verify(seed.CLINIC, "q2")
            return _check(saved and same, f"{message} {verdict}")
        cs = shared_classes.get(seed.CLINIC, "q2")
        return _check(not saved and not shared_classes.usable(cs),
                      "save failed and the class set is an unsynced draft that rules cannot use"
                      if not saved else "save unexpectedly succeeded")
    finally:
        with gateway.override_faults({}):
            shared_classes.sync(seed.CLINIC, "q2", trigger="scenario: restore class set")


def s1_one_call(col: str, ctx: Ctx) -> Outcome:
    response_id = _clinic(1, NURSE)
    calls = _calls(response_id, "/v1/classify")
    if col in ("normal", "lowconf"):
        return _check(len(calls) == 1 and not calls[0]["fallback"],
                      f"{len(calls)} classify call(s) for a question read by a Logic rule and two alert rules")
    return _check(bool(calls) and all(c["fallback"] for c in calls) and _submitted(response_id),
                  f"{len(calls)} attempt(s), all fallbacks; nothing stored; response submitted")


def s1_hash_changes(col: str, ctx: Ctx) -> Outcome:
    if col != "normal":
        return "na", "needs a working gateway"
    before = shared_classes.get(seed.CLINIC, "q2")
    old_response = _clinic(1, NURSE)
    probe = {"id": "scenario_probe", "type": "yesno", "instructions": "Does this survey answer mention parking?",
             "classes": {"true": "mentions parking", "false": "does not mention parking"}}
    try:
        shared_classes.save(seed.CLINIC, "q2", before["tasks"], before["custom"] + [probe],
                            before["act"], before["suggest"], before["model"])
        after = shared_classes.get(seed.CLINIC, "q2")
        stale = shared_classes.stored(old_response, seed.CLINIC, "q2") is None
        new_response = _clinic(1, NURSE)
        calls = _calls(new_response, "/v1/classify")
        changed = after["question_hash"] != before["question_hash"]
        reclassified = len(calls) == 1 and calls[0]["question_hash"] == after["question_hash"]
        return _check(changed and stale and reclassified,
                      f"hash {before['question_hash']} -> {after['question_hash']}; old result reusable: "
                      f"{not stale}; new response classified against the new hash: {reclassified}")
    finally:
        shared_classes.save(seed.CLINIC, "q2", before["tasks"], before["custom"],
                            before["act"], before["suggest"], before["model"])


def s1_clinic(col: str, ctx: Ctx) -> Outcome:
    response_id = _clinic(1, NURSE)
    shown = "q3" in engine.shown_qids(response_id)
    emailed = "Complaint and Negative" in _fired(response_id)
    if _works(col, "classify"):
        return _check(shown and emailed, f"service-recovery question shown: {shown}; "
                                         f"'Complaint and Negative' email written: {emailed}")
    return _check(not shown and not emailed and _submitted(response_id),
                  f"default path taken: {not shown}; no meaning alert: {not emailed}; response submitted")


# ── scope 2: Rules & Alerts on comment meaning ──────────────────────────────

def s2_may_leave(col: str, ctx: Ctx) -> Outcome:
    fired = _fired(_retail(9, LOVE_IT)) & MEANING_RULES
    if _works(col, "classify"):
        return _check(fired == {"Promoter may leave"}, f"meaning rules fired: {sorted(fired) or 'none'}")
    return _check(not fired, f"meaning rules fired: {sorted(fired) or 'none'}")


def s2_module_off(col: str, ctx: Ctx) -> Outcome:
    with modules.override({**{m: True for m in modules.MODULE_IDS}, "alert_meaning": False}):
        fired = _fired(_retail(9, LOVE_IT)) & MEANING_RULES
    return _check(not fired, f"with the module off, meaning rules fired: {sorted(fired) or 'none'}")


def s2_webhook(col: str, ctx: Ctx) -> Outcome:
    rule = "Complaint: open a case"
    on_complaint = rule in _fired(_clinic(1, NURSE), "webhook")
    on_praise = rule in _fired(_clinic(5, PRAISE), "webhook")
    if _works(col, "classify"):
        return _check(on_complaint and not on_praise,
                      f"webhook written for the complaint: {on_complaint}; for the praise: {on_praise}")
    return _check(not on_complaint and not on_praise,
                  f"webhook written for the complaint: {on_complaint}; for the praise: {on_praise}")


def s2_score_rules(col: str, ctx: Ctx) -> Outcome:
    started = time.perf_counter()
    response_id = _retail(3, SOFA)
    took_ms = round((time.perf_counter() - started) * 1000)
    fired = _fired(response_id)
    score_fired = "Detractor score" in fired
    if col == "normal":
        return _check(score_fired and _submitted(response_id), f"score rule fired: {score_fired}; {took_ms} ms")
    skipped = [n for n in db.loads(engine.response(response_id)["notes"], []) if n["kind"] == "check skipped"]
    recorded = bool(skipped) or col == "lowconf"
    return _check(score_fired and not (fired & MEANING_RULES) and _submitted(response_id) and recorded,
                  f"score rule fired: {score_fired}; meaning rules fired: {sorted(fired & MEANING_RULES) or 'none'}; "
                  f"skipped checks recorded on the response: {len(skipped)}; {took_ms} ms")


def s2_anonymous(col: str, ctx: Ctx) -> Outcome:
    response_id = engine.take(seed.STAFF, {"q1": 2, "q2": 2, "q3": SLIPPERY, "q4": "30,000 to 60,000"},
                              respondent="Priya Example <priya@example.test>")
    emails = db.rows("SELECT s.payload, a.name FROM sink_log s JOIN alert_rule a ON a.id=s.rule_id"
                     " WHERE s.response_id=? AND s.sink='email'", response_id)
    leaks = [e["name"] for e in emails
             if "Priya" in e["payload"] or "response #" in e["payload"] or "response_id" in e["payload"]]
    names = {e["name"] for e in emails}
    expected = {"Low support score"} | ({"Safety concern raised"} if _works(col, "classify") else set())
    return _check(names == expected and not leaks,
                  f"emails: {sorted(names) or 'none'}; emails naming the respondent: {leaks or 'none'}")


# ── scope 3: Logic on text answers ──────────────────────────────────────────

def s3_price(col: str, ctx: Ctx) -> Outcome:
    shown = [text for text in PRICE_ANSWERS if "q3" in engine.shown_qids(_retail(6, text))]
    with modules.override({**{m: True for m in modules.MODULE_IDS}, "logic_text": False}):
        keyword = [text for text in PRICE_ANSWERS if "q3" in engine.shown_qids(_retail(6, text))]
    detail = f"pricing question shown for {len(shown)} of 3; with today's Contains \"price\" rule only: {len(keyword)}"
    if _works(col, "classify"):
        return _check(len(shown) == 3 and not keyword, detail)
    return _check(not shown and not keyword, detail)


def s3_delivery(col: str, ctx: Ctx) -> Outcome:
    started = time.perf_counter()
    response_id = _retail(3, SOFA)
    took_ms = round((time.perf_counter() - started) * 1000)
    shown = "q4" in engine.shown_qids(response_id)
    if _works(col, "classify"):
        return _check(shown, f"delivery follow-up shown on the next page: {shown}")
    calls = [c for c in _calls(response_id) if c["trigger"] == "page.next"]
    logged = col == "lowconf" or (bool(calls) and all(c["fallback"] for c in calls))
    return _check(not shown and _submitted(response_id) and logged,
                  f"default path taken: {not shown}; survey completed in {took_ms} ms; "
                  f"page.next calls: {len(calls)}, fallbacks: {sum(1 for c in calls if c['fallback'])}")


def s3_offline(col: str, ctx: Ctx) -> Outcome:
    response_id = _retail(3, SOFA, offline=True)
    shown = set(engine.shown_qids(response_id)) & {"q3", "q4"}
    calls = _calls(response_id)
    return _check(not calls and not shown and _submitted(response_id),
                  f"gateway calls: {len(calls)}; extra questions shown: {sorted(shown) or 'none'}")


# ── scopes 4 and 5: builder hints ───────────────────────────────────────────

def s4_income(col: str, ctx: Ctx) -> Outcome:
    ctx["income_first_log"] = _last_log_id()
    ctx["income"] = _save_question(ctx, "radio", INCOME, required="mandatory",
                                   options=[("Under 30,000", 0), ("Over 30,000", 0)])
    hints = _pending(ctx["income"]["id"], "sensitive")
    if _works(col, "classify"):
        return _check(len(hints) == 1, f"hints on the Mandatory income question: {len(hints)}")
    return _check(not hints, f"hints: {len(hints)}; the question was saved")


def s4_branch(col: str, ctx: Ctx) -> Outcome:
    question = _save_question(ctx, "radio", BRANCH, required="mandatory",
                              options=[("Downtown", 0), ("Riverside", 0)])
    hints = _pending(question["id"], "sensitive")
    return _check(not hints, f"hints on \"{BRANCH}\": {len(hints)}")


def s4_wording_only(col: str, ctx: Ctx) -> Outcome:
    call = db.one("SELECT * FROM gateway_call_log WHERE id>? AND trigger='question.saved' ORDER BY id",
                  ctx.get("income_first_log", 0))
    if not call:
        return "fail", "no call was logged for the saved question"
    request = db.loads(call["request"], {})
    extra = set(request) - {"text", "questions", "context", "timeout_ms"}
    return _check(request.get("text") == INCOME and not extra and call["response_id"] is None,
                  f"request fields: {sorted(request)}; text is the question wording: {request.get('text') == INCOME}")


def s4_dismiss(col: str, ctx: Ctx) -> Outcome:
    if col != "normal":
        return "na", "needs a hint to dismiss"
    hints = _pending(ctx["income"]["id"], "sensitive")
    if not hints:
        return "na", "no hint was created, so there is nothing to dismiss"
    suggestions.dismiss(hints[0]["id"])
    before = _last_log_id()
    events.emit("question.saved", question=engine.question(ctx["income"]["id"]))
    again = _pending(ctx["income"]["id"], "sensitive")
    return _check(not again and _last_log_id() == before,
                  f"hints after saving again unchanged: {len(again)}; new gateway calls: {_last_log_id() - before}")


def s5_reason(col: str, ctx: Ctx) -> Outcome:
    _save_question(ctx, "metric", "How likely are you to recommend us?", metric_kind="nps")
    ctx["reason_first_log"] = _last_log_id()
    question = _save_question(ctx, "text", REASON)
    ctx["reason_calls"] = _last_log_id() - ctx["reason_first_log"]
    found = _pending(question["id"], "followup")
    if _works(col, "classify"):
        return _check(len(found) == 1, f"follow-up suggestions on \"{REASON}\": {len(found)}")
    return _check(not found, f"follow-up suggestions: {len(found)}")


def s5_order(col: str, ctx: Ctx) -> Outcome:
    question = _save_question(ctx, "text", ORDER_NO)
    found = _pending(question["id"], "followup")
    return _check(not found, f"follow-up suggestions on \"{ORDER_NO}\": {len(found)}")


def s5_one_call(col: str, ctx: Ctx) -> Outcome:
    calls = ctx.get("reason_calls")
    return _check(calls == 1, f"gateway calls for one saved Text Box with modules 4 and 5 on: {calls}")


# ── scope 6: tag suggestions ────────────────────────────────────────────────

def _suggest_tags(ctx: Ctx, survey_no: int) -> dict[str, Any]:
    key = f"tags_{survey_no}"
    if key not in ctx:
        for table, owner in (("option_tag", "option_id IN (SELECT o.id FROM answer_option o JOIN question q"
                                            " ON q.id=o.question_id WHERE q.survey_no=?)"),
                             ("question_tag", "question_id IN (SELECT id FROM question WHERE survey_no=?)")):
            db.run(f"DELETE FROM {table} WHERE source='suggestion' AND {owner}", survey_no)
        first = _last_log_id()
        ctx[key] = tag_suggest.suggest(survey_no)
        ctx[key]["first_log"] = first
    return ctx[key]


def _tag_suggested(survey_no: int, target_type: str, text: str, tag: str) -> bool:
    return any(s["target_type"] == target_type and s["target_text"] == text and s["proposed"]["tag"] == tag
               for s in tag_suggest.review_list(survey_no))


def s6_downtown(col: str, ctx: Ctx) -> Outcome:
    summary = _suggest_tags(ctx, seed.HOTEL)
    option = db.one("SELECT o.id FROM answer_option o JOIN question q ON q.id=o.question_id"
                    " WHERE q.survey_no=? AND o.text=?", seed.HOTEL, "Grand – Downtown")
    automapped = bool(engine.option_tags(seed.HOTEL).get(option["id"]))
    found = _tag_suggested(seed.HOTEL, "option", "Grand – Downtown", "Downtown")
    if _works(col, "batch"):
        return _check(found and not automapped, f"Downtown suggested: {found}; exact auto-map matched: {automapped}")
    return _check(not found, f"no suggestion: {not found}; 'no suggestions available' shown: "
                             f"{summary['unavailable']}")


def s6_questions(col: str, ctx: Ctx) -> Outcome:
    _suggest_tags(ctx, seed.HOTEL)
    clean = _tag_suggested(seed.HOTEL, "question", "How clean was your room?", "Housekeeping")
    food = _tag_suggested(seed.HOTEL, "question", "Rate the breakfast buffet", "Restaurant")
    if _works(col, "batch"):
        return _check(clean and food, f"Housekeeping suggested: {clean}; Restaurant suggested: {food}")
    return _check(not clean and not food, f"Housekeeping suggested: {clean}; Restaurant suggested: {food}")


def s6_not_applied(col: str, ctx: Ctx) -> Outcome:
    _suggest_tags(ctx, seed.HOTEL)
    applied = lambda: sum(1 for tags in list(engine.option_tags(seed.HOTEL).values())      # noqa: E731
                          + list(engine.question_tags(seed.HOTEL).values())
                          for t in tags if t["source"] == "suggestion")
    before = applied()
    pending = tag_suggest.review_list(seed.HOTEL)
    if not pending:
        return _check(before == 0, f"tags applied without Accept: {before}; nothing to accept")
    suggestions.accept(pending[0]["id"])
    return _check(before == 0 and applied() == 1,
                  f"tags applied before Accept: {before}; after accepting one: {applied()}")


def s6_large(col: str, ctx: Ctx) -> Outcome:
    summary = _suggest_tags(ctx, seed.LARGE)
    rejected = db.val("SELECT COUNT(*) FROM gateway_call_log WHERE id>? AND endpoint='/v1/classify/batch'"
                      " AND http_status=422", summary["first_log"])
    detail = (f"batch calls: {summary['batch_calls']}; refused by gateway validation: {rejected}; items sent with "
              f"the full list: {summary['full']}, with a shortlist: {summary['shortlist']}, "
              f"with no candidate tag: {summary['no_candidates']}")
    if _works(col, "batch"):
        return _check(not rejected and not summary["unavailable"] and summary["shortlist"] > 0, detail)
    return _check(not rejected, detail)


# ── scope 7: Create with AI template pick ───────────────────────────────────

def _s7(prompt: str, expected: str) -> Callable[[str, Ctx], Outcome]:
    def run(col: str, ctx: Ctx) -> Outcome:
        decision = create_ai.decide(prompt)
        confidence = decision.get("confidence")
        detail = (f"detected {decision.get('detected')} "
                  f"({confidence if confidence is None else round(confidence, 2)}, band {decision.get('band')}); "
                  f"template used: {decision['template']}")
        want = expected if _works(col, "classify") else create_ai.GENERIC
        return _check(decision["template"] == want, detail)
    return run


# ── scope 8: open-ended quiz scoring ────────────────────────────────────────

def _grade(ctx: Ctx) -> dict[str, int]:
    """Three fresh responses with the scope page's answers, then open the grading list."""
    if "quiz" not in ctx:
        ctx["quiz"] = {name: engine.take(seed.QUIZ, {"q1": "75°C", "q2": "Up to 2 hours", "q3": text})
                       for name, text in QUIZ_ANSWERS.items()}
        events.emit("grading.opened", survey_no=seed.QUIZ, question=engine.question_by_qid(seed.QUIZ, "q3"))
    return ctx["quiz"]


def _s8(name: str, grade: str, found: int) -> Callable[[str, Ctx], Outcome]:
    def run(col: str, ctx: Ctx) -> Outcome:
        row = engine.post_populated(_grade(ctx)[name]).get("q3")
        if not _works(col, "batch"):
            return _check(row is None, "no suggestion; the grader grades by hand" if row is None
                          else f"unexpected suggestion: {row['feedback']}")
        if row is None:
            return "fail", "no suggestion was written"
        detail = row["detail"]
        return _check(detail.get("grade") == grade and len(detail.get("found", [])) == found,
                      f"{row['feedback']}; confidence {detail.get('confidence')}, band {detail.get('band')}")
    return run


def s8_not_final(col: str, ctx: Ctx) -> Outcome:
    rows = [engine.post_populated(rid).get("q3") for rid in _grade(ctx).values()]
    final = [r for r in rows if r and r["status"] != "draft"]
    return _check(not final, f"suggested grades: {sum(1 for r in rows if r)}; final without a grader: {len(final)}")


def s8_jobs(col: str, ctx: Ctx) -> Outcome:
    if col != "normal":
        return "na", "the jobs polling test runs once, against a working gateway"
    started = quiz_scoring.start_job(seed.QUIZ, "q3", JOB_ITEMS)
    if started.fallback:
        return "fail", f"job not accepted: {started.message}"
    deadline = time.time() + JOB_WAIT_S
    status: dict[str, Any] = started.data
    while status.get("status") in ("queued", "running") and time.time() < deadline:
        time.sleep(0.5)
        polled = quiz_scoring.job_status(status["job_id"])
        if polled.fallback:
            return "fail", f"polling failed: {polled.message}"
        status = polled.data
    return _check(status.get("status") == "done" and status.get("processed") == status.get("total"),
                  f"job {status.get('job_id')}: {status.get('status')}, {status.get('processed')}/"
                  f"{status.get('total')} items, {status.get('items_per_s')} items/s. Output is a file on the "
                  f"gateway host ({status.get('output_path')}); no endpoint returns it")


SCENARIOS: list[Scenario] = [
    Scenario("1a", "1 Shared classes", "A saved class set reads back from the gateway and matches", s1_roundtrip),
    Scenario("1b", "1 Shared classes", "One classify call for a question that a branch and an alert both read",
             s1_one_call),
    Scenario("1c", "1 Shared classes", "Editing the class set changes the hash; a new response is classified again",
             s1_hash_changes),
    Scenario("1d", "1 Shared classes", "Clinic example: service-recovery branch and the Complaint and Negative email",
             s1_clinic),
    Scenario("2a", "2 Alerts on meaning", "NPS 9 \"Love it, but…\" fires may-leave only", s2_may_leave),
    Scenario("2b", "2 Alerts on meaning", "With the module off, no meaning alert fires", s2_module_off),
    Scenario("2c", "2 Alerts on meaning", "Webhook only when the comment is a complaint", s2_webhook),
    Scenario("2d", "2 Alerts on meaning", "Score-based rules still fire; submission is not blocked", s2_score_rules),
    Scenario("2e", "2 Alerts on meaning", "Anonymous project: the email names no respondent", s2_anonymous),
    Scenario("3a", "3 Logic on text", "Three paraphrases of price show the pricing question; Contains shows none",
             s3_price),
    Scenario("3b", "3 Logic on text", "NPS 3 with the sofa comment shows the delivery follow-up", s3_delivery),
    Scenario("3c", "3 Logic on text", "Offline Mode makes no gateway call", s3_offline),
    Scenario("4a", "4 Sensitive hints", "Mandatory income question gets a hint", s4_income),
    Scenario("4b", "4 Sensitive hints", "\"Which branch did you visit?\" gets none", s4_branch),
    Scenario("4c", "4 Sensitive hints", "Only the question wording is sent", s4_wording_only),
    Scenario("4d", "4 Sensitive hints", "A dismissed hint stays dismissed", s4_dismiss),
    Scenario("5a", "5 Follow-up flag", "\"What is the main reason for your score?\" is suggested", s5_reason),
    Scenario("5b", "5 Follow-up flag", "\"Please enter your order number\" is not", s5_order),
    Scenario("5c", "5 Follow-up flag", "Modules 4 and 5 share one gateway call", s5_one_call),
    Scenario("6a", "6 Tag suggestions", "\"Grand – Downtown\" is suggested Downtown", s6_downtown),
    Scenario("6b", "6 Tag suggestions", "Room cleanliness → Housekeeping; breakfast → Restaurant", s6_questions),
    Scenario("6c", "6 Tag suggestions", "Nothing is applied until Accept", s6_not_applied),
    Scenario("6d", "6 Tag suggestions", "A 500-tag category completes; the shortlist path is recorded", s6_large),
] + [Scenario(f"7{'abcd'[i]}", "7 Template pick", f"\"{prompt}\" → {expected}", _s7(prompt, expected))
     for i, (prompt, expected) in enumerate(PROMPTS)] + [
    Scenario("8a", "8 Quiz scoring", "Full answer → 3 of 3 key points, Correct", _s8("correct", "correct", 3)),
    Scenario("8b", "8 Quiz scoring", "Partial answer → 2 of 3, Partly correct", _s8("partly", "partly_correct", 2)),
    Scenario("8c", "8 Quiz scoring", "\"Keep it covered.\" → 0 of 3, Incorrect", _s8("incorrect", "incorrect", 0)),
    Scenario("8d", "8 Quiz scoring", "No suggested grade is final without a grader", s8_not_final),
    Scenario("8e", "8 Quiz scoring", "A job can be followed to done by polling", s8_jobs),
]


# ── running ─────────────────────────────────────────────────────────────────

def _endpoint_group(endpoint: str) -> str:
    if endpoint.startswith("/v1/jobs/"):
        return "/v1/jobs/{job_id}"
    if endpoint.startswith("/v1/bindings/"):
        return "/v1/bindings/{survey_no}/{question_id}"
    return endpoint


def _latency(first: int, last: int) -> list[dict[str, Any]]:
    """Latency by call type for the calls of one column."""
    calls = db.rows("SELECT method, kind, endpoint, client_ms, fallback, cached FROM gateway_call_log"
                    " WHERE id>? AND id<=?", first, last)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for call in calls:
        key = (call["kind"] or "", f"{call['method']} {_endpoint_group(call['endpoint'])}")
        groups.setdefault(key, []).append(call)
    timeouts = get_settings().timeouts_ms
    out = []
    for (kind, endpoint), members in sorted(groups.items()):
        timeout = timeouts.get(kind, 0)
        within = sum(1 for c in members if not c["fallback"] and c["client_ms"] <= timeout)
        out.append({"kind": kind, "endpoint": endpoint, "timeout_ms": timeout, **gateway.summarise(members),
                    "within_timeout_pct": round(100.0 * within / len(members), 1)})
    return out


def _run() -> None:
    run_id = STATE["run_id"]
    results: dict[str, dict[str, Any]] = {s.id: {} for s in SCENARIOS}
    latency: list[dict[str, Any]] = []
    everything_on = {m: True for m in modules.MODULE_IDS}
    try:
        with modules.override(everything_on):
            for col, _, faults in COLUMNS:
                with gateway.override_faults({}):
                    seed.sync_class_sets()
                ctx: Ctx = {}
                column_first = _last_log_id()
                with gateway.override_faults(faults):
                    for scenario in SCENARIOS:
                        STATE["current"] = f"{col}: {scenario.id} {scenario.title}"
                        first = _last_log_id()
                        try:
                            status, detail = scenario.fn(col, ctx)
                        except Exception as exc:
                            logger.exception("Scenario %s failed in column %s", scenario.id, col)
                            status, detail = "fail", f"error: {exc!r}"
                        results[scenario.id][col] = {"status": status, "detail": detail,
                                                     "log_from": first, "log_to": _last_log_id()}
                        STATE["done"] += 1
                if col == "normal":
                    latency = _latency(column_first, _last_log_id())
                if "scratch" in ctx:
                    engine.delete_project(ctx["scratch"])
    finally:
        db.run("UPDATE scenario_run SET finished_at=?, results=? WHERE id=?", db.now(),
               db.dumps({"results": results, "latency": latency}), run_id)
        STATE.update(running=False, current="")


def start() -> bool:
    """Run every scenario in every column on a background thread."""
    with _lock:
        if STATE["running"]:
            return False
        run_id = db.run("INSERT INTO scenario_run(started_at) VALUES(?)", db.now())
        STATE.update(running=True, done=0, total=len(SCENARIOS) * len(COLUMNS), current="starting", run_id=run_id)
    threading.Thread(target=_run, name="scenario-run", daemon=True).start()
    return True


def last_run() -> Optional[dict[str, Any]]:
    run = db.one("SELECT * FROM scenario_run WHERE results IS NOT NULL ORDER BY id DESC LIMIT 1")
    if run:
        stored = db.loads(run["results"], {})
        run["results"], run["latency"] = stored.get("results", {}), stored.get("latency", [])
    return run


def export_csv(run: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["scope", "check", "title"] + [label for _, label, _ in COLUMNS]
                    + [f"{label}: detail" for _, label, _ in COLUMNS])
    for scenario in SCENARIOS:
        cells = run["results"].get(scenario.id, {})
        writer.writerow([scenario.scope, scenario.id, scenario.title]
                        + [cells.get(col, {}).get("status", "") for col, _, _ in COLUMNS]
                        + [cells.get(col, {}).get("detail", "") for col, _, _ in COLUMNS])
    writer.writerow([])
    writer.writerow(["Latency, normal column", "endpoint", "timeout_ms", "calls", "fallback_rate_pct",
                     "cache_hit_rate_pct", "p50_ms", "p95_ms", "within_timeout_pct"])
    for row in run["latency"]:
        writer.writerow([row["kind"], row["endpoint"], row["timeout_ms"], row["calls"], row["fallback_rate"],
                         row["cache_hit_rate"], row["p50_ms"], row["p95_ms"], row["within_timeout_pct"]])
    return out.getvalue()
