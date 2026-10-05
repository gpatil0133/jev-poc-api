"""Compile template + binding + inline questions into one Laya `questions` dict.

Rules enforced here (see README "Prompt setup"):
  - every choice gets an escape option
  - yes/no and multi-label compile to a two-option choice with neutral keys
    (laya's `noul` can follow its true/false labels instead of the text)
  - author input is a name + one-line description; instructions are generated
  - the option budget is checked before anything is sent or saved
  - compiled questions are hashed; the hash is the cache key
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.schemas import Binding, BudgetLine, BudgetReport, QuestionSpec
from app.registry.loader import DEFAULT_STATE_FIELDS, TaskRegistry, TaskTemplate

# Neutral option keys for yes/no questions.
YES_KEY = "A"
NO_KEY = "B"

ESCAPE_KEY = "other"
ESCAPE_DESCRIPTION = "none of the above, or not a real answer"
ESCAPE_KEYS = {"other", "none", "none of these", "none_of_these", "none of the above", "unknown", "n/a"}

# laya-serve limits (laya/serve.py, laya/common.py build_head).
HEAD_BUDGET = {"english": 192, "multilingual": 256, "auto": 192}
OPTION_TOKEN_CAP = 48
MIN_INSTRUCTION_TOKENS = 16
MAX_WIRE_QUESTIONS = 64
MAX_CHOICE_OPTIONS = 100
MAX_SCORE_LEVELS = 32
SHORTLIST_ADVICE_OPTIONS = 15
ROTATION_SEP = "@r"

DEFAULT_CHOICE_INSTRUCTIONS = "What is this survey answer mainly about?"


class CompileError(ValueError):
    """The question set cannot be compiled. `errors` is a list of messages safe to show an author."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class CompiledQuestion:
    qid: str
    kind: str                           # choice | yesno | score
    wire: dict[str, Any]                # the Laya question, canonical option order
    thresholds: dict[str, float] = field(default_factory=dict)
    rotations: int = 1
    escape: Optional[str] = None        # label that means "none of these" (choice only)
    qhash: str = ""

    @property
    def n_options(self) -> int:
        return len(self.wire["criteria"])


@dataclass
class CompiledSet:
    questions: list[CompiledQuestion]
    state_fields: list[str]
    model: str
    set_hash: str
    warnings: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, CompiledQuestion]:
        return {q.qid: q for q in self.questions}

    def wire_questions(self, qids: Optional[set[str]] = None, rotate: bool = False) -> dict[str, dict]:
        """The `questions` dict for one Laya request. With rotate=True each question is
        sent under several option orders (`qid@r1`, ...) that share the forward pass."""
        out: dict[str, dict] = {}
        for q in self.questions:
            if qids is not None and q.qid not in qids:
                continue
            k = q.n_options
            passes = min(q.rotations, k) if rotate else 1
            out[q.qid] = q.wire
            for r in range(1, passes):
                out[f"{q.qid}{ROTATION_SEP}{r}"] = dict(q.wire, option_order=[(i + r) % k for i in range(k)])
        return out

    def budget_report(self) -> BudgetReport:
        budget = HEAD_BUDGET.get(self.model, HEAD_BUDGET["auto"])
        lines = [_budget_line(q, budget) for q in self.questions]
        return BudgetReport(
            model=self.model,
            ok=all(line.status != "over" for line in lines),
            wire_questions=len(self.wire_questions(rotate=True)),
            lines=lines,
            warnings=list(self.warnings),
        )


# ── token estimate ──────────────────────────────────────────────────────────

_PIECE = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """Rough subword count. The real tokenizer lives with the checkpoint on the GPU
    box; this over-counts slightly so a budget that passes here passes there."""
    total = 0
    for piece in _PIECE.findall(text):
        total += 1 if len(piece) <= 7 else math.ceil(len(piece) / 5)
    return math.ceil(total * 1.1)


def _option_texts(q: CompiledQuestion) -> list[str]:
    criteria = q.wire["criteria"]
    if q.wire["type"] == "score":
        return [f"level {i}: {c}" for i, c in enumerate(criteria)]
    return [f"{k}: {v}" if v else str(k) for k, v in criteria.items()]


def _budget_line(q: CompiledQuestion, budget: int) -> BudgetLine:
    # Mirrors laya build_head: each option costs one [MASK] plus its text capped at 48 tokens;
    # when fewer than 16 tokens are left for the instructions every option is cut to fit.
    raw = [estimate_tokens(text) for text in _option_texts(q)]
    option_tokens = sum(1 + min(OPTION_TOKEN_CAP, n) for n in raw)
    instruction_tokens = estimate_tokens(f"{q.wire['type']} question: {q.wire['instructions']}")
    head_tokens = option_tokens + instruction_tokens + 3
    status, detail = "ok", ""
    if budget - option_tokens < MIN_INSTRUCTION_TOKENS:
        per = max(4, (budget - MIN_INSTRUCTION_TOKENS) // max(1, len(raw)))
        status = "over"
        detail = (f"options need ~{option_tokens} tokens of {budget}; each would be cut to ~{per} tokens. "
                  "Shorten the descriptions or use fewer options.")
    elif instruction_tokens > budget - option_tokens:
        status = "warn"
        detail = "instructions would be truncated to make room for the options"
    elif any(n > OPTION_TOKEN_CAP for n in raw):
        status = "warn"
        detail = f"an option description is longer than {OPTION_TOKEN_CAP} tokens and would be cut"
    elif q.n_options > SHORTLIST_ADVICE_OPTIONS:
        status = "warn"
        detail = f"more than {SHORTLIST_ADVICE_OPTIONS} options: shortlist first"
    return BudgetLine(id=q.qid, options=q.n_options, option_tokens=option_tokens,
                      instruction_tokens=instruction_tokens, head_tokens=head_tokens,
                      budget=budget, status=status, detail=detail)


# ── building questions ──────────────────────────────────────────────────────

def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _find_escape(labels: list[str]) -> Optional[str]:
    for label in labels:
        if label.strip().lower() in ESCAPE_KEYS:
            return label
    return None


def _choice(qid: str, instructions: str, classes: dict[str, str]) -> CompiledQuestion:
    criteria = {str(k): str(v or "") for k, v in classes.items()}
    escape = _find_escape(list(criteria))
    if escape is None:
        # Never force the model into a wrong class.
        criteria[ESCAPE_KEY] = ESCAPE_DESCRIPTION
        escape = ESCAPE_KEY
    wire = {"type": "choice", "instructions": instructions, "criteria": criteria}
    return CompiledQuestion(qid=qid, kind="choice", wire=wire, escape=escape)


def _yesno(qid: str, instructions: str, true_desc: str, false_desc: str) -> CompiledQuestion:
    wire = {"type": "choice", "instructions": instructions,
            "criteria": {YES_KEY: true_desc, NO_KEY: false_desc}}
    return CompiledQuestion(qid=qid, kind="yesno", wire=wire)


def _score(qid: str, instructions: str, levels: list[str]) -> CompiledQuestion:
    wire = {"type": "score", "instructions": instructions, "criteria": [str(lv) for lv in levels]}
    return CompiledQuestion(qid=qid, kind="score", wire=wire)


def _from_template(t: TaskTemplate) -> CompiledQuestion:
    if t.type == "choice":
        q = _choice(t.id, t.instructions, t.criteria)
    elif t.type == "yesno":
        q = _yesno(t.id, t.instructions, t.criteria["true"], t.criteria["false"])
    else:
        q = _score(t.id, t.instructions, t.criteria)
    q.thresholds = dict(t.thresholds)
    q.rotations = t.rotations
    return q


def _label_slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", label.strip()).strip("_") or "label"


def _from_spec(spec: QuestionSpec, errors: list[str]) -> list[CompiledQuestion]:
    """Expand an author/inline spec. A bare name + description is all the author writes."""
    classes = spec.classes or {}
    built: list[CompiledQuestion] = []
    if spec.type == "choice":
        if len(classes) < 2:
            errors.append(f"{spec.id}: a choice needs at least two classes")
            return []
        built = [_choice(spec.id, spec.instructions or DEFAULT_CHOICE_INSTRUCTIONS, classes)]
    elif spec.type == "labels":
        if not classes:
            errors.append(f"{spec.id}: labels needs at least one entry in classes")
            return []
        for label, desc in classes.items():
            about = f"{label}: {desc}" if desc else label
            built.append(_yesno(
                f"{spec.id}.{_label_slug(label)}",
                spec.instructions or f"Is this survey answer about {label}?",
                f"the answer is about {about}",
                f"the answer is not about {label}",
            ))
    elif spec.type == "yesno":
        if set(classes) != {"true", "false"} or not spec.instructions:
            errors.append(f"{spec.id}: yesno needs instructions and classes with exactly 'true' and 'false'")
            return []
        built = [_yesno(spec.id, spec.instructions, classes["true"], classes["false"])]
    else:
        if not spec.levels or len(spec.levels) < 2 or not spec.instructions:
            errors.append(f"{spec.id}: score needs instructions and at least two levels")
            return []
        built = [_score(spec.id, spec.instructions, spec.levels)]
    for q in built:
        if spec.thresholds:
            q.thresholds = spec.thresholds.model_dump(exclude_none=True)
        if spec.rotations:
            q.rotations = spec.rotations
    return built


def _resolve_model(explicit: Optional[str], templates: list[TaskTemplate], default_model: str) -> str:
    if explicit:
        return explicit
    wanted = {t.model for t in templates if t.model != "auto"}
    if len(wanted) == 1:
        return wanted.pop()
    # Mixed English/multilingual templates share one forward pass, so the wider checkpoint wins.
    if "multilingual" in wanted:
        return "multilingual"
    return default_model


def compile_set(
    registry: TaskRegistry,
    tasks: Optional[list[str]] = None,
    binding: Optional[Binding] = None,
    inline: Optional[list[QuestionSpec]] = None,
    model: Optional[str] = None,
    default_model: str = "auto",
    rotations: Optional[int] = None,
) -> CompiledSet:
    """Merge the three layers into one question set. Most specific wins on an id clash:
    template < binding.custom < inline."""
    errors: list[str] = []
    warnings: list[str] = []
    compiled: dict[str, CompiledQuestion] = {}
    templates: list[TaskTemplate] = []

    task_ids: list[str] = []
    for task_id in (binding.tasks if binding else []) + list(tasks or []):
        if task_id not in task_ids:
            task_ids.append(task_id)
    for task_id in task_ids:
        template = registry.get(task_id)
        if template is None:
            errors.append(f"unknown task {task_id!r}")
            continue
        templates.append(template)
        compiled[template.id] = _from_template(template)

    if binding:
        for task_id, override in binding.thresholds.items():
            if task_id in compiled:
                compiled[task_id].thresholds.update(override.model_dump(exclude_none=True))
            else:
                warnings.append(f"threshold override for {task_id!r} matches no task on this binding")

    for spec in (binding.custom if binding else []) + list(inline or []):
        # A spec replaces anything it expands over, including an earlier `labels` group.
        for stale in [qid for qid in compiled if qid == spec.id or qid.startswith(spec.id + ".")]:
            del compiled[stale]
        for q in _from_spec(spec, errors):
            compiled[q.qid] = q

    if not compiled and not errors:
        errors.append("no questions: give tasks, a stored binding (survey_no + question_id) or inline questions")

    resolved_model = _resolve_model(model or (binding.model if binding else None), templates, default_model)
    for q in compiled.values():
        if rotations:
            q.rotations = rotations
        if q.kind == "choice" and q.n_options > MAX_CHOICE_OPTIONS:
            errors.append(f"{q.qid}: {q.n_options} options is over the limit of {MAX_CHOICE_OPTIONS}")
        if q.kind == "score" and q.n_options > MAX_SCORE_LEVELS:
            errors.append(f"{q.qid}: {q.n_options} levels is over the limit of {MAX_SCORE_LEVELS}")
        q.qhash = _hash(q.wire)

    state_fields: list[str] = []
    for template in templates:
        for name in template.state:
            if name not in state_fields:
                state_fields.append(name)
    if not state_fields:
        state_fields = list(DEFAULT_STATE_FIELDS)

    questions = list(compiled.values())
    cset = CompiledSet(
        questions=questions,
        state_fields=state_fields,
        model=resolved_model,
        set_hash=_hash([resolved_model, sorted(q.qhash for q in questions)]),
        warnings=warnings,
    )
    wire_count = len(cset.wire_questions(rotate=True))
    if wire_count > MAX_WIRE_QUESTIONS:
        errors.append(f"{wire_count} questions (with rotations) is over the limit of {MAX_WIRE_QUESTIONS} per call")
    if errors:
        raise CompileError(errors)
    return cset


def build_state(fields: list[str], values: dict[str, Any]) -> dict[str, Any]:
    """Render the state. Context travels with the answer: 'ok' only means something
    next to the question that was asked."""
    state = {name: values[name] for name in fields if values.get(name) not in (None, "")}
    if not state:
        state = {"answer": values.get("answer") or ""}
    return state


def state_hash(state: dict[str, Any]) -> str:
    return _hash(state)
