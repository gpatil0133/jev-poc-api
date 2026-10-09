"""Feature concepts: Survey Coach and Question type pick.

Before launch, on `question.saved`:
  - wording flaw (leading, double-barrelled, …) -> a hint with fixed advice for that flaw.
    The classifier picks the flaw; it cannot write a reworded question.
  - question type -> a hint to change the type when the wording reads like another one.
Both ride in one classify call, separate from the scope 4, 5 and 9 hints: these
two read the answer options as well as the wording.

After launch, on demand: which of the survey's own questions is each comment
about, or "none of these"? A large "none of these" share is a blind spot. Naming
what those comments share needs a list of likely topics from the author.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Optional

from sogo_lite import db, engine, events, gateway, modules
from sogo_lite.modules import free_text, inferred

WORDING_MODULE = "survey_coach"
QTYPE_MODULE = "qtype_pick"
BLIND_SPOT_MODULE = "coach_blind_spot"
WORDING_TASK = "design.wording_flaw"
QTYPE_TASK = "design.question_type"
KIND_MODULES = {"wording": WORDING_MODULE, "qtype": QTYPE_MODULE}

# flaw -> (name, advice). "sensitive" is left to the scope 4 hint, which also proposes the fix.
ADVICE = {
    "leading": ("Leading question", "Remove words that praise or criticise, so the wording does not push toward "
                                    "an answer. For example: \"How would you rate our staff?\""),
    "double_barrelled": ("Double-barrelled question", "It asks about two things at once. Split it into two questions."),
    "vague_time_frame": ("Vague time frame", "Name the period, for example \"in the last 30 days\"."),
    "jargon": ("Jargon", "Replace internal terms and abbreviations with words a respondent would use."),
    "overlapping_options": ("Overlapping answer options", "Give each option its own range with no overlap or gap, "
                                                          "for example 0–4, 5–9, 10 or more."),
}
# classifier label -> (mock question type, metric kind, name shown to the author)
QTYPES = {
    "rating": ("metric", "csat", "a rating"),
    "nps": ("metric", "nps", "an NPS question"),
    "multiple_choice": ("radio", None, "multiple choice"),
    "open_text": ("text", None, "an open text question"),
    "yes_no": ("radio", None, "a yes / no question"),
}
NONE_OF_THESE = "none_of_these"
SAMPLE_SIZE = 400
BATCH_SIZE = 64
MAX_QUESTIONS = 30
MIN_NONE_SHARE = 15.0
MIN_TOPIC_SHARE = 10.0
MAX_EXAMPLES = 15


# ── before launch ───────────────────────────────────────────────────────────

def _signature(question: dict[str, Any], tasks: list[str]) -> str:
    raw = "|".join([question["wording"], question["type"], question["metric_kind"] or "",
                    ";".join(o["text"] for o in question["options"]), ",".join(tasks)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _fits(label: str, question: dict[str, Any]) -> bool:
    """Does the question already have the type the classifier picked?"""
    qtype, metric_kind, _ = QTYPES[label]
    if qtype == "radio":
        return question["type"] in ("radio", "checkbox")
    if label == "nps":
        return question["type"] == "metric" and question["metric_kind"] == metric_kind
    return question["type"] == qtype


def _on_question_saved(question: dict[str, Any], **_: Any) -> None:
    wanted = [(module_id, task) for module_id, task in ((WORDING_MODULE, WORDING_TASK), (QTYPE_MODULE, QTYPE_TASK))
              if modules.is_on(module_id)]
    if not wanted:
        return
    signature = _signature(question, [task for _, task in wanted])
    if question["coach_sig"] == signature:
        return      # Saved again without changes: a dismissed hint stays dismissed.
    context = {"question": question["wording"]}
    if question["options"]:
        context["answer_options"] = "; ".join(o["text"] for o in question["options"])
    body = {"text": question["wording"], "tasks": [task for _, task in wanted], "context": context}
    result = gateway.call("POST", "/v1/classify", body, module="+".join(m for m, _ in wanted),
                          trigger="question.saved", kind="author", survey_no=question["survey_no"])
    db.run("DELETE FROM suggestion WHERE target_type='question' AND target_id=? AND kind IN ('wording', 'qtype')",
           question["id"])
    if result.fallback:
        gateway.set_outcome(result.log_id, "no hint (fallback)")
        return
    if "forced_low_confidence" not in result.faults:
        db.run("UPDATE question SET coach_sig=? WHERE id=?", signature, question["id"])

    outcomes: list[str] = []
    flaw = result.answers.get(WORDING_TASK) or {}
    if flaw.get("label") in ADVICE and flaw.get("band") in ("act", "suggest"):
        _suggest("wording", question, {"flaw": flaw["label"]}, flaw, signature)
        outcomes.append(f"wording hint created: {flaw['label']}")
    picked = result.answers.get(QTYPE_TASK) or {}
    if picked.get("label") in QTYPES and picked.get("band") in ("act", "suggest") \
            and not _fits(picked["label"], question):
        qtype, metric_kind, _ = QTYPES[picked["label"]]
        _suggest("qtype", question, {"label": picked["label"], "type": qtype, "metric_kind": metric_kind},
                 picked, signature)
        outcomes.append(f"question type hint created: {picked['label']}")
    gateway.set_outcome(result.log_id, "; ".join(outcomes) or "no hint")


def _suggest(kind: str, question: dict[str, Any], proposed: dict[str, Any], answer: dict[str, Any],
             signature: str) -> None:
    db.run("INSERT INTO suggestion(kind, survey_no, target_type, target_id, signature, proposed, confidence,"
           " band, created_at) VALUES(?,?,?,?,?,?,?,?,?)", kind, question["survey_no"], "question",
           question["id"], signature, db.dumps(proposed), answer.get("confidence"), answer["band"], db.now())


def pending(question_id: int) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM suggestion WHERE target_type='question' AND target_id=? AND status='pending'"
                    " AND kind IN ('wording', 'qtype') ORDER BY id", question_id)
    for s in found:
        s["proposed"] = db.loads(s["proposed"], {})
    return [s for s in found if modules.is_on(KIND_MODULES[s["kind"]])]


def apply(suggestion: dict[str, Any]) -> None:
    """A wording hint changes nothing: the author rewords the question. A type hint
    changes the type, and gives a yes / no question its two options."""
    proposed = db.loads(suggestion["proposed"], {})
    if suggestion["kind"] != "qtype":
        return
    question = engine.question(suggestion["target_id"])
    if not question:
        return
    db.run("UPDATE question SET type=?, metric_kind=?, is_followup=CASE WHEN ?='text' THEN is_followup ELSE 0 END"
           " WHERE id=?", proposed["type"], proposed.get("metric_kind"), proposed["type"], question["id"])
    if proposed.get("label") == "yes_no" and not question["options"]:
        engine.set_options(question["id"], [("Yes", 0.0), ("No", 0.0)])


# ── after launch: what no question asks ─────────────────────────────────────

def closed_questions(survey_no: int) -> list[dict[str, Any]]:
    return [q for q in engine.questions(survey_no) if q["type"] != "text"][:MAX_QUESTIONS]


def comment_questions(survey_no: int) -> list[dict[str, Any]]:
    return [q for q in engine.questions(survey_no)
            if q["type"] == "text" and not free_text.has_list(survey_no, q["qid"])]


def _batches(survey_no: int, question: dict[str, Any], spec: dict[str, Any], comments: list[dict[str, Any]],
             trigger: str) -> Optional[dict[int, dict[str, Any]]]:
    """Answers by response id, or None when the gateway did not answer."""
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(comments), BATCH_SIZE):
        chunk = comments[start:start + BATCH_SIZE]
        items = [{"id": str(c["response_id"]), **inferred.item(c["response_id"], question, c["text"])}
                 for c in chunk]
        result = gateway.call("POST", "/v1/classify/batch", {"questions": [spec], "items": items},
                              module=BLIND_SPOT_MODULE, trigger=trigger, kind="batch", survey_no=survey_no)
        if result.fallback:
            gateway.set_outcome(result.log_id, "no result (fallback)")
            return None
        for row in result.data.get("results") or []:
            answer = (row.get("answers") or {}).get(spec["id"])
            if answer and str(row.get("id") or "").isdigit():
                out[int(row["id"])] = answer
        gateway.set_outcome(result.log_id, f"{len(chunk)} comment(s) matched to the survey's questions"
                            if spec["id"] == "about_question" else f"{len(chunk)} comment(s) matched to a topic")
    return out


def run_blind_spot(survey_no: int, qid: str, topic_lines: str = "") -> tuple[bool, str]:
    question = engine.question_by_qid(survey_no, qid)
    closed = closed_questions(survey_no)
    if not question or question["type"] != "text":
        return False, "Pick the Text Box whose comments should be checked."
    if len(closed) < 2:
        return False, "The survey needs at least two closed questions to compare the comments with."
    comments = db.rows("SELECT r.id AS response_id, a.text FROM response r JOIN response_answer a"
                       " ON a.response_id=r.id WHERE r.survey_no=? AND a.qid=? AND a.text IS NOT NULL"
                       " AND r.submitted_at IS NOT NULL ORDER BY r.id", survey_no, qid)
    if not comments:
        return False, "There are no comments on that question yet."
    total = len(comments)
    comments = random.Random(survey_no).sample(comments, min(SAMPLE_SIZE, total))
    spec = {"id": "about_question", "type": "choice",
            "instructions": "Which survey question is this comment mainly about?",
            "classes": {**{q["qid"]: q["wording"][:120] for q in closed},
                        NONE_OF_THESE: "about something no listed question asks, or not a real answer"}}
    answers = _batches(survey_no, question, spec, comments, "Survey Coach: blind-spot check")
    if answers is None:
        return False, "No result: the gateway did not answer. Nothing was changed."
    matched = {q["qid"]: 0 for q in closed}
    none: list[dict[str, Any]] = []
    unsure = 0
    for comment in comments:
        answer = answers.get(comment["response_id"]) or {}
        if answer.get("label") == NONE_OF_THESE and not answer.get("_forced"):
            none.append(comment)
        elif answer.get("label") in matched and answer.get("band") in ("act", "suggest"):
            matched[answer["label"]] += 1
        else:
            unsure += 1
    result: dict[str, Any] = {"total": total, "checked": len(comments), "matched": matched, "none": len(none),
                              "unsure": unsure, "none_share": round(100.0 * len(none) / len(comments), 1),
                              "examples": [c["text"] for c in none[:MAX_EXAMPLES]], "topics": {}}
    topics = free_text.parse_lines(topic_lines)
    if len(topics) >= 2 and none:
        named = _batches(survey_no, question, {"id": "topic", "type": "choice", "classes": topics,
                                               "instructions": "What is this comment mainly about?"},
                         none, "Survey Coach: name the blind spot")
        if named is not None:
            counts = {topic: 0 for topic in topics}
            for answer in named.values():
                if answer.get("label") in counts and answer.get("band") in ("act", "suggest"):
                    counts[answer["label"]] += 1
            result["topics"] = {topic: {"count": n, "share": round(100.0 * n / len(comments), 1)}
                                for topic, n in counts.items() if n}
    db.run("INSERT INTO coach_run(survey_no, qid, created_at, result) VALUES(?,?,?,?)", survey_no, qid, db.now(),
           db.dumps(result))
    return True, f"{len(comments)} comment(s) checked; {len(none)} fit no question."


def latest_blind_spot(survey_no: int) -> Optional[dict[str, Any]]:
    run = db.one("SELECT * FROM coach_run WHERE survey_no=? ORDER BY id DESC LIMIT 1", survey_no)
    if not run:
        return None
    run["result"] = db.loads(run["result"], {})
    result = run["result"]
    run["suggest"] = result.get("none_share", 0) >= MIN_NONE_SHARE
    run["suggested_topics"] = [topic for topic, t in (result.get("topics") or {}).items()
                               if t["share"] >= MIN_TOPIC_SHARE]
    return run


events.subscribe("question.saved", None, _on_question_saved)
