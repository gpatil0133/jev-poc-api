"""JevBackend against an in-process stand-in for the TypeSafe API: the wire it sends
and the Laya-shaped result it hands back. No network, no key."""
import asyncio
import json

from tests.helpers import make_service

import httpx

from app.backends.base import BackendError
from app.backends.jev import JevBackend
from app.models.schemas import QuestionSelector, QuestionSpec

TOPIC = {"type": "choice", "instructions": "What is this about?",
         "criteria": {"price": "cost", "staff": "people", "other": "neither"}}


class FakeTypeSafe:
    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [{"name": "jev-latest", "description": "", "release_date": ""}]})
        body = json.loads(request.content)
        answers = {}
        for qid, q in body["questions"].items():
            labels = list(q["criteria"])
            # The first listed option wins, so a reordered rotation is visible in the answer.
            probs = {label: (0.7 if i == 0 else 0.3 / (len(labels) - 1)) for i, label in enumerate(labels)}
            answers[qid] = {"type": "choice", "choice": labels[0], "confidence": 0.55, "probabilities": probs}
        return httpx.Response(200, json={"model": body["model"], "answers": answers,
                                         "usage": {"input_tokens": 40, "output_tokens": 2}})


def _backend(api: FakeTypeSafe) -> JevBackend:
    return JevBackend("https://api.typesafe.test", "key", "jev-latest", max_concurrent=4,
                      transport=httpx.MockTransport(api.handle))


def test_predict_sends_jev_wire_and_returns_laya_shape():
    api = FakeTypeSafe()
    result = asyncio.run(_backend(api).predict({"answer": "too expensive"}, {"topic": TOPIC}, "english"))
    sent = json.loads(api.requests[0].content)
    assert api.requests[0].url.path == "/v1/systemone" and api.requests[0].headers["authorization"] == "Bearer key"
    assert sent["model"] == "jev-latest", f"A Laya checkpoint name must map to the Jev default but got {sent['model']}"
    assert set(sent) == {"state", "questions", "model"}, f"Got {set(sent)}"
    answer = result["answers"]["topic"]
    assert answer["answer_confidence"] == 0.7, f"Banding should read the top probability but got {answer}"
    assert result["routing"]["model"] == "jev-latest"


def test_rotation_reorders_criteria_instead_of_option_order():
    api = FakeTypeSafe()
    rotated = dict(TOPIC, option_order=[1, 2, 0])
    asyncio.run(_backend(api).predict("x", {"topic": TOPIC, "topic@r1": rotated}))
    sent = json.loads(api.requests[0].content)["questions"]
    assert "option_order" not in sent["topic@r1"], "Jev rejects unknown fields"
    assert list(sent["topic@r1"]["criteria"]) == ["staff", "other", "price"], f"Got {list(sent['topic@r1']['criteria'])}"
    assert list(sent["topic"]["criteria"]) == ["price", "staff", "other"]


def test_batch_is_single_calls_in_input_order():
    api = FakeTypeSafe()
    states = [{"answer": f"comment {i}"} for i in range(10)]
    results = asyncio.run(_backend(api).predict_batch(states, {"topic": TOPIC}))
    assert len(api.requests) == 10 and len(results) == 10, f"Got {len(api.requests)} calls"
    assert all(r.url.path == "/v1/systemone" for r in api.requests)


def test_errors_become_backend_errors_and_health_never_raises():
    api = FakeTypeSafe(status=401)
    backend = _backend(api)
    try:
        asyncio.run(backend.predict("x", {"topic": TOPIC}))
        raise AssertionError("Expected BackendError")
    except BackendError as exc:
        assert exc.status == 401, f"Got {exc.status}"
    assert asyncio.run(backend.health())["status"] == "down"
    health = asyncio.run(_backend(FakeTypeSafe()).health())
    assert health["status"] == "ok" and health["loaded"] == ["jev-latest"], f"Got {health}"


def test_gateway_classifies_through_jev_and_keeps_caches_apart():
    api = FakeTypeSafe()
    service = make_service(_backend(api))
    selector = QuestionSelector(questions=[QuestionSpec(id="topic", classes={"price": "cost", "staff": "people"})])
    cset, _ = service.compile_for(selector, "77245")
    state = service.state_for(cset, "too expensive", None, None, {})

    async def run():
        first = await service.classify_one(cset, state, "77245", timeout_ms=2000, text="too expensive")
        second = await service.classify_one(cset, state, "77245", timeout_ms=2000, text="too expensive")
        return first, second

    first, second = asyncio.run(run())
    answer = first.answers["topic"]
    assert not first.fallback and answer.label == "price" and answer.confidence == 0.7, f"Got {first}"
    assert first.model == "jev-latest" and second.answers["topic"].cached
    assert all(key[1].startswith("jev:") for key in service.cache._data), "The cache key must name the backend"
