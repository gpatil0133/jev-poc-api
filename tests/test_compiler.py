"""Unit tests for the question compiler: layering, escape option, yes/no, budget, rotations."""
from tests.helpers import REGISTRY

from app.models.schemas import Binding, QuestionSpec, Thresholds
from app.registry.compiler import (
    ESCAPE_KEY, NO_KEY, YES_KEY, CompileError, build_state, compile_set, estimate_tokens,
)

TOPIC = QuestionSpec(id="topic", type="choice", classes={
    "price": "cost, fees, value for money",
    "delivery": "shipping time, courier, damaged on arrival",
    "staff": "behaviour or helpfulness of employees",
})


def test_templates_load():
    ids = REGISTRY.ids()
    print(f"templates: {ids}")
    for expected in ("comment.sentiment", "comment.response_type", "comment.quality",
                     "meaning.may_leave", "meaning.safety_concern", "meaning.complaint",
                     "meaning.callback_request", "comment.actionable", "comment.urgent",
                     "column.field_type"):
        assert expected in ids, f"Expected template {expected} but got {ids}"


def test_layering_most_specific_wins():
    binding = Binding(
        survey_no=11, question_id="Q5", tasks=["comment.sentiment", "meaning.may_leave"],
        custom=[TOPIC], thresholds={"meaning.may_leave": Thresholds(act=0.95)},
    )
    cset = compile_set(REGISTRY, binding=binding)
    qs = cset.by_id()
    assert set(qs) == {"comment.sentiment", "meaning.may_leave", "topic"}, f"Got {set(qs)}"
    assert qs["meaning.may_leave"].thresholds["act"] == 0.95, "Binding threshold override not applied"
    assert qs["meaning.may_leave"].thresholds["suggest"] == 0.60, "Template suggest threshold was lost"

    # Inline replaces the binding's custom question of the same id.
    inline = QuestionSpec(id="topic", type="choice", classes={"food": "meals", "room": "the bedroom"})
    merged = compile_set(REGISTRY, binding=binding, inline=[inline])
    labels = list(merged.by_id()["topic"].wire["criteria"])
    assert labels == ["food", "room", ESCAPE_KEY], f"Expected inline classes but got {labels}"
    assert merged.set_hash != cset.set_hash, "Different questions must hash differently"

    # Same inputs, same hash: the hash is the cache key.
    again = compile_set(REGISTRY, binding=binding)
    assert again.set_hash == cset.set_hash, f"Expected stable hash but got {again.set_hash} vs {cset.set_hash}"


def test_escape_option_injected():
    cset = compile_set(REGISTRY, inline=[TOPIC])
    q = cset.by_id()["topic"]
    assert ESCAPE_KEY in q.wire["criteria"], f"Expected escape option in {list(q.wire['criteria'])}"
    assert q.escape == ESCAPE_KEY

    # Not added twice when the author already has one.
    with_none = QuestionSpec(id="t", type="choice", classes={"a": "x", "b": "y", "None of these": "z"})
    q2 = compile_set(REGISTRY, inline=[with_none]).by_id()["t"]
    assert len(q2.wire["criteria"]) == 3, f"Escape added twice: {list(q2.wire['criteria'])}"
    assert q2.escape == "None of these", f"Expected author's escape label but got {q2.escape}"


def test_yesno_uses_neutral_keys():
    q = compile_set(REGISTRY, tasks=["meaning.may_leave"]).by_id()["meaning.may_leave"]
    assert q.kind == "yesno"
    assert q.wire["type"] == "choice", f"Expected the noul workaround (choice) but got {q.wire['type']}"
    assert list(q.wire["criteria"]) == [YES_KEY, NO_KEY], f"Got keys {list(q.wire['criteria'])}"


def test_labels_expand_to_one_yesno_each():
    spec = QuestionSpec(id="areas", type="labels", classes={"Price": "cost and fees", "Wait time": ""})
    cset = compile_set(REGISTRY, inline=[spec])
    ids = [q.qid for q in cset.questions]
    assert ids == ["areas.Price", "areas.Wait_time"], f"Got {ids}"
    price = cset.by_id()["areas.Price"]
    assert price.kind == "yesno"
    assert "Price" in price.wire["instructions"], "Instructions must be generated from the label name"
    assert "cost and fees" in price.wire["criteria"][YES_KEY]


def test_budget_rejects_oversized_options():
    long_desc = "customers describing a wide range of very specific situations in great detail " * 3
    spec = QuestionSpec(id="big", type="choice", classes={f"class{i}": long_desc for i in range(12)})
    report = compile_set(REGISTRY, inline=[spec]).budget_report()
    line = report.lines[0]
    print(f"budget line: options={line.options} option_tokens={line.option_tokens} status={line.status}")
    assert not report.ok, "Expected the report to fail"
    assert line.status == "over", f"Expected over but got {line.status}"

    small = compile_set(REGISTRY, inline=[TOPIC]).budget_report()
    assert small.ok and small.lines[0].status == "ok", f"Expected ok but got {small.lines[0]}"
    # More room on the multilingual checkpoint.
    assert compile_set(REGISTRY, inline=[TOPIC], model="multilingual").budget_report().lines[0].budget == 256


def test_shipped_templates_fit_budget():
    for task_id in REGISTRY.ids():
        report = compile_set(REGISTRY, tasks=[task_id]).budget_report()
        line = report.lines[0]
        assert line.status == "ok", f"{task_id}: {line.status} ({line.option_tokens}/{line.budget}) {line.detail}"


def test_rotations_only_when_asked():
    cset = compile_set(REGISTRY, tasks=["comment.sentiment"], rotations=3)
    single = cset.wire_questions()
    assert list(single) == ["comment.sentiment"], f"Live path must be one pass, got {list(single)}"
    rotated = cset.wire_questions(rotate=True)
    assert list(rotated) == ["comment.sentiment", "comment.sentiment@r1", "comment.sentiment@r2"]
    assert rotated["comment.sentiment@r1"]["option_order"] == [1, 2, 3, 4, 0]
    assert "option_order" not in rotated["comment.sentiment"]


def test_compile_errors():
    for kwargs, needle in (
        ({"tasks": ["nope.missing"]}, "unknown task"),
        ({}, "no questions"),
        ({"inline": [QuestionSpec(id="x", type="choice", classes={"only": "one"})]}, "at least two"),
        ({"inline": [QuestionSpec(id="many", type="labels", classes={f"l{i}": "" for i in range(40)})],
          "rotations": 2}, "over the limit"),
    ):
        try:
            compile_set(REGISTRY, **kwargs)
            assert False, f"Expected CompileError for {kwargs}"
        except CompileError as exc:
            assert needle in str(exc), f"Expected '{needle}' in '{exc}'"


def test_state_carries_context():
    cset = compile_set(REGISTRY, tasks=["comment.sentiment"])
    state = build_state(cset.state_fields, {"answer": "ok", "survey_question": "How was check-in?",
                                            "metric_score": None})
    assert state == {"survey_question": "How was check-in?", "answer": "ok"}, f"Got {state}"
    assert estimate_tokens("refunds, billing and invoices") >= 5


if __name__ == "__main__":
    test_templates_load()
    test_layering_most_specific_wins()
    test_escape_option_injected()
    test_yesno_uses_neutral_keys()
    test_labels_expand_to_one_yesno_each()
    test_budget_rejects_oversized_options()
    test_shipped_templates_fit_budget()
    test_rotations_only_when_asked()
    test_compile_errors()
    test_state_carries_context()
    print("\n=== All compiler tests PASSED ===")
