"""The Evaluation page's runner against a fake gateway: sampling, storage and the stop rules."""
from tests.helpers import calls, fresh

import httpx

from sogo_lite import db, evaluation


def _use(fake) -> None:
    evaluation._client = lambda: httpx.Client(base_url="http://gateway.test",
                                              transport=httpx.MockTransport(fake.handle))


def _run(config: dict) -> dict:
    run_id = db.run("INSERT INTO eval_run(started_at, config) VALUES(?,?)", db.now(), db.dumps(config))
    evaluation._run(run_id, config)
    return evaluation.get(run_id)


def test_a_run_sends_the_chosen_number_of_texts_and_keeps_its_files():
    fake = fresh()
    _use(fake)
    config, error = evaluation.parse_config({"n_alerts": "6", "n_templates": "4", "n_quiz": "0",
                                             "usd_per_mtok": "", "split_check": "0"})
    assert config and config["counts"] == {"alerts": 6, "templates": 4}, f"Got {config} / {error}"
    run = _run(config)
    headline = run["results"]["headline"]
    assert headline["calls"] == 10 and headline["no_answer"] == 0, f"Got {headline}"
    assert len(fake.paths("/v1/classify")) == 10, f"One call per text: {len(fake.paths('/v1/classify'))}"
    assert [s["set"] for s in run["results"]["sets"]] == ["alerts", "templates"], f"Got {run['results']['sets']}"
    assert not calls(), "Evaluation calls stay out of the Inspector"
    for key in run["results"]["files"]:
        assert evaluation.file_path(run["id"], key), f"Missing file for {key}"
    questions = evaluation.by_question(run)
    assert {q["question"] for q in questions} >= {"meaning.complaint", "project_type"}, f"Got {questions}"


def test_a_small_sample_keeps_every_tier_and_is_repeatable():
    data = evaluation.rows("alerts")
    picked = evaluation.runner.sample(data, 20)
    assert len(picked) == 20 and {r["tier"] for r in picked} == {"easy", "medium", "hard"}
    assert [r["id"] for r in picked] == [r["id"] for r in evaluation.runner.sample(data, 20)]


def test_a_run_that_gets_no_answers_gives_up_early():
    fake = fresh()
    fake.fallback = True
    _use(fake)
    run = _run({"counts": {"alerts": 30}, "split_check": 0, "usd_per_mtok": None})
    assert run["results"]["headline"]["calls"] == evaluation.GIVE_UP_AFTER, f"Got {run['results']['headline']}"
    assert run["error"] and "no answer" in run["error"], f"Got {run['error']}"


def test_bad_input_is_refused():
    assert evaluation.parse_config({"n_alerts": "many"})[0] is None
    assert evaluation.parse_config({"n_alerts": "0"})[0] is None, "A run needs at least one text"
    config, _ = evaluation.parse_config({"n_alerts": "99999"})
    assert config["counts"]["alerts"] == 170, f"A count above the set is capped: {config}"
