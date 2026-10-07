"""The labelled sets for judging answer quality: 1,000 texts in seven sets, one call each.

A call carries every question that applies to its text, so one text gives several
scored decisions (a comment is judged on all four alert meanings in one call).
The texts live in simulators/fixtures/task_eval/*.jsonl; this module says which
questions each set asks and what the expected answer to each is.

    set          texts  questions per text
    quiz           180  key points (two wordings), grade                 scope 8
    alerts         170  the four ready-made meanings                     scope 2
    tags           150  a tag for an answer option, or tags for a question   scope 6
    builder        150  sensitive, asks for personal data, "why" follow-up   scopes 4, 9, 5
    pii_answers    130  personal data in an answer                       scope 10
    logic          120  four "answer is about…" topics                   scope 3
    templates      100  project type of a Create with AI prompt          scope 7

Two more sets are kept but switched off (`enabled: False`): the shared classes
(scope 1) and the gateway tasks no Design scope uses. `--include-disabled` runs them.

The inline questions are copies of what the Sogo-lite modules send
(ui/sogo_lite/modules); keep the two in step. Every text is invented and the labels
are one author's judgement.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

DATA_DIR = Path(__file__).parent / "fixtures" / "task_eval"
TIERS = ("easy", "medium", "hard")
# Expected answers for which nothing is due: staying quiet is the right result.
NOTHING_DUE = {"no", "other", "none"}

Check = dict[str, Any]      # one scored decision: question, answer_id, kind, expected, accept, item
Plan = dict[str, Any]       # request fields for one call, plus its checks


def label_slug(label: str) -> str:
    """How the gateway names a `labels` answer: `{id}.{slug}` (app/registry/compiler.py)."""
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", label.strip()).strip("_") or "label"


def rows(name: str) -> list[dict[str, Any]]:
    with (DATA_DIR / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _json(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _check(question: str, answer_id: str, kind: str, expected: str, *, item: str = "",
           accept: Optional[list[str]] = None) -> Check:
    return {"question": question, "answer_id": answer_id, "kind": kind, "expected": expected,
            "accept": accept or [expected], "item": item}


def _yesno(question: str, answer_id: str, yes: bool, item: str = "") -> Check:
    return _check(question, answer_id, "yesno", "yes" if yes else "no", item=item)


def _labels(question: str, spec_id: str, labels: list[str], expected: list[str]) -> list[Check]:
    """A `labels` question is one yes/no decision per label."""
    return [_yesno(question, f"{spec_id}.{label_slug(label)}", label in expected, item=label) for label in labels]


def _context(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("survey_question", "metric_score", "context") if key in row}


# ── scope 2: alerts on comment meaning ──────────────────────────────────────

MEANINGS = ["may_leave", "safety_concern", "complaint", "callback_request"]


def _alerts(row: dict[str, Any]) -> Plan:
    return {"tasks": [f"meaning.{m}" for m in MEANINGS], **_context(row),
            "checks": [_yesno(f"meaning.{m}", f"meaning.{m}", m in row["expected"]) for m in MEANINGS]}


# ── scope 3: logic on text answers ──────────────────────────────────────────

TOPICS = {
    "price": ("price", "cost, fees, too expensive, value for money, discounts, price rises"),
    "delivery": ("delivery", "shipping time, courier, driver, damaged or late arrival, tracking, "
                             "collection of an order"),
    "staff": ("staff", "the behaviour, helpfulness, knowledge or attitude of employees or support agents"),
    "website_app": ("the website or app", "the website or mobile app: login, checkout, search, crashes, "
                                          "usability"),
}


def _about(key: str) -> dict[str, Any]:
    topic, description = TOPICS[key]
    return {"id": f"about_{key}", "type": "yesno", "instructions": f"Is this survey answer about {topic}?",
            "classes": {"true": f"the answer is about {topic}: {description}",
                        "false": f"the answer is not about {topic}"}}


def _logic(row: dict[str, Any]) -> Plan:
    return {"questions": [_about(key) for key in TOPICS], **_context(row),
            "checks": [_yesno(f"about.{key}", f"about_{key}", key in row["expected"]) for key in TOPICS]}


# ── scopes 4, 5 and 9: builder hints (ui/sogo_lite/modules/design_hints.py, pii.py) ──

SENSITIVE_SPEC: dict[str, Any] = {
    "id": "sensitive", "type": "yesno",
    "instructions": "Does this survey question ask for sensitive personal information?",
    "classes": {"true": "asks about income, finances, health, religion, sexuality, politics, criminal record "
                        "or another private matter people may not want to disclose",
                "false": "an everyday question people answer without discomfort"}}
FOLLOWUP_SPEC: dict[str, Any] = {
    "id": "followup", "type": "yesno",
    "instructions": "Does this survey question ask someone to explain the reason for a score or rating they gave?",
    "classes": {"true": "asks why, or for the main reason behind, a score or rating",
                "false": "asks for a fact, a detail or anything other than the reason for a score"}}
PII_QUESTION_SPEC: dict[str, Any] = {
    "id": "pii_q", "type": "choice",
    "instructions": "What personal data that identifies a person does this survey question ask the "
                    "respondent to give?",
    "classes": {
        "name": "the person's own first name, surname or full name",
        "contact": "an email address, phone number, or home or postal address",
        "government_id": "a passport, national ID, social security, tax or driving licence number",
        "date_of_birth": "a full date of birth",
        "financial_account": "a bank account, card number or other payment details",
        "account_id": "an employee, customer, patient, student or membership number, a username or a password",
        "none": "asks for nothing that identifies the person: an opinion, rating, choice, or a broad category "
                "such as age band, city, department or job role"}}
PII_ANSWER_SPEC: dict[str, Any] = {
    "id": "pii_a", "type": "choice",
    "instructions": "What personal data that identifies a person does this survey answer contain?",
    "classes": {
        "name": "the name of a specific person, the respondent or someone else such as a staff member",
        "contact": "an email address, phone number, or home or postal address",
        "government_id": "a passport, national ID, social security, tax or driving licence number",
        "date_of_birth": "a full date of birth",
        "financial_account": "a bank account number, card number or other payment details",
        "account_id": "an employee, customer, patient, student, order or membership number, a username "
                      "or a password",
        "none": "contains nothing that identifies a person: opinions, descriptions, or general details such "
                "as a city, a job role or an age band"}}


def _builder(row: dict[str, Any]) -> Plan:
    expected = row["expected"]
    return {"questions": [SENSITIVE_SPEC, PII_QUESTION_SPEC, FOLLOWUP_SPEC], "checks": [
        _yesno("sensitive_question", "sensitive", expected["sensitive"] == "yes"),
        _check("pii_in_question", "pii_q", "choice", expected["pii"], accept=expected.get("pii_accept")),
        _yesno("followup_question", "followup", expected["followup"] == "yes")]}


def _pii_answers(row: dict[str, Any]) -> Plan:
    return {"questions": [PII_ANSWER_SPEC], **_context(row),
            "checks": [_check("pii_in_answer", "pii_a", "choice", row["expected"], accept=row.get("accept"))]}


# ── scope 6: tag suggestions (ui/sogo_lite/modules/tag_suggest.py) ──────────

def _tags(row: dict[str, Any]) -> Plan:
    category = TAG_CATEGORIES[row["category"]]
    classes = {tag: "" for tag in category["tags"]}
    if category["kind"] == "option":
        spec = {"id": "tag", "type": "choice", "classes": classes,
                "instructions": "Which of these tags best describes this survey answer option?"}
        return {"questions": [spec], "survey_question": category["survey_question"],
                "checks": [_check("tag_for_option", "tag", "choice", row["expected"])]}
    return {"questions": [{"id": "tag", "type": "labels", "classes": classes}],
            "checks": _labels("tags_for_question", "tag", category["tags"], row["expected"])}


# ── scope 7: Create with AI template pick (ui/sogo_lite/modules/template_pick.py) ──

PROJECT_TYPE_SPEC: dict[str, Any] = {
    "id": "project_type", "type": "choice",
    "instructions": "What kind of project is this request asking to create?",
    "classes": {
        "Survey": "a general questionnaire or research study that fits none of the other types",
        "CX Project": "customer feedback on a purchase, product, service or support contact; NPS, CSAT or CES",
        "EX Project": "employee or staff feedback: engagement, workplace, policies, how staff feel",
        "Assessment": "a quiz, test or exam with right and wrong answers, scores or a pass mark",
        "Poll": "a quick vote or single question to choose between options"}}


def _templates(row: dict[str, Any]) -> Plan:
    return {"questions": [PROJECT_TYPE_SPEC],
            "checks": [_check("project_type", "project_type", "choice", row["expected"])]}


# ── scope 8: quiz scoring (ui/sogo_lite/modules/quiz_scoring.py) ────────────

def quiz_specs(points: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The questions for one quiz answer, by part. `key_points` and `grade` are what the
    module sends today. `key_points_strict` is the reworded key-point check under test:
    a `labels` question compiles to "the answer is about X", which credits a point the
    answer only touches on; the strict wording asks whether the point is stated."""
    expected = "; ".join(points)
    return {
        "key_points": [{"id": "kp", "type": "labels", "classes": {point: "" for point in points},
                        "instructions": "Does this quiz answer state this point?"}],
        "key_points_strict": [
            {"id": f"kps_{i}", "type": "yesno",
             "instructions": f"Does this quiz answer explicitly state this point: {point}?",
             "classes": {"true": f"the answer states, in its own words, that: {point}",
                         "false": "the answer does not state this point: it leaves it out, only hints at "
                                  "it, or says something different"}}
            for i, point in enumerate(points, start=1)],
        "grade": [{"id": "grade", "type": "choice",
                   "instructions": "How well does this quiz answer match the expected answer?",
                   "classes": {
                       "correct": f"covers every expected point: {expected}",
                       "partly_correct": f"covers some but not all of the expected points: {expected}",
                       "incorrect": "a real attempt that covers none of the expected points or gets them wrong",
                       "not_an_answer": "blank, off-topic, a joke or declines to answer"}}],
    }


def _quiz(row: dict[str, Any]) -> Plan:
    question = QUIZ_QUESTIONS[row["question"]]
    points, found = question["key_points"], row["expected"]["points"]
    specs = quiz_specs(points)
    checks = _labels("quiz_key_points", "kp", points, found)
    checks += [_yesno("quiz_key_points_strict", f"kps_{i}", point in found, item=point)
               for i, point in enumerate(points, start=1)]
    checks.append(_check("quiz_grade", "grade", "choice", row["expected"]["grade"]))
    return {"questions": [spec for part in specs.values() for spec in part],
            "survey_question": question["question"], "checks": checks,
            # For the split check: the same questions, one part per call.
            "parts": {name: {"questions": part, "survey_question": question["question"]}
                      for name, part in specs.items()}}


def grade_from_points(found: int, total: int) -> str:
    """The grade the key points alone imply. It cannot tell a wrong answer from a non-answer."""
    return "correct" if found == total else "partly_correct" if found else "incorrect"


# ── the parked sets: one catalog task per text ──────────────────────────────

YESNO_TASKS = {"comment.actionable", "comment.urgent"}


def _single_task(row: dict[str, Any]) -> Plan:
    task = row["task"]
    return {"tasks": [task], **_context(row),
            "checks": [_check(task, task, "yesno" if task in YESNO_TASKS else "choice", row["expected"])]}


# ── the sets ────────────────────────────────────────────────────────────────

def _set(set_id: str, scope: str, use: str, plan: Callable[[dict[str, Any]], Plan],
         enabled: bool = True) -> dict[str, Any]:
    return {"id": set_id, "scope": scope, "use": use, "plan": plan, "enabled": enabled}


SETS = [
    _set("quiz", "8 Quiz scoring", "Key points and a grade for a written quiz answer", _quiz),
    _set("alerts", "2 Alerts on comment meaning", "Rules & Alerts: the comment means…", _alerts),
    _set("tags", "6 Tag suggestions", "Suggest a tag for an answer option, or tags for a question", _tags),
    _set("builder", "4, 5, 9 Builder hints", "Sensitive question, personal data asked, \"why\" follow-up",
         _builder),
    _set("pii_answers", "10 Personal data in answers", "Flag a text answer that contains personal data",
         _pii_answers),
    _set("logic", "3 Logic on text answers", "Logic: the answer is about…", _logic),
    _set("templates", "7 Create with AI", "Pick the project template from the prompt", _templates),
    _set("shared_classes", "1 Shared classes", "Sentiment and response type (parked)", _single_task,
         enabled=False),
    _set("other_gateway", "none", "Gateway tasks no Design scope uses (parked)", _single_task, enabled=False),
]

TAG_CATEGORIES: dict[str, dict[str, Any]] = _json("tag_categories.json")
QUIZ_QUESTIONS: dict[str, dict[str, Any]] = _json("quiz_questions.json")
