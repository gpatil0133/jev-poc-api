"""Pydantic v2 request/response models for the gateway."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"

Band = Literal["act", "suggest", "none"]
ModelName = Literal["auto", "english", "multilingual"]


class Thresholds(BaseModel):
    act: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    suggest: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class QuestionSpec(BaseModel):
    """An author-defined or inline question. The author gives names and one-line
    descriptions; the compiler writes the instructions when they are omitted.

    type:
      choice  pick one of `classes` ({label: description}); an escape option is added
      labels  multi-label: one yes/no per entry of `classes`, ids become `{id}.{label}`
      yesno   `classes` is {"true": "...", "false": "..."}
      score   `levels` is an ordered list of level descriptions
    """
    id: str = Field(pattern=ID_PATTERN)
    type: Literal["choice", "labels", "yesno", "score"] = "choice"
    instructions: Optional[str] = Field(default=None, max_length=400)
    classes: Optional[dict[str, str]] = None
    levels: Optional[list[str]] = None
    thresholds: Optional[Thresholds] = None
    rotations: Optional[int] = Field(default=None, ge=1, le=8)


class Binding(BaseModel):
    """Layer 2: what runs on one survey question."""
    survey_no: int
    question_id: str = Field(pattern=ID_PATTERN)
    # Question text as the respondent sees it; rendered into the state.
    survey_question: Optional[str] = Field(default=None, max_length=1000)
    tasks: list[str] = Field(default_factory=list)
    custom: list[QuestionSpec] = Field(default_factory=list)
    # Per-task threshold overrides, keyed by task id.
    thresholds: dict[str, Thresholds] = Field(default_factory=dict)
    model: Optional[ModelName] = None


class BudgetLine(BaseModel):
    id: str
    options: int
    option_tokens: int
    instruction_tokens: int
    head_tokens: int
    budget: int
    status: Literal["ok", "warn", "over"]
    detail: str = ""


class BudgetReport(BaseModel):
    model: str
    estimated: bool = True
    ok: bool
    wire_questions: int
    lines: list[BudgetLine]
    warnings: list[str] = Field(default_factory=list)


class BindingSaved(BaseModel):
    binding: Binding
    question_hash: str
    budget: BudgetReport


# ── classify ────────────────────────────────────────────────────────────────

class QuestionSelector(BaseModel):
    """How a call names its questions. Layers merge, most specific wins:
    templates (`tasks`) < stored binding (`survey_no` + `question_id`) < inline `questions`."""
    survey_no: Optional[int] = None
    question_id: Optional[str] = Field(default=None, pattern=ID_PATTERN)
    tasks: list[str] = Field(default_factory=list)
    questions: list[QuestionSpec] = Field(default_factory=list)
    model: Optional[ModelName] = None
    # Consumer-side thresholds: applied to every answer of this call, over the
    # template/binding ones. The stored result is the same either way.
    thresholds: Optional[Thresholds] = None


class TextItem(BaseModel):
    text: str = Field(max_length=20000)
    survey_question: Optional[str] = Field(default=None, max_length=1000)
    metric_score: Optional[float] = None
    # Extra state fields a template names in `state:` (e.g. column_name).
    context: dict[str, Any] = Field(default_factory=dict)


class ClassifyRequest(QuestionSelector, TextItem):
    timeout_ms: Optional[int] = Field(default=None, ge=20, le=30000)


class Answer(BaseModel):
    label: Optional[str] = None
    confidence: float = 0.0
    probabilities: dict[str, float] = Field(default_factory=dict)
    # Set for yes/no questions (P(yes)) and score questions (expected level).
    value: Optional[float] = None
    band: Band = "none"
    cached: bool = False


class ClassifyResponse(BaseModel):
    answers: dict[str, Answer]
    fallback: bool = False
    fallback_reason: Optional[str] = None
    latency_ms: float
    backend_ms: Optional[float] = None
    model: Optional[str] = None
    truncated: bool = False
    question_hash: str


class BatchItem(TextItem):
    id: Optional[str] = None
    # Per-item binding; falls back to the request-level question_id.
    question_id: Optional[str] = Field(default=None, pattern=ID_PATTERN)


class BatchRequest(QuestionSelector):
    items: list[BatchItem] = Field(min_length=1)
    # Override template rotations for this call (batch only).
    rotations: Optional[int] = Field(default=None, ge=1, le=8)


class BatchResult(BaseModel):
    id: Optional[str] = None
    answers: dict[str, Answer]
    model: Optional[str] = None
    truncated: bool = False


class BatchResponse(BaseModel):
    results: list[BatchResult]
    latency_ms: float
    groups: int
    backend_calls: int
    cache_hits: int
    items: int


# ── jobs ────────────────────────────────────────────────────────────────────

class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    total: int
    processed: int
    failed_items: int = 0
    cache_hits: int = 0
    output_path: str
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    items_per_s: Optional[float] = None


# ── column mapping ──────────────────────────────────────────────────────────

class Column(BaseModel):
    name: str = Field(max_length=200)
    samples: list[str] = Field(default_factory=list, max_length=20)


class SurveyQuestionRef(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    text: str = Field(max_length=1000)


class MapColumnsRequest(BaseModel):
    mode: Literal["field_type", "survey_question"]
    columns: list[Column] = Field(min_length=1, max_length=256)
    # Required for mode=survey_question.
    survey_questions: list[SurveyQuestionRef] = Field(default_factory=list, max_length=2000)
    top_k: int = Field(default=8, ge=2, le=15)
    thresholds: Optional[Thresholds] = None
    model: Optional[ModelName] = None


class ColumnMapping(BaseModel):
    column: str
    target: Optional[str] = None
    confidence: float = 0.0
    band: Band = "none"
    probabilities: dict[str, float] = Field(default_factory=dict)
    # survey_question mode: the candidates the embedding step kept, best first.
    shortlist: list[str] = Field(default_factory=list)
    fallback: bool = False


class MapColumnsResponse(BaseModel):
    mode: str
    mappings: list[ColumnMapping]
    latency_ms: float
    embedding: Optional[str] = None


# ── debugging ───────────────────────────────────────────────────────────────

class CompileRequest(QuestionSelector):
    rotations: Optional[int] = Field(default=None, ge=1, le=8)
    # Optional sample to render the state the way a call would.
    sample: Optional[TextItem] = None


class CompileResponse(BaseModel):
    model: str
    question_hash: str
    state_fields: list[str]
    budget: BudgetReport
    # Exactly what would be POSTed to laya-serve /v1/systemone.
    laya_request: dict[str, Any]
