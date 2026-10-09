"""Shared test setup: a temp database, a fake token, and an in-process stand-in for
the gateway. Import this before anything under sogo_lite/ so the settings pick the env up."""
import hashlib
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="sogo_lite_test_")
TOKEN = "test-token-do-not-log"
os.environ["SOGO_LITE_DB"] = os.path.join(_TMP, "sogo_lite.db")
os.environ["SOGO_LITE_GATEWAY_URL"] = "http://gateway.test"
os.environ["SOGO_LITE_GATEWAY_TOKEN"] = TOKEN

import httpx  # noqa: E402

from sogo_lite import db, gateway, modules, seed  # noqa: E402

TASKS = [
    {"id": "meaning.may_leave", "type": "yesno", "description": "may leave", "state": ["answer"],
     "criteria": {"true": "y", "false": "n"}},
    {"id": "meaning.safety_concern", "type": "yesno", "description": "safety", "state": ["answer"],
     "criteria": {"true": "y", "false": "n"}},
    {"id": "meaning.callback_request", "type": "yesno", "description": "callback", "state": ["answer"],
     "criteria": {"true": "y", "false": "n"}},
    {"id": "comment.sentiment", "type": "choice", "description": "sentiment", "state": ["answer"],
     "criteria": {"positive": "", "negative": "", "neutral": ""}},
    {"id": "comment.response_type", "type": "choice", "description": "type", "state": ["answer"],
     "criteria": {"complaint": "", "praise": "", "question": ""}},
    {"id": "meaning.unresolved_problem", "type": "yesno", "description": "unresolved", "state": ["answer"],
     "criteria": {"true": "y", "false": "n"}},
    {"id": "design.wording_flaw", "type": "choice", "description": "flaw", "state": ["question"],
     "criteria": {"leading": "", "fine": ""}},
    {"id": "design.question_type", "type": "choice", "description": "type", "state": ["question"],
     "criteria": {"nps": "", "open_text": ""}},
    {"id": "ex.identity_risk", "type": "choice", "description": "identity", "state": ["answer"],
     "criteria": {"identifies_writer": "", "safe": ""}},
    {"id": "invite.timing", "type": "choice", "description": "timing", "state": ["activity_note"],
     "criteria": {"send": "", "hold": "", "dont_send": ""}},
    {"id": "invite.reply_type", "type": "choice", "description": "reply", "state": ["email_reply"],
     "criteria": {"unsubscribe": "", "feedback": ""}},
]


class FakeGateway:
    """Answers like the gateway, from rules a test sets.

    `yes` holds substrings: a yes/no answer is "yes" when its id contains one of them.
    `pick` maps a choice id to the label to return. Everything else is "no" / the escape.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bindings: dict[str, dict] = {}
        self.yes: list[str] = []
        self.pick: dict[str, str] = {}
        self.status = 200
        self.fallback = False
        self.raise_error: Exception | None = None

    def paths(self, suffix: str = "") -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.endswith(suffix)]

    def _answer(self, answer_id: str, kind: str) -> dict:
        if kind == "yesno":
            yes = any(part in answer_id for part in self.yes)
            p = 0.92 if yes else 0.04
            return {"label": "yes" if yes else "no", "confidence": max(p, 1 - p), "value": p,
                    "probabilities": {"yes": p, "no": round(1 - p, 2)}, "band": "act" if yes else "none",
                    "cached": False}
        label = self.pick.get(answer_id, "other")
        return {"label": label, "confidence": 0.91, "value": None, "probabilities": {label: 0.91},
                "band": "none" if label == "other" else "act", "cached": False}

    def _answers(self, body: dict) -> dict:
        task_types = {t["id"]: t["type"] for t in TASKS}
        binding = self.bindings.get(f"{body.get('survey_no')}/{body.get('question_id')}", {})
        answers = {}
        for task in list(binding.get("tasks", [])) + list(body.get("tasks", [])):
            answers[task] = self._answer(task, task_types.get(task, "yesno"))
        for spec in list(binding.get("custom", [])) + list(body.get("questions", [])):
            if spec["type"] == "labels":
                for label in spec["classes"]:
                    answer_id = f"{spec['id']}.{label.replace(' ', '_')}"
                    answers[answer_id] = self._answer(answer_id + "|" + label, "yesno")
            else:
                answers[spec["id"]] = self._answer(spec["id"], spec["type"])
        return answers

    def _hash(self, obj: object) -> str:
        return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": {"error": "boom", "message": "boom"}})
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/tasks":
            return httpx.Response(200, json={"tasks": TASKS})
        if path.startswith("/v1/bindings/"):
            key = path.removeprefix("/v1/bindings/")
            if request.method == "PUT":
                self.bindings[key] = body
                return httpx.Response(200, json={
                    "binding": body, "budget": {"model": "auto", "ok": True, "wire_questions": 1, "lines": []},
                    "question_hash": self._hash([body["tasks"], body["custom"]])})
            return httpx.Response(200, json=self.bindings[key]) if key in self.bindings \
                else httpx.Response(404, json={"detail": {"error": "not_found", "message": "no binding"}})
        if path == "/v1/tasks/compile":
            options = max((len(q.get("classes") or {}) for q in body.get("questions", [])), default=0)
            if options > 64:
                return httpx.Response(422, json={"detail": {"error": "compile_error", "message": "too many",
                                                            "errors": ["too many options"]}})
            return httpx.Response(200, json={"model": "auto", "question_hash": "h", "state_fields": [],
                                             "budget": {"model": "auto", "ok": True, "wire_questions": 1,
                                                        "lines": [{"id": "tag", "status": "ok"}]},
                                             "laya_request": {}})
        if path == "/v1/classify":
            binding = self.bindings.get(f"{body.get('survey_no')}/{body.get('question_id')}")
            qhash = self._hash([binding["tasks"], binding["custom"]]) if binding else self._hash(body.get("questions"))
            answers = {} if self.fallback else self._answers(body)
            return httpx.Response(200, json={
                "answers": answers, "fallback": self.fallback,
                "fallback_reason": "timeout" if self.fallback else None, "latency_ms": 12.0, "backend_ms": 9.0,
                "model": "english", "truncated": False, "question_hash": qhash})
        if path == "/v1/classify/batch":
            results = [{"id": item.get("id"), "answers": self._answers(body), "model": "english",
                        "truncated": False} for item in body["items"]]
            return httpx.Response(200, json={"results": results, "latency_ms": 20.0, "groups": 1,
                                             "backend_calls": 1, "cache_hits": 0, "items": len(results)})
        return httpx.Response(404, json={"detail": {"error": "not_found", "message": path}})


def fresh(all_modules_on: bool = True) -> FakeGateway:
    """A clean seeded database and a fresh fake gateway with every class set synced."""
    fake = FakeGateway()
    gateway._http = httpx.Client(base_url="http://gateway.test", transport=httpx.MockTransport(fake.handle))
    db.init()
    modules.load()
    for name in ("down", "slow", "lowconf", "badtoken"):
        db.set_setting(f"fault.{name}", "")
    db.set_setting("catalog", "")
    for module_id in modules.MODULE_IDS:
        modules.set_on(module_id, all_modules_on)
    seed.reseed()
    if all_modules_on:
        seed.sync_class_sets()
        db.run("DELETE FROM gateway_call_log")
        fake.requests.clear()
    return fake


def calls(**where: object) -> list[dict]:
    found = db.rows("SELECT * FROM gateway_call_log ORDER BY id")
    return [c for c in found if all(c[k] == v for k, v in where.items())]
