"""Baseline platform (plan section 5): today's Design behaviour, cut down.

Nothing in this file calls the gateway. Scope modules hook in through events and
through the two registries below (extra Logic operators, extra alert conditions).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from sogo_lite import db, events, modules

logger = logging.getLogger(__name__)

PROJECT_TYPES = ["Survey", "CX Project", "EX Project", "Assessment", "Poll"]
QUESTION_TYPES = {"radio": "Radio Button", "checkbox": "Check Box", "metric": "Metric rating", "text": "Text Box"}
# kind -> (label, lowest, highest)
METRICS = {"nps": ("NPS", 0, 10), "csat": ("CSAT", 1, 5), "ces": ("CES", 1, 7)}
REQUIRED_MODES = {"none": "Neither", "mandatory": "Mandatory", "encouraged": "Encouraged Response"}
OPTION_OPS = {"picked": "option is picked", "not_picked": "option is not picked"}
TEXT_OPS = {"contains": "Contains", "not_contains": "Does not contain", "starts_with": "starts with",
            "is_exactly": "is exactly", "answered": "is answered", "not_answered": "is not answered"}
ACTIONS = {"show": "show a question", "jump": "jump to a page"}
SINKS = {"email": "Send an email", "webhook": "Call a webhook", "salesforce": "Push to Salesforce"}
FIRST_SURVEY_NO = 9001
MAX_TAG_CATEGORIES = 10
MAX_TAGS_PER_CATEGORY = 500

Answer = dict[str, Any]          # {"option_ids": [int], "text": str|None, "number": float|None}
Context = dict[str, Any]         # {"survey_no", "response", "answers"}

# op -> (module id, fn(rule, ctx) -> bool)
LOGIC_OPS_EXT: dict[str, tuple[str, Callable[[dict, Context], bool]]] = {}
# condition type -> (module id, fn(condition, ctx) -> (met, detail))
ALERT_CONDITIONS_EXT: dict[str, tuple[str, Callable[[dict, Context], tuple[bool, str]]]] = {}


def register_logic_op(op: str, module_id: str, fn: Callable[[dict, Context], bool]) -> None:
    LOGIC_OPS_EXT[op] = (module_id, fn)


def register_alert_condition(ctype: str, module_id: str,
                             fn: Callable[[dict, Context], tuple[bool, str]]) -> None:
    ALERT_CONDITIONS_EXT[ctype] = (module_id, fn)


# ── projects, pages, questions ──────────────────────────────────────────────

def projects() -> list[dict[str, Any]]:
    return db.rows(
        "SELECT p.*, (SELECT COUNT(*) FROM question q WHERE q.survey_no=p.survey_no) AS questions,"
        " (SELECT COUNT(*) FROM response r WHERE r.survey_no=p.survey_no AND r.submitted_at IS NOT NULL)"
        " AS responses FROM project p ORDER BY p.survey_no")


def project(survey_no: int) -> Optional[dict[str, Any]]:
    return db.one("SELECT * FROM project WHERE survey_no=?", survey_no)


def create_project(name: str, ptype: str, anonymous: bool = False,
                   survey_no: Optional[int] = None) -> int:
    if survey_no is None:
        survey_no = max(FIRST_SURVEY_NO, (db.val("SELECT MAX(survey_no) FROM project") or 0) + 1)
    db.run("INSERT INTO project(survey_no, name, type, anonymous, created_at) VALUES(?,?,?,?,?)",
           survey_no, name, ptype if ptype in PROJECT_TYPES else "Survey", 1 if anonymous else 0, db.now())
    add_page(survey_no)
    return survey_no


def delete_project(survey_no: int) -> None:
    for q in db.rows("SELECT id FROM question WHERE survey_no=?", survey_no):
        delete_question(q["id"])
    for rid in [r["id"] for r in db.rows("SELECT id FROM response WHERE survey_no=?", survey_no)]:
        for table in ("response_answer", "post_populate", "classification_result", "pii_flag"):
            db.run(f"DELETE FROM {table} WHERE response_id=?", rid)
    for table in ("response", "page", "logic_rule", "alert_rule", "sink_log", "class_set",
                  "suggestion", "key_point_set", "project"):
        db.run(f"DELETE FROM {table} WHERE survey_no=?", survey_no)


def pages(survey_no: int) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM page WHERE survey_no=? ORDER BY ord", survey_no)


def add_page(survey_no: int) -> int:
    ord_ = (db.val("SELECT MAX(ord) FROM page WHERE survey_no=?", survey_no) or 0) + 1
    return db.run("INSERT INTO page(survey_no, ord) VALUES(?,?)", survey_no, ord_)


def _with_options(question: dict[str, Any]) -> dict[str, Any]:
    question["options"] = db.rows("SELECT * FROM answer_option WHERE question_id=? ORDER BY ord", question["id"])
    return question


def questions(survey_no: int) -> list[dict[str, Any]]:
    """Every question of a project in survey order, with its options and page number."""
    found = db.rows(
        "SELECT q.*, p.ord AS page_ord FROM question q JOIN page p ON p.id=q.page_id"
        " WHERE q.survey_no=? ORDER BY p.ord, q.ord", survey_no)
    return [_with_options(q) for q in found]


def question(question_id: int) -> Optional[dict[str, Any]]:
    q = db.one("SELECT q.*, p.ord AS page_ord FROM question q JOIN page p ON p.id=q.page_id WHERE q.id=?",
               question_id)
    return _with_options(q) if q else None


def question_by_qid(survey_no: int, qid: str) -> Optional[dict[str, Any]]:
    q = db.one("SELECT q.*, p.ord AS page_ord FROM question q JOIN page p ON p.id=q.page_id"
               " WHERE q.survey_no=? AND q.qid=?", survey_no, qid)
    return _with_options(q) if q else None


def _next_qid(survey_no: int) -> str:
    """A stable id that fits the gateway's pattern, for example `q12`."""
    numbers = [int(m.group(1)) for q in db.rows("SELECT qid FROM question WHERE survey_no=?", survey_no)
               if (m := re.fullmatch(r"q(\d+)", q["qid"]))]
    return f"q{max(numbers, default=0) + 1}"


def add_question(survey_no: int, page_id: int, qtype: str, wording: str, *,
                 options: Optional[list[tuple[str, float]]] = None, metric_kind: Optional[str] = None,
                 required: str = "none", is_followup: bool = False, parent_qid: Optional[str] = None,
                 followup_min: Optional[float] = None, followup_max: Optional[float] = None,
                 max_points: float = 0, qid: Optional[str] = None) -> int:
    ord_ = (db.val("SELECT MAX(ord) FROM question WHERE page_id=?", page_id) or 0) + 1
    question_id = db.run(
        "INSERT INTO question(survey_no, page_id, qid, ord, type, metric_kind, wording, required,"
        " is_followup, parent_qid, followup_min, followup_max, max_points) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        survey_no, page_id, qid or _next_qid(survey_no), ord_, qtype,
        (metric_kind or "nps") if qtype == "metric" else None, wording.strip(), required,
        1 if is_followup else 0, parent_qid, followup_min, followup_max, max_points)
    if options and qtype in ("radio", "checkbox"):
        set_options(question_id, options)
    return question_id


def set_options(question_id: int, options: list[tuple[str, float]]) -> None:
    """Replace a question's options, keeping the id (and so the tags) of any whose text is unchanged."""
    existing = {o["text"]: o for o in db.rows("SELECT * FROM answer_option WHERE question_id=?", question_id)}
    kept: set[int] = set()
    for ord_, (text, points) in enumerate(options, start=1):
        old = existing.get(text)
        if old:
            db.run("UPDATE answer_option SET ord=?, points=? WHERE id=?", ord_, points, old["id"])
            kept.add(old["id"])
        else:
            kept.add(db.run("INSERT INTO answer_option(question_id, text, ord, points) VALUES(?,?,?,?)",
                            question_id, text, ord_, points))
    for old in existing.values():
        if old["id"] not in kept:
            db.run("DELETE FROM option_tag WHERE option_id=?", old["id"])
            db.run("DELETE FROM answer_option WHERE id=?", old["id"])


def delete_question(question_id: int) -> None:
    q = db.one("SELECT * FROM question WHERE id=?", question_id)
    if not q:
        return
    for option in db.rows("SELECT id FROM answer_option WHERE question_id=?", question_id):
        db.run("DELETE FROM option_tag WHERE option_id=?", option["id"])
    db.run("DELETE FROM answer_option WHERE question_id=?", question_id)
    db.run("DELETE FROM question_tag WHERE question_id=?", question_id)
    db.run("DELETE FROM logic_rule WHERE survey_no=? AND (source_qid=? OR (action='show' AND target=?))",
           q["survey_no"], q["qid"], q["qid"])
    db.run("DELETE FROM class_set WHERE survey_no=? AND qid=?", q["survey_no"], q["qid"])
    db.run("DELETE FROM key_point_set WHERE survey_no=? AND qid=?", q["survey_no"], q["qid"])
    db.run("DELETE FROM suggestion WHERE target_type='question' AND target_id=?", question_id)
    db.run("DELETE FROM question WHERE id=?", question_id)


def parse_option_lines(text: str) -> list[tuple[str, float]]:
    """One option per line; an optional `| points` suffix sets its score."""
    options: list[tuple[str, float]] = []
    for line in (text or "").splitlines():
        label, _, points = line.partition("|")
        label = label.strip()
        if not label or any(label == seen for seen, _ in options):
            continue
        try:
            options.append((label, float(points.strip() or 0)))
        except ValueError:
            options.append((label, 0.0))
    return options


def metric_range(q: dict[str, Any]) -> tuple[int, int]:
    _, low, high = METRICS.get(q.get("metric_kind") or "nps", METRICS["nps"])
    return low, high


# ── responses and answers ───────────────────────────────────────────────────

def response(response_id: int) -> Optional[dict[str, Any]]:
    return db.one("SELECT * FROM response WHERE id=?", response_id)


def answers(response_id: int) -> dict[str, Answer]:
    return {a["qid"]: {"option_ids": db.loads(a["option_ids"], []), "text": a["text"], "number": a["number"]}
            for a in db.rows("SELECT * FROM response_answer WHERE response_id=?", response_id)}


def is_answered(answer: Optional[Answer]) -> bool:
    if not answer:
        return False
    return bool(answer.get("option_ids")) or bool((answer.get("text") or "").strip()) \
        or answer.get("number") is not None


def answer_text(q: dict[str, Any], answer: Optional[Answer]) -> str:
    """How an answer reads in the response views."""
    if not is_answered(answer):
        return ""
    if q["type"] in ("radio", "checkbox"):
        by_id = {o["id"]: o["text"] for o in q["options"]}
        return ", ".join(by_id.get(i, f"#{i}") for i in answer["option_ids"])
    if q["type"] == "metric":
        return f"{answer['number']:g}"
    return answer.get("text") or ""


def add_note(response_id: int, note: dict[str, Any]) -> None:
    """Record something a tester should see against the response, such as a skipped check."""
    notes = db.loads(db.val("SELECT notes FROM response WHERE id=?", response_id), [])
    if note not in notes:
        notes.append(note)
        db.run("UPDATE response SET notes=? WHERE id=?", db.dumps(notes), response_id)


def _context(response_id: int) -> Context:
    resp = response(response_id)
    return {"survey_no": resp["survey_no"], "response": resp, "answers": answers(response_id)}


# ── Logic (5.3) ─────────────────────────────────────────────────────────────

def _baseline_op(rule: dict[str, Any], answer: Optional[Answer]) -> bool:
    op, operand = rule["op"], (rule["operand"] or "")
    text = ((answer or {}).get("text") or "").strip().lower()
    needle = operand.strip().lower()
    picked = (answer or {}).get("option_ids") or []
    if op == "picked":
        return operand.isdigit() and int(operand) in picked
    if op == "not_picked":
        return operand.isdigit() and is_answered(answer) and int(operand) not in picked
    if op == "contains":
        return bool(needle) and needle in text
    if op == "not_contains":
        return bool(text) and needle not in text
    if op == "starts_with":
        return bool(needle) and text.startswith(needle)
    if op == "is_exactly":
        return text == needle and bool(text)
    if op == "answered":
        return is_answered(answer)
    if op == "not_answered":
        return not is_answered(answer)
    return False


def rule_met(rule: dict[str, Any], ctx: Context) -> bool:
    ext = LOGIC_OPS_EXT.get(rule["op"])
    if ext is not None:
        module_id, fn = ext
        if not modules.is_on(module_id):
            return False
        try:
            return bool(fn(rule, ctx))
        except Exception:
            # A module problem must never break the live survey: default path.
            logger.exception("Logic operator %s failed; taking the default path", rule["op"])
            return False
    return _baseline_op(rule, ctx["answers"].get(rule["source_qid"]))


def logic_rules(survey_no: int) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM logic_rule WHERE survey_no=? ORDER BY id", survey_no)


def _is_visible(q: dict[str, Any], show_rules: dict[str, list[dict]], ctx: Context) -> bool:
    """A question that a 'show' rule targets is hidden until one of its rules is met.
    A score-triggered follow-up is hidden until its metric score is in range."""
    targeted = q["qid"] in show_rules
    scored = bool(q["is_followup"] and q["parent_qid"]
                  and (q["followup_min"] is not None or q["followup_max"] is not None))
    if not targeted and not scored:
        return True
    if scored:
        number = (ctx["answers"].get(q["parent_qid"]) or {}).get("number")
        low = q["followup_min"] if q["followup_min"] is not None else float("-inf")
        high = q["followup_max"] if q["followup_max"] is not None else float("inf")
        if number is not None and low <= number <= high:
            return True
    return any(rule_met(rule, ctx) for rule in show_rules.get(q["qid"], []))


def visible_questions(survey_no: int, page_ord: int, ctx: Context) -> list[dict[str, Any]]:
    show_rules: dict[str, list[dict]] = {}
    for rule in logic_rules(survey_no):
        if rule["action"] == "show":
            show_rules.setdefault(rule["target"], []).append(rule)
    return [q for q in questions(survey_no) if q["page_ord"] == page_ord and _is_visible(q, show_rules, ctx)]


def _next_page(survey_no: int, page_ord: int, ctx: Context) -> Optional[int]:
    last = db.val("SELECT MAX(ord) FROM page WHERE survey_no=?", survey_no) or 0
    on_page = {q["qid"] for q in questions(survey_no) if q["page_ord"] == page_ord}
    candidate = page_ord + 1
    for rule in logic_rules(survey_no):
        # Forward jumps only, so a rule can never loop the survey.
        if rule["action"] == "jump" and rule["source_qid"] in on_page and str(rule["target"]).isdigit() \
                and int(rule["target"]) > page_ord and rule_met(rule, ctx):
            candidate = int(rule["target"])
            break
    while candidate <= last and not visible_questions(survey_no, candidate, ctx):
        candidate += 1
    return candidate if candidate <= last else None


# ── Participate (5.8) ───────────────────────────────────────────────────────

def start_response(survey_no: int, offline: bool = False, respondent: Optional[str] = None,
                   origin: str = "live") -> int:
    response_id = db.run(
        "INSERT INTO response(survey_no, started_at, offline, respondent, page_ord, origin) VALUES(?,?,?,?,?,?)",
        survey_no, db.now(), 1 if offline else 0, respondent or None, 0, origin)
    first = _next_page(survey_no, 0, _context(response_id))
    db.run("UPDATE response SET page_ord=? WHERE id=?", first, response_id)
    if first is None:
        submit(response_id)
    return response_id


def current_questions(response_id: int) -> list[dict[str, Any]]:
    ctx = _context(response_id)
    if ctx["response"]["page_ord"] is None:
        return []
    return visible_questions(ctx["survey_no"], ctx["response"]["page_ord"], ctx)


def missing_required(visible: list[dict[str, Any]], provided: dict[str, Answer]) -> dict[str, list[str]]:
    """qids left unanswered, by required mode."""
    missing: dict[str, list[str]] = {"mandatory": [], "encouraged": []}
    for q in visible:
        if q["required"] in missing and not is_answered(provided.get(q["qid"])):
            missing[q["required"]].append(q["qid"])
    return missing


def advance(response_id: int, provided: dict[str, Answer],
            defer: Optional[Callable[..., Any]] = None) -> Optional[int]:
    """The participant clicked Next: save the page, raise `page.next`, move on.
    Returns the next page number, or None when the response was submitted."""
    ctx = _context(response_id)
    resp, survey_no = ctx["response"], ctx["survey_no"]
    if resp["submitted_at"] or resp["page_ord"] is None:
        return None
    visible = visible_questions(survey_no, resp["page_ord"], ctx)
    for q in visible:
        answer = provided.get(q["qid"])
        db.run("DELETE FROM response_answer WHERE response_id=? AND qid=?", response_id, q["qid"])
        if is_answered(answer):
            db.run("INSERT INTO response_answer(response_id, qid, option_ids, text, number) VALUES(?,?,?,?,?)",
                   response_id, q["qid"], db.dumps(answer.get("option_ids") or []),
                   (answer.get("text") or "").strip() or None, answer.get("number"))
    shown = db.loads(resp["shown"], []) + [q["qid"] for q in visible]
    db.run("UPDATE response SET shown=? WHERE id=?", db.dumps(shown), response_id)

    ctx = _context(response_id)
    events.emit("page.next", response=ctx["response"], questions=visible, answers=ctx["answers"])
    next_ord = _next_page(survey_no, resp["page_ord"], ctx)
    db.run("UPDATE response SET page_ord=? WHERE id=?", next_ord, response_id)
    if next_ord is None:
        submit(response_id, defer)
    return next_ord


def submit(response_id: int, defer: Optional[Callable[..., Any]] = None) -> None:
    """Mark the response submitted, then run what happens at submit. The live page
    passes `defer` so the participant is not kept waiting on rules and alerts."""
    db.run("UPDATE response SET submitted_at=?, page_ord=NULL WHERE id=?", db.now(), response_id)
    if defer is not None:
        defer(_after_submit, response_id)
    else:
        _after_submit(response_id)


def _after_submit(response_id: int) -> None:
    run_alerts(response_id)
    ctx = _context(response_id)
    events.emit("response.submitted", response=ctx["response"], answers=ctx["answers"])


def to_answer(q: dict[str, Any], value: Any) -> Answer:
    """Build an answer from a plain value: option text(s), a number or a string."""
    if q["type"] in ("radio", "checkbox"):
        wanted = value if isinstance(value, list) else [value]
        ids = [o["id"] for o in q["options"] if o["text"] in wanted]
        return {"option_ids": ids[:1] if q["type"] == "radio" else ids, "text": None, "number": None}
    if q["type"] == "metric":
        return {"option_ids": [], "text": None, "number": float(value)}
    return {"option_ids": [], "text": str(value), "number": None}


def take(survey_no: int, values: dict[str, Any], offline: bool = False, origin: str = "scenario",
         respondent: Optional[str] = None) -> int:
    """Take a survey from start to submit through the same path as the live page.
    `values` maps question id to a plain value; questions that are never shown are skipped."""
    response_id = start_response(survey_no, offline=offline, respondent=respondent, origin=origin)
    for _ in range(100):
        visible = current_questions(response_id)
        if not visible:
            break
        provided = {q["qid"]: to_answer(q, values[q["qid"]]) for q in visible if q["qid"] in values}
        if advance(response_id, provided) is None:
            break
    return response_id


def shown_qids(response_id: int) -> list[str]:
    return db.loads(db.val("SELECT shown FROM response WHERE id=?", response_id), [])


# ── Rules & Alerts (5.6) ────────────────────────────────────────────────────

def alert_rules(survey_no: int) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM alert_rule WHERE survey_no=? ORDER BY id", survey_no)
    for rule in found:
        rule["conditions"] = db.loads(rule["conditions"], [])
        rule["actions"] = db.loads(rule["actions"], [])
    return found


def alert_rule(rule_id: int) -> Optional[dict[str, Any]]:
    rule = db.one("SELECT * FROM alert_rule WHERE id=?", rule_id)
    if rule:
        rule["conditions"] = db.loads(rule["conditions"], [])
        rule["actions"] = db.loads(rule["actions"], [])
    return rule


def save_alert_rule(rule: dict[str, Any]) -> None:
    db.run("UPDATE alert_rule SET name=?, conditions=?, actions=?, enabled=? WHERE id=?",
           rule["name"], db.dumps(rule["conditions"]), db.dumps(rule["actions"]),
           1 if rule["enabled"] else 0, rule["id"])


def _compare(left: Optional[float], op: str, right: float) -> bool:
    if left is None:
        return False
    return {">=": left >= right, "<=": left <= right, "=": left == right}.get(op, False)


def _baseline_condition(cond: dict[str, Any], ctx: Context) -> tuple[bool, str]:
    answer = ctx["answers"].get(cond.get("qid", ""))
    if cond["type"] == "picked":
        return int(cond["option_id"]) in ((answer or {}).get("option_ids") or []), ""
    if cond["type"] == "count":
        return _compare(float(len((answer or {}).get("option_ids") or [])), cond["op"], float(cond["value"])), ""
    if cond["type"] == "score":
        number = (answer or {}).get("number")
        return _compare(number, cond["op"], float(cond["value"])), (f"score {number:g}" if number is not None else "")
    return False, ""


def evaluate_alert_rule(rule: dict[str, Any], ctx: Context) -> tuple[bool, list[str]]:
    """Existing conditions first; a module's condition is only asked when they all hold."""
    details: list[str] = []
    baseline = [c for c in rule["conditions"] if c["type"] not in ALERT_CONDITIONS_EXT]
    extended = [c for c in rule["conditions"] if c["type"] in ALERT_CONDITIONS_EXT]
    if not rule["conditions"]:
        return False, details
    for cond in baseline:
        met, detail = _baseline_condition(cond, ctx)
        if not met:
            return False, details
        details.append(f"{cond.get('label', cond['type'])}" + (f" ({detail})" if detail else ""))
    for cond in extended:
        module_id, fn = ALERT_CONDITIONS_EXT[cond["type"]]
        if not modules.is_on(module_id):
            return False, details
        try:
            met, detail = fn(cond, ctx)
        except Exception:
            logger.exception("Alert condition %s failed; counted as not met", cond["type"])
            met, detail = False, "error"
        if not met:
            return False, details
        details.append(f"{cond.get('label', cond['type'])}" + (f" ({detail})" if detail else ""))
    return True, details


def run_alerts(response_id: int) -> list[int]:
    """Rules run when a response is submitted. Returns the ids of the rules that fired."""
    ctx = _context(response_id)
    proj = project(ctx["survey_no"])
    fired: list[int] = []
    for rule in alert_rules(ctx["survey_no"]):
        if not rule["enabled"]:
            continue
        met, details = evaluate_alert_rule(rule, ctx)
        if met:
            fired.append(rule["id"])
            _fire(rule, proj, ctx["response"], details)
    return fired


def _fire(rule: dict[str, Any], proj: dict[str, Any], resp: dict[str, Any], details: list[str]) -> None:
    """Write each action to its local sink. Nothing leaves the mock."""
    anonymous = bool(proj["anonymous"])
    # In an Anonymous project the alert says a concern was raised and leaves out who raised it.
    who = ("A concern was raised in an anonymous project. Respondent details are withheld." if anonymous
           else f"Respondent: {resp['respondent'] or 'not identified'} (response #{resp['id']})")
    matched = "; ".join(details)
    for action in rule["actions"]:
        base: dict[str, Any] = {"rule": rule["name"], "survey_no": proj["survey_no"], "project": proj["name"],
                                "matched": details}
        if not anonymous:
            base["response_id"] = resp["id"]
            base["respondent"] = resp["respondent"]
        if action["type"] == "email":
            payload = {"to": action.get("target") or "alerts@example.test",
                       "subject": f"[{proj['name']}] Alert: {rule['name']}",
                       "body": f"Rule \"{rule['name']}\" fired.\nMatched: {matched}\n{who}"}
        elif action["type"] == "webhook":
            payload = {"url": action.get("target") or "https://example.test/hook", "body": base}
        else:
            payload = {"object": action.get("target") or "Case",
                       "fields": {"Subject": f"{rule['name']} ({proj['name']})", "Description": f"{matched}\n{who}",
                                  "Origin": "Sogo-lite"}}
        db.run("INSERT INTO sink_log(sink, survey_no, rule_id, response_id, payload, created_at)"
               " VALUES(?,?,?,?,?,?)", action["type"], proj["survey_no"], rule["id"], resp["id"],
               db.dumps(payload), db.now())


# ── Tags (5.4) ──────────────────────────────────────────────────────────────

def tag_categories() -> list[dict[str, Any]]:
    categories = db.rows("SELECT * FROM tag_category ORDER BY id")
    for category in categories:
        category["tags"] = db.rows("SELECT * FROM tag WHERE category_id=? ORDER BY name", category["id"])
    return categories


def add_tag_category(name: str) -> Optional[str]:
    """Returns an error message, or None on success."""
    name = name.strip()
    if not name:
        return "Give the category a name."
    if db.val("SELECT COUNT(*) FROM tag_category") >= MAX_TAG_CATEGORIES:
        return f"An account can have at most {MAX_TAG_CATEGORIES} tag categories."
    if db.val("SELECT 1 FROM tag_category WHERE name=?", name):
        return "That category already exists."
    db.run("INSERT INTO tag_category(name) VALUES(?)", name)
    return None


def add_tags(category_id: int, names: list[str]) -> tuple[int, int]:
    """Returns (added, refused because of the per-category limit)."""
    added = refused = 0
    count = db.val("SELECT COUNT(*) FROM tag WHERE category_id=?", category_id)
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        if db.val("SELECT 1 FROM tag WHERE category_id=? AND name=?", category_id, name):
            continue
        if count >= MAX_TAGS_PER_CATEGORY:
            refused += 1
            continue
        db.run("INSERT INTO tag(category_id, name) VALUES(?,?)", category_id, name)
        added += 1
        count += 1
    return added, refused


def option_tags(survey_no: int) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for row in db.rows(
            "SELECT ot.option_id, ot.source, t.id, t.name, t.category_id FROM option_tag ot"
            " JOIN tag t ON t.id=ot.tag_id JOIN answer_option o ON o.id=ot.option_id"
            " JOIN question q ON q.id=o.question_id WHERE q.survey_no=?", survey_no):
        out.setdefault(row["option_id"], []).append(row)
    return out


def question_tags(survey_no: int) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for row in db.rows(
            "SELECT qt.question_id, qt.source, t.id, t.name, t.category_id FROM question_tag qt"
            " JOIN tag t ON t.id=qt.tag_id JOIN question q ON q.id=qt.question_id WHERE q.survey_no=?",
            survey_no):
        out.setdefault(row["question_id"], []).append(row)
    return out


def tag_option(option_id: int, tag_id: int, source: str = "manual") -> None:
    """One tag per answer option within a category."""
    category_id = db.val("SELECT category_id FROM tag WHERE id=?", tag_id)
    db.run("DELETE FROM option_tag WHERE option_id=? AND tag_id IN (SELECT id FROM tag WHERE category_id=?)",
           option_id, category_id)
    db.run("INSERT INTO option_tag(option_id, tag_id, source) VALUES(?,?,?)", option_id, tag_id, source)


def tag_question(question_id: int, tag_id: int, source: str = "manual") -> None:
    db.run("INSERT OR IGNORE INTO question_tag(question_id, tag_id, source) VALUES(?,?,?)",
           question_id, tag_id, source)


def automap(survey_no: int) -> int:
    """Auto-map: an answer option gets a tag only when its text exactly matches the tag name."""
    tagged = option_tags(survey_no)
    mapped = 0
    for q in questions(survey_no):
        for option in q["options"]:
            have = {t["category_id"] for t in tagged.get(option["id"], [])}
            for tag in db.rows("SELECT * FROM tag WHERE lower(name)=lower(?)", option["text"].strip()):
                if tag["category_id"] not in have:
                    tag_option(option["id"], tag["id"], "auto-map")
                    have.add(tag["category_id"])
                    mapped += 1
    return mapped


# ── Assign Scores (5.5) ─────────────────────────────────────────────────────

def auto_score(survey_no: int, response_answers: dict[str, Answer]) -> tuple[float, float]:
    """(points earned, points available) over fixed-option questions. Open-ended
    questions are not scored automatically."""
    earned = available = 0.0
    for q in questions(survey_no):
        if q["type"] not in ("radio", "checkbox") or not q["options"]:
            continue
        points = [o["points"] for o in q["options"]]
        available += max(points) if q["type"] == "radio" else sum(p for p in points if p > 0)
        picked = (response_answers.get(q["qid"]) or {}).get("option_ids") or []
        earned += sum(o["points"] for o in q["options"] if o["id"] in picked)
    return earned, available


def post_populated(response_id: int) -> dict[str, dict[str, Any]]:
    out = {}
    for row in db.rows("SELECT * FROM post_populate WHERE response_id=?", response_id):
        row["detail"] = db.loads(row["detail"], {})
        out[row["qid"]] = row
    return out


def save_post_populate(response_id: int, qid: str, score: Optional[float], feedback: str,
                       status: str = "confirmed", source: str = "grader",
                       detail: Optional[dict[str, Any]] = None) -> None:
    existing = db.one("SELECT detail FROM post_populate WHERE response_id=? AND qid=?", response_id, qid)
    kept = detail if detail is not None else db.loads(existing["detail"], {}) if existing else {}
    db.run("INSERT INTO post_populate(response_id, qid, score, feedback, status, source, detail)"
           " VALUES(?,?,?,?,?,?,?) ON CONFLICT(response_id, qid) DO UPDATE SET score=excluded.score,"
           " feedback=excluded.feedback, status=excluded.status, source=excluded.source, detail=excluded.detail",
           response_id, qid, score, feedback, status, source, db.dumps(kept))
