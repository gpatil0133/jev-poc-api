"""The feature-concept eval sets as dry runs: every plan compiles, fits the option
budget and comes back with an answer for each check. The fake backend's answers
mean nothing, so nothing here says how well a model does."""
from tests.helpers import AUTH

from fastapi.testclient import TestClient

from app.api.main import app
from app.backends.fake import FakeBackend
from simulators.eval_task_quality import REQUEST_FIELDS
from simulators.task_eval_set import FEATURE_SOURCES, SETS, TIERS, rows

FEATURE_SETS = [s for s in SETS if s["id"] in FEATURE_SOURCES]


def _body(row: dict, plan: dict) -> dict:
    return {"text": row["text"], **{k: plan[k] for k in REQUEST_FIELDS if k in plan}}


def test_every_feature_set_has_rows_in_every_tier():
    assert len(FEATURE_SETS) == len(FEATURE_SOURCES), f"Got {[s['id'] for s in FEATURE_SETS]}"
    for eval_set in FEATURE_SETS:
        data = rows(eval_set["id"])
        assert data and {r["tier"] for r in data} <= set(TIERS), f"{eval_set['id']}: bad tiers"
        assert len({r["id"] for r in data}) == len(data), f"{eval_set['id']}: ids are not unique"


def test_every_feature_plan_is_answered_and_within_budget():
    app.state.backend = FakeBackend()
    with TestClient(app) as client:
        for eval_set in FEATURE_SETS:
            for row in rows(eval_set["id"]):
                plan = eval_set["plan"](row)
                resp = client.post("/v1/classify", json=_body(row, plan), headers=AUTH)
                assert resp.status_code == 200, f"{row['id']}: {resp.status_code} {resp.text[:200]}"
                answers = resp.json()["answers"]
                missing = [c["answer_id"] for c in plan["checks"] if c["answer_id"] not in answers]
                assert not missing, f"{row['id']}: no answer for {missing}"
                for check in plan["checks"]:
                    options = set(answers[check["answer_id"]]["probabilities"])
                    assert check["expected"] in options, \
                        f"{row['id']}: expected {check['expected']!r} is not an option of {check['answer_id']}"
            row = rows(eval_set["id"])[0]
            compiled = client.post("/v1/tasks/compile", headers=AUTH, json={
                **{k: v for k, v in _body(row, eval_set["plan"](row)).items() if k in ("tasks", "questions")},
                "sample": {k: v for k, v in _body(row, eval_set["plan"](row)).items()
                           if k in ("text", "survey_question", "metric_score", "context")}}).json()
            over = [line["id"] for line in compiled["budget"]["lines"] if line["status"] == "over"]
            assert not over, f"{eval_set['id']}: over the option budget: {over}"


def test_non_comment_tasks_read_their_own_state_fields():
    app.state.backend = FakeBackend()
    with TestClient(app) as client:
        for set_id, fields in (("fc_questions", {"question", "answer_options"}), ("fc_notes", {"activity_note"}),
                               ("fc_replies", {"email_reply"})):
            eval_set = next(s for s in FEATURE_SETS if s["id"] == set_id)
            row = rows(set_id)[0]
            plan = eval_set["plan"](row)
            compiled = client.post("/v1/tasks/compile", headers=AUTH, json={
                "tasks": plan["tasks"], "sample": {"text": row["text"], "context": plan["context"]}}).json()
            assert set(compiled["laya_request"]["state"]) == fields, f"{set_id}: {compiled['laya_request']['state']}"
