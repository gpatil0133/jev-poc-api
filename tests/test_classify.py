"""Service tests against the fake backend: banding, timeout fallback, cache, batch grouping."""
import asyncio
import time

from tests.helpers import REGISTRY, TENANT, make_service

from app.backends.fake import FakeBackend
from app.models.schemas import QuestionSelector, QuestionSpec, Thresholds
from app.registry.compiler import NO_KEY, YES_KEY, compile_set
from app.services.classify import band_for, decode

DEFAULTS = {"act": 0.85, "suggest": 0.60}
TEXT = "Love it, but if prices go up again I'm switching"


def _state(service, cset, text):
    return service.state_for(cset, text, "Anything else to add?", None, {})


def test_banding():
    cset = compile_set(REGISTRY, tasks=["meaning.may_leave", "comment.sentiment"])
    yesno, choice = cset.by_id()["meaning.may_leave"], cset.by_id()["comment.sentiment"]

    for p_yes, expected in ((0.93, "act"), (0.70, "suggest"), (0.30, "none"), (0.02, "none")):
        raw = decode(yesno, {yesno.qid: {"probabilities": {YES_KEY: p_yes, NO_KEY: 1 - p_yes}}})
        band = band_for(yesno, raw, DEFAULTS)
        assert band == expected, f"P(yes)={p_yes}: expected {expected} but got {band}"
        assert raw["value"] == p_yes and raw["label"] == ("yes" if p_yes >= 0.5 else "no")

    # A confident escape answer is never acted on.
    probs = {"positive": 0.02, "negative": 0.02, "mixed": 0.02, "neutral": 0.02, "other": 0.92}
    raw = decode(choice, {choice.qid: {"probabilities": probs, "answer_confidence": 0.92}})
    assert band_for(choice, raw, DEFAULTS) == "none", "Escape option must band as none"

    probs = {"positive": 0.02, "negative": 0.90, "mixed": 0.04, "neutral": 0.02, "other": 0.02}
    raw = decode(choice, {choice.qid: {"probabilities": probs, "answer_confidence": 0.90}})
    assert band_for(choice, raw, DEFAULTS) == "act"
    # Thresholds belong to the consumer: same stored result, different band.
    assert band_for(choice, raw, DEFAULTS, Thresholds(act=0.95)) == "suggest"


def test_rotations_are_averaged():
    q = compile_set(REGISTRY, tasks=["comment.sentiment"], rotations=2).by_id()["comment.sentiment"]
    a = {"positive": 0.8, "negative": 0.2, "mixed": 0.0, "neutral": 0.0, "other": 0.0}
    b = {"positive": 0.2, "negative": 0.8, "mixed": 0.0, "neutral": 0.0, "other": 0.0}
    c = {"positive": 0.6, "negative": 0.4, "mixed": 0.0, "neutral": 0.0, "other": 0.0}
    raw = decode(q, {q.qid: {"probabilities": a}, q.qid + "@r1": {"probabilities": b},
                     q.qid + "@r2": {"probabilities": c}})
    assert raw["label"] == "positive", f"Expected positive but got {raw['label']}"
    assert abs(raw["probabilities"]["positive"] - 0.5333) < 0.001, f"Got {raw['probabilities']}"


def test_cache_hit():
    backend = FakeBackend()
    service = make_service(backend)
    cset = compile_set(REGISTRY, tasks=["meaning.may_leave", "comment.sentiment"])

    async def run():
        first = await service.classify_one(cset, _state(service, cset, TEXT), TENANT, text=TEXT)
        second = await service.classify_one(cset, _state(service, cset, TEXT), TENANT, text=TEXT)
        # A different task mix reuses the stored answer and only asks for the new question.
        wider = compile_set(REGISTRY, tasks=["meaning.may_leave", "comment.urgent"])
        third = await service.classify_one(wider, _state(service, wider, TEXT), TENANT, text=TEXT)
        return first, second, third

    first, second, third = asyncio.run(run())
    assert not first.fallback and first.model == "english"
    assert all(not a.cached for a in first.answers.values()), "First call must not be cached"
    assert all(a.cached for a in second.answers.values()), "Second call must be fully cached"
    assert first.answers["meaning.may_leave"].value == second.answers["meaning.may_leave"].value
    assert third.answers["meaning.may_leave"].cached and not third.answers["comment.urgent"].cached
    assert backend.predict_calls == 2, f"Expected 2 backend calls but got {backend.predict_calls}"
    assert list(backend.last_questions) == ["comment.urgent"], f"Got {list(backend.last_questions)}"
    answer = first.answers["meaning.may_leave"]
    print(f"may_leave: P(yes)={answer.value} band={answer.band}")
    assert answer.value is not None and answer.band in ("act", "suggest", "none")


def test_timeout_fallback():
    backend = FakeBackend(delay_s=0.5)
    service = make_service(backend)
    cset = compile_set(REGISTRY, tasks=["meaning.may_leave"])
    text = "timeout case"

    t0 = time.perf_counter()
    result = asyncio.run(service.classify_one(cset, _state(service, cset, text), TENANT,
                                              timeout_ms=60, text=text))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"timeout fallback returned in {elapsed_ms:.0f} ms")
    assert result.fallback and result.fallback_reason == "timeout", f"Got {result.fallback_reason}"
    assert all(a.band == "none" and a.label is None for a in result.answers.values())
    assert elapsed_ms < 300, f"Expected to return near the 60 ms budget but took {elapsed_ms:.0f} ms"


def test_backend_down_fallback():
    service = make_service(FakeBackend(down=True))
    cset = compile_set(REGISTRY, tasks=["meaning.may_leave", "comment.sentiment"])
    text = "backend down case"
    result = asyncio.run(service.classify_one(cset, _state(service, cset, text), TENANT, text=text))
    assert result.fallback and result.fallback_reason == "backend_error", f"Got {result.fallback_reason}"
    assert set(result.answers) == {"meaning.may_leave", "comment.sentiment"}
    assert all(a.band == "none" for a in result.answers.values())

    empty = asyncio.run(service.classify_one(cset, _state(service, cset, "  "), TENANT, text="  "))
    assert empty.fallback_reason == "empty_text", f"Got {empty.fallback_reason}"


def test_live_calls_past_the_cap_are_shed():
    backend = FakeBackend(delay_s=0.1)
    service = make_service(backend)
    cap = service.settings.live_max_inflight
    cset = compile_set(REGISTRY, tasks=["meaning.may_leave"])

    async def run():
        texts = [f"simultaneous respondent {i}" for i in range(cap + 6)]
        return await asyncio.gather(*(
            service.classify_one(cset, _state(service, cset, t), TENANT, text=t) for t in texts))

    results = asyncio.run(run())
    busy = [r for r in results if r.fallback_reason == "busy"]
    answered = [r for r in results if not r.fallback]
    assert len(answered) == cap and len(busy) == 6, f"Expected {cap} answered and 6 shed, got {len(answered)}/{len(busy)}"
    assert backend.predict_calls == cap, f"Shed calls must not reach Laya, got {backend.predict_calls} calls"
    assert all(r.latency_ms < 50 for r in busy), "Shed calls must return at once"


def test_batch_groups_and_dedupes():
    backend = FakeBackend()
    service = make_service(backend)
    set_a = compile_set(REGISTRY, tasks=["comment.sentiment"])
    set_b = compile_set(REGISTRY, tasks=["comment.sentiment"], inline=[
        QuestionSpec(id="topic", type="choice", classes={"price": "cost", "staff": "employees"})])
    texts = [f"batch comment {i}" for i in range(70)] + ["batch comment 0", "batch comment 1", ""]
    entries = [(set_a, _state(service, set_a, t), t) for t in texts]
    entries += [(set_b, _state(service, set_b, "another question"), "another question")]

    results, stats = asyncio.run(service.classify_batch(entries, TENANT))
    assert len(results) == len(entries), f"Expected {len(entries)} results but got {len(results)}"
    assert stats.groups == 2, f"Expected 2 groups but got {stats.groups}"
    # 70 distinct states (the two repeats and the blank are not sent) + 1 in the other group.
    assert backend.states_seen == 71, f"Expected 71 states sent but got {backend.states_seen}"
    assert results[70].answers == results[0].answers, "Repeated text must get the same answer"
    assert results[72].answers["comment.sentiment"].label is None, "Blank text must not be classified"
    assert set(results[-1].answers) == {"comment.sentiment", "topic"}

    again, stats2 = asyncio.run(service.classify_batch(entries, TENANT))
    assert stats2.cache_hits == 73 and stats2.backend_calls == 0, f"Got {stats2}"
    assert backend.states_seen == 71, "Second run must be served from cache"
    assert again[5].answers["comment.sentiment"].cached


def test_selector_uses_stored_binding():
    from app.models.schemas import Binding

    service = make_service(FakeBackend())
    service.bindings.put(TENANT, Binding(
        survey_no=11, question_id="Q5", survey_question="Why did you give that score?",
        tasks=["comment.sentiment"],
        custom=[QuestionSpec(id="topic", type="choice", classes={"price": "cost", "staff": "employees"})],
    ))
    cset, bound_question = service.compile_for(
        QuestionSelector(survey_no=11, question_id="Q5", tasks=["meaning.may_leave"]), TENANT)
    assert set(cset.by_id()) == {"comment.sentiment", "topic", "meaning.may_leave"}, f"Got {set(cset.by_id())}"
    assert bound_question == "Why did you give that score?"
    # Another tenant does not see it.
    other, _ = service.compile_for(
        QuestionSelector(survey_no=11, question_id="Q5", tasks=["meaning.may_leave"]), "999")
    assert set(other.by_id()) == {"meaning.may_leave"}, f"Binding leaked across tenants: {set(other.by_id())}"


if __name__ == "__main__":
    test_banding()
    test_rotations_are_averaged()
    test_cache_hit()
    test_timeout_fallback()
    test_backend_down_fallback()
    test_live_calls_past_the_cap_are_shed()
    test_batch_groups_and_dedupes()
    test_selector_uses_stored_binding()
    print("\n=== All classify tests PASSED ===")
