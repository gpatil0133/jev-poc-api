"""Route tests through FastAPI's TestClient with the fake backend injected."""
import time

from tests.helpers import AUTH

from fastapi.testclient import TestClient

from app.api.main import app
from app.backends.fake import FakeBackend

TOPIC = {"id": "topic", "type": "choice", "classes": {
    "price": "cost, fees, value for money", "staff": "behaviour or helpfulness of employees"}}


def _client(backend: FakeBackend) -> TestClient:
    app.state.backend = backend
    return TestClient(app)


def test_health_and_auth():
    with _client(FakeBackend()) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok" and health["laya"]["device"] == "fake", f"Got {health}"
        assert health["tasks"] >= 10

        resp = client.get("/v1/tasks")
        assert resp.status_code == 401, f"Expected 401 without a token but got {resp.status_code}"
        assert resp.json()["detail"]["error"] == "auth_required"
        assert len(client.get("/v1/tasks", headers=AUTH).json()["tasks"]) >= 10

    with _client(FakeBackend(down=True)) as client:
        assert client.get("/health").json()["status"] == "degraded"


def test_classify_and_compile():
    with _client(FakeBackend()) as client:
        body = {"text": "Love it, but if prices go up again I'm switching",
                "survey_question": "Anything else?", "tasks": ["meaning.may_leave"], "questions": [TOPIC]}
        resp = client.post("/v1/classify", json=body, headers=AUTH)
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert set(data["answers"]) == {"meaning.may_leave", "topic"}, f"Got {set(data['answers'])}"
        may_leave = data["answers"]["meaning.may_leave"]
        assert 0.0 <= may_leave["value"] <= 1.0 and may_leave["band"] in ("act", "suggest", "none")
        assert data["fallback"] is False and data["model"] == "english"

        compiled = client.post("/v1/tasks/compile", headers=AUTH, json={
            "tasks": ["meaning.may_leave"], "questions": [TOPIC],
            "sample": {"text": "hello", "survey_question": "Anything else?"}}).json()
        laya = compiled["laya_request"]
        assert laya["state"] == {"survey_question": "Anything else?", "answer": "hello"}, f"Got {laya['state']}"
        assert "other" in laya["questions"]["topic"]["criteria"]
        assert compiled["question_hash"] == data["question_hash"], "Dry run must hash like the real call"

        bad = client.post("/v1/classify", json={"text": "x", "tasks": ["nope"]}, headers=AUTH)
        assert bad.status_code == 422 and bad.json()["detail"]["error"] == "compile_error"


def test_classify_never_errors_when_laya_is_down():
    with _client(FakeBackend(down=True)) as client:
        resp = client.post("/v1/classify", headers=AUTH,
                           json={"text": "anything at all", "tasks": ["meaning.may_leave"]})
        assert resp.status_code == 200, f"Expected 200 with fallback but got {resp.status_code}"
        data = resp.json()
        assert data["fallback"] is True and data["answers"]["meaning.may_leave"]["band"] == "none"

        batch = client.post("/v1/classify/batch", headers=AUTH,
                            json={"tasks": ["meaning.may_leave"], "items": [{"text": "a b c"}]})
        assert batch.status_code == 503, f"Expected 503 for batch but got {batch.status_code}"


def test_bindings_roundtrip_and_budget():
    with _client(FakeBackend()) as client:
        binding = {"survey_no": 11, "question_id": "Q7", "survey_question": "What could we improve?",
                   "tasks": ["comment.sentiment", "comment.quality"], "custom": [TOPIC],
                   "thresholds": {"comment.sentiment": {"act": 0.8}}}
        saved = client.put("/v1/bindings/11/Q7", json=binding, headers=AUTH)
        assert saved.status_code == 200, f"Got {saved.status_code}: {saved.text}"
        assert saved.json()["budget"]["ok"] is True
        assert client.get("/v1/bindings/11/Q7", headers=AUTH).json()["tasks"] == binding["tasks"]
        assert client.get("/v1/bindings/11/Q7", headers={"Authorization": "Bearer 5"}).status_code == 404

        resp = client.post("/v1/classify", headers=AUTH,
                           json={"text": "the queue was slow", "survey_no": 11, "question_id": "Q7"})
        assert set(resp.json()["answers"]) == {"comment.sentiment", "comment.quality", "topic"}

        long_desc = "customers describing a wide range of very specific situations in great detail " * 3
        fat = {"survey_no": 11, "question_id": "Q8", "custom": [
            {"id": "big", "type": "choice", "classes": {f"c{i}": long_desc for i in range(12)}}]}
        rejected = client.put("/v1/bindings/11/Q8", json=fat, headers=AUTH)
        assert rejected.status_code == 422, f"Expected 422 but got {rejected.status_code}"
        assert rejected.json()["detail"]["error"] == "over_budget"
        assert client.get("/v1/bindings/11/Q8", headers=AUTH).status_code == 404, "Rejected binding was saved"


def test_batch_and_jobs():
    with _client(FakeBackend()) as client:
        items = [{"id": str(i), "text": f"comment number {i}"} for i in range(100)]
        body = {"tasks": ["comment.sentiment", "meaning.complaint"], "items": items, "rotations": 2}
        batch = client.post("/v1/classify/batch", json=body, headers=AUTH).json()
        assert batch["items"] == 100 and batch["backend_calls"] == 2, f"Got {batch['backend_calls']} calls"
        assert batch["results"][3]["id"] == "3"

        too_many = client.post("/v1/classify/batch", headers=AUTH, json={
            "tasks": ["comment.sentiment"], "items": [{"text": "x"}] * 300})
        assert too_many.status_code == 413, f"Expected 413 but got {too_many.status_code}"

        job_items = [{"id": str(i), "text": f"job comment {i}"} for i in range(600)]
        job = client.post("/v1/jobs", headers=AUTH,
                          json={"tasks": ["comment.sentiment"], "items": job_items})
        assert job.status_code == 202, f"Got {job.status_code}: {job.text}"
        job_id = job.json()["job_id"]
        status = job.json()
        for _ in range(100):
            status = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
            if status["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert status["status"] == "done" and status["processed"] == 600, f"Got {status}"
        with open(status["output_path"], encoding="utf-8") as fh:
            assert len(fh.readlines()) == 600
        assert client.get(f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer 5"}).status_code == 404


def test_map_columns():
    with _client(FakeBackend()) as client:
        columns = [{"name": "Email Address", "samples": ["a@x.com", "b@y.org"]},
                   {"name": "Joined", "samples": ["2024-01-03", "2023-11-20"]}]
        resp = client.post("/v1/map/columns", headers=AUTH, json={"mode": "field_type", "columns": columns})
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
        assert [m["column"] for m in resp.json()["mappings"]] == ["Email Address", "Joined"]

        questions = [{"id": f"Q{i}", "text": f"How satisfied are you with aspect number {i}?"}
                     for i in range(40)]
        questions[17]["text"] = "What is your email address?"
        resp = client.post("/v1/map/columns", headers=AUTH, json={
            "mode": "survey_question", "columns": columns, "survey_questions": questions, "top_k": 8})
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
        first = resp.json()["mappings"][0]
        assert len(first["shortlist"]) == 8, f"Expected a shortlist of 8 but got {len(first['shortlist'])}"
        assert "Q17" in first["shortlist"], f"Expected the email question shortlisted, got {first['shortlist']}"
        assert set(first["probabilities"]) == set(first["shortlist"]) | {"none"}

        missing = client.post("/v1/map/columns", headers=AUTH,
                              json={"mode": "survey_question", "columns": columns})
        assert missing.status_code == 422


if __name__ == "__main__":
    test_health_and_auth()
    test_classify_and_compile()
    test_classify_never_errors_when_laya_is_down()
    test_bindings_roundtrip_and_budget()
    test_batch_and_jobs()
    test_map_columns()
    print("\n=== All API tests PASSED ===")
