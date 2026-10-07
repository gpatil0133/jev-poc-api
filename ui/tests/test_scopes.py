"""The scope modules against a fake gateway: plumbing and fallbacks, not accuracy."""
from tests.helpers import calls, fresh

from sogo_lite import create_ai, db, engine, events, gateway, modules, seed, suggestions
from sogo_lite.modules import pii, quiz_scoring, shared_classes, tag_suggest

SOFA = "The sofa arrived two weeks late and the driver left it outside in the rain."
RETAIL = {"q1": 9, "q2": "Love it, but if prices go up again I'm switching.", "q3": "About right",
          "q4": "Late", "q5": "Downtown"}


def _fired(response_id: int) -> set[str]:
    return {r["name"] for r in db.rows(
        "SELECT a.name FROM sink_log s JOIN alert_rule a ON a.id=s.rule_id WHERE s.response_id=?", response_id)}


def _save(survey_no: int, qtype: str, wording: str, **kwargs) -> dict:
    question_id = engine.add_question(survey_no, engine.pages(survey_no)[0]["id"], qtype, wording, **kwargs)
    events.emit("question.saved", question=engine.question(question_id))
    return engine.question(question_id)


def _class_calls(**where) -> list[dict]:
    """Calls for the class set of a question; the personal-data check at submit is its own call."""
    return [c for c in calls(**where) if c["module"] != pii.ANSWER_MODULE]


def _pending(question_id: int) -> list[str]:
    return [s["kind"] for s in db.rows(
        "SELECT kind FROM suggestion WHERE target_id=? AND target_type='question' AND status='pending'", question_id)]


def test_one_classification_is_shared_by_the_branch_and_the_alerts():
    fake = fresh()
    fake.yes = ["may_leave", "about_delivery"]
    response_id = engine.take(seed.RETAIL, RETAIL)
    assert len(_class_calls(endpoint="/v1/classify")) == 1, \
        f"Expected one classify call but got {len(_class_calls(endpoint='/v1/classify'))}"
    # The comment contains "price", so today's Contains rule shows q3 either way; q4 needs the classifier.
    assert "q4" in engine.shown_qids(response_id), "\"answer is about delivery\" should show the follow-up"
    assert _fired(response_id) == {"Promoter may leave"}, f"Got {_fired(response_id)}"
    email = db.one("SELECT payload FROM sink_log WHERE response_id=?", response_id)["payload"]
    assert "matched 0.92" in email, f"The email should show the match but got {email}"
    sent = db.loads(_class_calls()[0]["request"])
    assert sent["metric_score"] == 9 and sent["question_id"] == "q2", f"Got {sent}"


def test_editing_the_class_set_classifies_new_responses_again():
    fake = fresh()
    first = engine.take(seed.RETAIL, RETAIL)
    cs = shared_classes.get(seed.RETAIL, "q2")
    shared_classes.ensure_task(seed.RETAIL, "q2", "comment.sentiment")
    assert shared_classes.get(seed.RETAIL, "q2")["question_hash"] != cs["question_hash"]
    assert shared_classes.stored(first, seed.RETAIL, "q2") is None, "An old result must not be reused"
    assert fake.paths("/v1/bindings/9001/q2"), "A meaning added from a rule editor goes into the one binding"


def test_gateway_down_takes_the_default_path_and_score_rules_still_fire():
    fresh()
    with gateway.override_faults({"down": "all"}):
        response_id = engine.take(seed.RETAIL, {**RETAIL, "q1": 3, "q2": SOFA})
    assert not set(engine.shown_qids(response_id)) & {"q3", "q4"}, "The default path shows no extra question"
    assert _fired(response_id) == {"Detractor score"}, f"Got {_fired(response_id)}"
    notes = db.loads(engine.response(response_id)["notes"])
    assert any(n["kind"] == "check skipped" for n in notes), f"Skipped checks should be recorded: {notes}"
    submit_calls = _class_calls(trigger="response.submitted")
    assert len(submit_calls) == 1, f"One attempt per question at submit, not one per rule: {len(submit_calls)}"


def test_unsure_answers_are_not_acted_on():
    fake = fresh()
    fake.yes = ["may_leave", "about_delivery"]
    with gateway.override_faults({"lowconf": "all"}):
        response_id = engine.take(seed.RETAIL, RETAIL)
    assert "q4" not in engine.shown_qids(response_id) and not _fired(response_id)


def test_offline_mode_and_module_off_make_no_call():
    fake = fresh()
    fake.yes = ["may_leave", "about_delivery"]
    offline = engine.take(seed.RETAIL, RETAIL, offline=True)
    assert not calls() and "q4" not in engine.shown_qids(offline), "Offline Mode must not call the gateway"
    with modules.override({"logic_text": False, "alert_meaning": False, "pii_answer": False}):
        off = engine.take(seed.RETAIL, RETAIL)
    assert not calls() and not _fired(off), "With the answer-level modules off nothing should be classified"


def test_builder_hints_share_one_call_and_stay_dismissed():
    fake = fresh()
    fake.yes = ["sensitive", "followup"]
    survey_no = engine.create_project("Hints", "CX Project")
    _save(survey_no, "metric", "How likely are you to recommend us?")
    reason = _save(survey_no, "text", "What is the main reason for your score?", required="mandatory")
    assert sorted(_pending(reason["id"])) == ["followup", "sensitive"], f"Got {_pending(reason['id'])}"
    mine = calls(module="sensitive_hint+pii_question+followup_flag")
    assert len(mine) == 1, f"Modules 4, 5 and 9 should share one call but made {len(mine)}"
    assert set(db.loads(mine[0]["request"])) <= {"text", "questions", "context", "timeout_ms"}

    hint = db.one("SELECT id FROM suggestion WHERE target_id=? AND kind='sensitive'", reason["id"])
    suggestions.dismiss(hint["id"])
    before = len(calls())
    events.emit("question.saved", question=engine.question(reason["id"]))
    assert "sensitive" not in _pending(reason["id"]) and len(calls()) == before, \
        "Saving again without changes must not bring a dismissed hint back"

    follow = db.one("SELECT id FROM suggestion WHERE target_id=? AND kind='followup'", reason["id"])
    assert engine.question(reason["id"])["is_followup"] == 0, "Nothing is applied before the author accepts"
    suggestions.accept(follow["id"])
    accepted = engine.question(reason["id"])
    assert accepted["is_followup"] == 1 and accepted["parent_qid"] == "q1", f"Got {accepted}"


def test_personal_data_hint_names_the_kind_and_waits_for_accept():
    fake = fresh()
    fake.pick["pii_q"] = "contact"
    survey_no = engine.create_project("Personal data", "CX Project")
    mobile = _save(survey_no, "text", "What is your mobile number?")
    hint = db.one("SELECT * FROM suggestion WHERE target_id=? AND kind='pii'", mobile["id"])
    assert hint and db.loads(hint["proposed"]) == {"pii_kind": "contact"}, f"Got {hint}"
    assert engine.question(mobile["id"])["pii_kind"] is None, "Nothing is applied before the author accepts"
    suggestions.accept(hint["id"])
    assert engine.question(mobile["id"])["pii_kind"] == "contact"
    events.emit("question.saved", question=engine.question(mobile["id"]))
    assert "pii" not in _pending(mobile["id"]), "A question already marked gets no second hint"

    fake.pick.clear()
    branch = _save(survey_no, "text", "Which branch did you visit?")
    assert "pii" not in _pending(branch["id"]), f"Got {_pending(branch['id'])}"


def test_personal_data_in_an_answer_is_flagged_at_submit():
    fake = fresh()
    fake.pick["pii_a"] = "contact"
    flagged = engine.take(seed.RETAIL, RETAIL)
    assert pii.flags(flagged)["q2"]["kind"] == "contact", f"Got {pii.flags(flagged)}"
    mine = calls(module="pii_answer")
    assert len(mine) == 1 and mine[0]["response_id"] == flagged, f"One call for the one text answer: {len(mine)}"

    offline = engine.take(seed.RETAIL, RETAIL, offline=True)
    assert not pii.flags(offline) and len(calls(module="pii_answer")) == 1, "Offline Mode must not call the gateway"
    with gateway.override_faults({"lowconf": "all"}):
        unsure = engine.take(seed.RETAIL, RETAIL)
    assert not pii.flags(unsure), "An unsure answer is not flagged"
    fake.fallback = True
    down = engine.take(seed.RETAIL, RETAIL)
    notes = db.loads(engine.response(down)["notes"], [])
    assert not pii.flags(down) and any("personal data" in n["what"] for n in notes), f"Got {notes}"


def test_shared_classes_off_keeps_the_meaning_alerts_and_parks_the_shared_ones():
    fake = fresh()
    fake.yes = ["may_leave"]
    fake.pick["comment.response_type"] = "complaint"
    with modules.override({"shared_classes": False}):
        retail = engine.take(seed.RETAIL, RETAIL)
        clinic = engine.take(seed.CLINIC, {"q1": 1, "q2": "The nurse was rude.", "q3": "An apology.", "q4": "No"})
    assert "Promoter may leave" in _fired(retail), f"Scope 2 must not depend on the switch: {_fired(retail)}"
    assert not _fired(clinic), f"Rules on the shared classes are parked: {_fired(clinic)}"
    assert not calls(response_id=clinic, module="alert_meaning"), "A class set nobody can read is not classified"


def test_tag_suggestions_shortlist_a_large_category_and_wait_for_accept():
    fake = fresh()
    fake.yes = ["Leather Sofa"]
    fake.pick["tag"] = "Leather Sofa"
    summary = tag_suggest.suggest(seed.LARGE)
    assert summary["shortlist"] > 0 and not summary["unavailable"], f"Got {summary}"
    assert not [c for c in calls(endpoint="/v1/classify/batch") if c["http_status"] != 200]
    review = tag_suggest.review_list(seed.LARGE)
    sofa = [s for s in review if s["target_text"] == "Leather corner sofa" and s["proposed"]["tag"] == "Leather Sofa"]
    assert sofa and sofa[0]["detail"]["path"] == "shortlist", f"Got {[(s['target_text'], s['proposed']) for s in review]}"
    applied = [t for tags in engine.option_tags(seed.LARGE).values() for t in tags if t["source"] == "suggestion"]
    assert not applied, "Nothing is applied until the author clicks Accept"
    suggestions.accept(sofa[0]["id"])
    applied = [t["name"] for tags in engine.option_tags(seed.LARGE).values() for t in tags
               if t["source"] == "suggestion"]
    assert applied == ["Leather Sofa"], f"Got {applied}"


def test_template_pick_needs_band_act():
    fake = fresh()
    fake.pick["project_type"] = "Assessment"
    assert create_ai.decide("Food-safety quiz with a pass mark")["template"] == "Assessment"
    with gateway.override_faults({"lowconf": "all"}):
        assert create_ai.decide("Food-safety quiz with a pass mark")["template"] == create_ai.GENERIC
    with modules.override({"template_pick": False}):
        decision = create_ai.decide("Food-safety quiz with a pass mark")
    assert decision == {"template": create_ai.GENERIC, "checked": False}, f"Got {decision}"


def test_quiz_suggestions_are_drafts_until_a_grader_confirms():
    fake = fresh()
    fake.yes = ["bottom shelf", "stops juices"]
    fake.pick["grade"] = "partly_correct"
    question = engine.question_by_qid(seed.QUIZ, "q3")
    events.emit("grading.opened", survey_no=seed.QUIZ, question=question)
    rows = quiz_scoring.grading_rows(seed.QUIZ, "q3")
    assert len(calls(endpoint="/v1/classify/batch")) == 1, "All ungraded answers go in one batch call"
    assert rows and all(r["status"] == "draft" and r["source"] == "suggestion" for r in rows), \
        "No suggested grade is final without a grader action"
    assert rows[0]["detail"]["found"] == ["bottom shelf", "stops juices dripping"] and rows[0]["score"] == 2.0, \
        f"Got {rows[0]}"
    events.emit("grading.opened", survey_no=seed.QUIZ, question=question)
    assert len(calls(endpoint="/v1/classify/batch")) == 1, "Answers with a draft are not sent again"

    fake2 = fresh()
    with gateway.override_faults({"down": "all"}):
        events.emit("grading.opened", survey_no=seed.QUIZ, question=engine.question_by_qid(seed.QUIZ, "q3"))
    rows = quiz_scoring.grading_rows(seed.QUIZ, "q3")
    assert rows and all(r["status"] is None for r in rows), "On a fallback the grader grades by hand"
    assert not fake2.requests
