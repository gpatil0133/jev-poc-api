"""Scope 1: shared classes for branching and alerts (plan section 8.1).

Classes are defined once on a text question and saved to the gateway as a binding.
An answer is classified once per response; Logic and Rules & Alerts both read the
stored result for as long as the class set (its `question_hash`) is unchanged.

The class set and the classify-once store are also what scopes 2 and 3 run on, so
they stay in use when this module is off. Off means: no Classes tab, and rules read
only the classes a scope 2 or 3 rule editor added (see `readable`).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from sogo_lite import db, engine, gateway, modules

MODULE = "shared_classes"
CATALOG_SETTING = "catalog"
CATALOG_RETRY_S = 30.0
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
SPEC_TYPES = {"choice": "choice (pick one)", "labels": "labels (any that apply)", "yesno": "yes / no"}
# The ready-made tasks this scope is about: classes shared by a branch and an alert.
SHARED_TASKS = ("comment.sentiment", "comment.response_type")
# Answer ids a scope 2 or 3 rule editor adds: catalog meanings, "means_…" and "about_…".
RULE_EDITOR_PREFIXES = ("meaning.", "comment.", "means_", "about_")

_catalog_failed_at = 0.0


@dataclass
class Classification:
    answers: Optional[dict[str, dict[str, Any]]]
    reason: Optional[str] = None      # why there is no result
    log_id: Optional[int] = None      # the gateway call that produced the stored result
    reused: bool = False


# ── catalog ─────────────────────────────────────────────────────────────────

def catalog(refresh: bool = False) -> list[dict[str, Any]]:
    """Ready-made tasks from GET /v1/tasks. Cached locally so the editors render
    without a round trip; the cache holds task definitions only."""
    global _catalog_failed_at
    cached = db.loads(db.get_setting(CATALOG_SETTING), None)
    if cached is not None and not refresh:
        return cached
    if not refresh and time.time() - _catalog_failed_at < CATALOG_RETRY_S:
        return cached or []
    result = gateway.call("GET", "/v1/tasks", module=MODULE, trigger="task catalog", kind="author")
    if result.fallback:
        _catalog_failed_at = time.time()
        gateway.set_outcome(result.log_id, "catalog unavailable; picker shows the last known list")
        return cached or []
    tasks = result.data.get("tasks") or []
    db.set_setting(CATALOG_SETTING, db.dumps(tasks))
    gateway.set_outcome(result.log_id, f"catalog loaded: {len(tasks)} tasks")
    return tasks


def comment_tasks() -> list[dict[str, Any]]:
    """Catalog tasks that read a text answer (the column-mapping task does not)."""
    return [t for t in catalog() if "answer" in (t.get("state") or [])]


# ── class sets ──────────────────────────────────────────────────────────────

def get(survey_no: int, qid: str) -> Optional[dict[str, Any]]:
    cs = db.one("SELECT * FROM class_set WHERE survey_no=? AND qid=?", survey_no, qid)
    if cs:
        cs["tasks"] = db.loads(cs["tasks"], [])
        cs["custom"] = db.loads(cs["custom"], [])
        cs["budget"] = db.loads(cs["budget"], None)
    return cs


def usable(cs: Optional[dict[str, Any]]) -> bool:
    """Only a class set the gateway has accepted can be read by rules."""
    return bool(cs and not cs["dirty"] and cs["question_hash"])


def _thresholds(cs: dict[str, Any]) -> dict[str, float]:
    return {k: cs[k] for k in ("act", "suggest") if cs.get(k) is not None}


def binding_body(cs: dict[str, Any], wording: str) -> dict[str, Any]:
    """The PUT /v1/bindings body. The class set's thresholds go on every custom
    question and, keyed by task id, on every catalog task."""
    thresholds = _thresholds(cs)
    custom = [dict(spec, thresholds=thresholds) if thresholds else dict(spec) for spec in cs["custom"]]
    body: dict[str, Any] = {
        "survey_no": cs["survey_no"], "question_id": cs["qid"], "survey_question": wording[:1000],
        "tasks": list(cs["tasks"]), "custom": custom,
        "thresholds": {task: thresholds for task in cs["tasks"]} if thresholds else {},
    }
    if cs.get("model"):
        body["model"] = cs["model"]
    return body


def check_budget(survey_no: int, tasks: list[str], custom: list[dict[str, Any]],
                 model: Optional[str]) -> gateway.GatewayResult:
    """Dry run through POST /v1/tasks/compile. Nothing is sent to the model."""
    body: dict[str, Any] = {"tasks": tasks, "questions": custom}
    if model:
        body["model"] = model
    result = gateway.call("POST", "/v1/tasks/compile", body, module=MODULE, trigger="Check budget",
                          kind="author", survey_no=survey_no)
    if not result.fallback:
        lines = (result.data.get("budget") or {}).get("lines") or []
        worst = "over" if any(l["status"] == "over" for l in lines) else \
            "warn" if any(l["status"] == "warn" for l in lines) else "ok"
        gateway.set_outcome(result.log_id, f"budget {worst}")
    else:
        gateway.set_outcome(result.log_id, "budget check failed")
    return result


def save(survey_no: int, qid: str, tasks: list[str], custom: list[dict[str, Any]],
         act: Optional[float], suggest: Optional[float], model: Optional[str]) -> tuple[bool, str]:
    db.run("INSERT INTO class_set(survey_no, qid, tasks, custom, act, suggest, model, dirty)"
           " VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(survey_no, qid) DO UPDATE SET tasks=excluded.tasks,"
           " custom=excluded.custom, act=excluded.act, suggest=excluded.suggest, model=excluded.model, dirty=1",
           survey_no, qid, db.dumps(tasks), db.dumps(custom), act, suggest, model or None)
    return sync(survey_no, qid)


def sync(survey_no: int, qid: str, trigger: str = "class set saved") -> tuple[bool, str]:
    """Save the class set to the gateway. On failure it stays an unsynced local
    draft and cannot be used in rules."""
    cs = get(survey_no, qid)
    question = engine.question_by_qid(survey_no, qid)
    if not cs or not question:
        return False, "No class set to save."
    if not cs["tasks"] and not cs["custom"]:
        db.run("UPDATE class_set SET dirty=1, question_hash=NULL, sync_error=? WHERE survey_no=? AND qid=?",
               "The class set is empty.", survey_no, qid)
        return False, "The class set is empty, so nothing was saved to the gateway."
    result = gateway.call("PUT", f"/v1/bindings/{survey_no}/{qid}", binding_body(cs, question["wording"]),
                          module=MODULE, trigger=trigger, kind="author", survey_no=survey_no)
    if result.fallback:
        errors = result.detail.get("errors") or []
        message = "; ".join(errors) or result.message
        budget = result.detail.get("budget")
        db.run("UPDATE class_set SET dirty=1, sync_error=?, budget=COALESCE(?, budget) WHERE survey_no=? AND qid=?",
               message, db.dumps(budget) if budget else None, survey_no, qid)
        gateway.set_outcome(result.log_id, "binding not saved; kept as an unsynced local draft")
        return False, f"Not saved to the gateway ({message}). Kept as a local draft; rules cannot use it yet."
    db.run("UPDATE class_set SET dirty=0, sync_error=NULL, question_hash=?, budget=?, synced_at=?"
           " WHERE survey_no=? AND qid=?", result.data.get("question_hash"),
           db.dumps(result.data.get("budget")), db.now(), survey_no, qid)
    gateway.set_outcome(result.log_id, "binding saved")
    return True, "Class set saved to the gateway."


def _strip(value: Any) -> Any:
    """Drop nulls and empty containers so a local body compares with what the gateway returns."""
    if isinstance(value, dict):
        cleaned = {k: _strip(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, {}, [])}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def verify(survey_no: int, qid: str) -> tuple[bool, str]:
    """Read the binding back with GET /v1/bindings and compare it with the local copy."""
    cs = get(survey_no, qid)
    question = engine.question_by_qid(survey_no, qid)
    if not cs or not question:
        return False, "No class set to check."
    result = gateway.call("GET", f"/v1/bindings/{survey_no}/{qid}", module=MODULE,
                          trigger="Check sync", kind="author", survey_no=survey_no)
    if result.fallback:
        gateway.set_outcome(result.log_id, "binding could not be read")
        return False, f"Could not read the binding back ({result.message})."
    same = _strip(result.data) == _strip(binding_body(cs, question["wording"]))
    gateway.set_outcome(result.log_id, "local copy matches" if same else "local copy differs")
    return same, ("The gateway's binding matches the local copy." if same
                  else "The gateway's binding differs from the local copy. Save the class set to re-sync.")


# ── adding classes from the rule editors ────────────────────────────────────

def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:limit] or "x"


def label_slug(label: str) -> str:
    """How the gateway names a `labels` answer: `{id}.{slug}` (app/registry/compiler.py)."""
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", label.strip()).strip("_") or "label"


def _ensure(survey_no: int, qid: str) -> dict[str, Any]:
    cs = get(survey_no, qid)
    if cs is None:
        db.run("INSERT INTO class_set(survey_no, qid) VALUES(?,?)", survey_no, qid)
        cs = get(survey_no, qid)
    return cs


def ensure_task(survey_no: int, qid: str, task_id: str) -> tuple[bool, str]:
    """Add a ready-made meaning to the question's class set, so there is still one binding per question."""
    cs = _ensure(survey_no, qid)
    if task_id in cs["tasks"] and usable(cs):
        return True, ""
    tasks = cs["tasks"] if task_id in cs["tasks"] else cs["tasks"] + [task_id]
    return save(survey_no, qid, tasks, cs["custom"], cs["act"], cs["suggest"], cs["model"])


def ensure_yesno(survey_no: int, qid: str, spec_id: str, instructions: str, yes: str,
                 no: str) -> tuple[bool, str]:
    """Add an author-written meaning or topic to the class set as one yes/no question."""
    cs = _ensure(survey_no, qid)
    spec = {"id": spec_id, "type": "yesno", "instructions": instructions[:400],
            "classes": {"true": yes, "false": no}}
    if spec in cs["custom"] and usable(cs):
        return True, ""
    custom = [s for s in cs["custom"] if s["id"] != spec_id] + [spec]
    return save(survey_no, qid, cs["tasks"], custom, cs["act"], cs["suggest"], cs["model"])


def readable(ref: str) -> bool:
    """Can a rule read this class? Always when the module is on. When it is off, only
    the classes the rule editors of scopes 2 and 3 added for themselves."""
    if modules.is_on(MODULE):
        return True
    answer_id = ref.partition("|")[0]
    return answer_id not in SHARED_TASKS and answer_id.startswith(RULE_EDITOR_PREFIXES)


def class_refs(cs: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    """Every (answer id, label) a rule can read from a class set, as `answer_id|label`."""
    return [r for r in _all_refs(cs) if readable(r["ref"])]


def _all_refs(cs: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    if not cs:
        return []
    refs: list[dict[str, str]] = []
    by_id = {t["id"]: t for t in catalog()}
    for task_id in cs["tasks"]:
        task = by_id.get(task_id)
        if task is None or task["type"] == "yesno":
            refs.append({"ref": f"{task_id}|yes", "text": f"{task_id}: {(task or {}).get('description', 'yes')}"})
        elif task["type"] == "choice":
            refs += [{"ref": f"{task_id}|{label}", "text": f"{task_id} is {label}"} for label in task["criteria"]]
    for spec in cs["custom"]:
        classes = spec.get("classes") or {}
        if spec["type"] == "yesno":
            refs.append({"ref": f"{spec['id']}|yes", "text": f"{spec['id']}: {classes.get('true', 'yes')}"})
        elif spec["type"] == "labels":
            refs += [{"ref": f"{spec['id']}.{label_slug(label)}|yes", "text": f"{spec['id']} includes {label}"}
                     for label in classes]
        elif spec["type"] == "choice":
            refs += [{"ref": f"{spec['id']}|{label}", "text": f"{spec['id']} is {label}"} for label in classes]
    return refs


# ── classify once, read many times ──────────────────────────────────────────

def stored(response_id: int, survey_no: int, qid: str) -> Optional[dict[str, Any]]:
    """The stored result row, only while the class set is unchanged. Makes no gateway call."""
    cs = get(survey_no, qid)
    row = db.one("SELECT * FROM classification_result WHERE response_id=? AND qid=?", response_id, qid)
    if not row or not usable(cs) or row["question_hash"] != cs["question_hash"]:
        return None
    row["answers"] = db.loads(row["answers"], {})
    return row


def classify_answer(resp: dict[str, Any], qid: str, text: str, *, kind: str, trigger: str,
                    module: str) -> Classification:
    """Return the stored classification of this answer, or classify it now."""
    survey_no = resp["survey_no"]
    cs = get(survey_no, qid)
    if not usable(cs):
        return Classification(None, "class set is not saved to the gateway")
    # Ids only: this runs on the live page, so it must not go and fetch the catalog.
    if not any(readable(answer_id) for answer_id in cs["tasks"] + [s["id"] for s in cs["custom"]]):
        return Classification(None, "shared classes module is off")
    if resp["offline"]:
        return Classification(None, "offline mode")
    if not (text or "").strip():
        return Classification(None, "no answer")
    row = stored(resp["id"], survey_no, qid)
    if row is not None:
        return Classification(row["answers"], log_id=row["log_id"], reused=True)

    question = engine.question_by_qid(survey_no, qid)
    body: dict[str, Any] = {"text": text, "survey_no": survey_no, "question_id": qid,
                            "survey_question": question["wording"][:1000]}
    if question["parent_qid"]:
        # The Text Box follows a metric question: the score travels with the comment.
        score = (engine.answers(resp["id"]).get(question["parent_qid"]) or {}).get("number")
        if score is not None:
            body["metric_score"] = score
    result = gateway.call("POST", "/v1/classify", body, module=module, trigger=trigger, kind=kind,
                          survey_no=survey_no, response_id=resp["id"])
    if result.fallback:
        gateway.set_outcome(result.log_id, "no result; readers run their fallback")
        return Classification(None, result.reason, log_id=result.log_id)
    db.run("INSERT INTO classification_result(response_id, qid, question_hash, answers, log_id, created_at)"
           " VALUES(?,?,?,?,?,?) ON CONFLICT(response_id, qid) DO UPDATE SET question_hash=excluded.question_hash,"
           " answers=excluded.answers, log_id=excluded.log_id, created_at=excluded.created_at",
           resp["id"], qid, result.data.get("question_hash") or "", db.dumps(result.answers),
           result.log_id, db.now())
    return Classification(result.answers, log_id=result.log_id)


def meets(answers: dict[str, dict[str, Any]], ref: str, min_prob: Optional[float]) -> tuple[bool, float]:
    """Does a stored classification satisfy a rule? A rule with its own minimum
    probability compares against the label's probability, which is how an alert can be
    stricter than a branch on one classification. Without one, the band decides."""
    answer_id, _, label = ref.partition("|")
    answer = answers.get(answer_id)
    if not answer or not readable(ref):
        return False, 0.0
    probability = float((answer.get("probabilities") or {}).get(label, 0.0))
    if answer.get("_forced"):
        # Forced-low-confidence fault: the model is to be treated as unsure.
        return False, probability
    if min_prob is not None:
        return probability >= min_prob, probability
    return answer.get("label") == label and answer.get("band") == "act", probability


def results_for(response_id: int) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM classification_result WHERE response_id=? ORDER BY qid", response_id)
    for row in found:
        row["answers"] = db.loads(row["answers"], {})
    return found


def parse_custom_form(form: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Read the class editor's rows: an id, a type, and `label: description` lines."""
    specs: list[dict[str, Any]] = []
    errors: list[str] = []
    index = 0
    while f"c_id_{index}" in form:
        spec_id = (form.get(f"c_id_{index}") or "").strip()
        stype = form.get(f"c_type_{index}") or "choice"
        instructions = (form.get(f"c_instr_{index}") or "").strip()
        lines = form.get(f"c_lines_{index}") or ""
        index += 1
        if not spec_id:
            continue
        if not ID_PATTERN.match(spec_id):
            errors.append(f"{spec_id}: use letters, digits, _ . - only (up to 64 characters)")
            continue
        classes: dict[str, str] = {}
        for line in lines.splitlines():
            label, _, description = line.partition(":")
            if label.strip():
                classes[label.strip()] = description.strip()
        spec: dict[str, Any] = {"id": spec_id, "type": stype if stype in SPEC_TYPES else "choice",
                                "classes": classes}
        if instructions:
            spec["instructions"] = instructions[:400]
        specs.append(spec)
    return specs, errors
