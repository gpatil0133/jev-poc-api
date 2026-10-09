"""The feature concepts against a fake gateway: plumbing, guardrails and fallbacks.
The fake's answers are set by each test, so nothing here says how well a model does."""
from tests.helpers import calls, fresh

from fastapi.testclient import TestClient

from sogo_lite import db, engine, events, gateway, modules, scenarios, seed, suggestions
from sogo_lite.app import app
from sogo_lite.modules import (
    distribution, fix_tracker, free_text, identity, inferred, logic_text, owners, shared_classes, survey_coach,
    virtual_questions,
)

REFUND = "I was charged twice for the same rug and my refund still has not arrived after three weeks."
INFERRED_Q2 = "virtual_questions+feedback_owners+fix_tracker"


def _harbour(comment: str = REFUND, title: str = "Sr. Eng. Manager", **kwargs) -> int:
    return engine.take(seed.HARBOUR, {"q1": 3, "q2": comment, "q3": "No, thanks", "q4": 4, "q5": 4, "q6": 4,
                                      "q7": title}, **kwargs)


def _vqs() -> tuple[dict, dict]:
    app_vq, staff_vq = virtual_questions.for_project(seed.HARBOUR)
    return app_vq, staff_vq


def _fired(response_id: int) -> set[str]:
    return {r["name"] for r in db.rows(
        "SELECT a.name FROM sink_log s JOIN alert_rule a ON a.id=s.rule_id WHERE s.response_id=?", response_id)}


def test_callback_offer_shows_the_contact_question_only_for_an_unresolved_problem():
    fake = fresh()
    assert "q3" not in engine.shown_qids(_harbour()), "Without the meaning the contact question stays hidden"
    fake.yes = ["unresolved_problem"]
    assert "q3" in engine.shown_qids(_harbour()), "An unresolved problem should show the contact question"
    with gateway.override_faults({"lowconf": "all"}):
        assert "q3" not in engine.shown_qids(_harbour()), "An unsure answer takes the default path"
    with gateway.override_faults({"down": "all"}):
        down = _harbour()
    assert "q3" not in engine.shown_qids(down) and engine.response(down)["submitted_at"]

    ok, message = logic_text.add_rule(seed.RETAIL, "q2", "ready", "meaning.unresolved_problem", "", "q4", 0.6)
    assert ok and "meaning.unresolved_problem" in shared_classes.get(seed.RETAIL, "q2")["tasks"], message


def test_inferred_answers_share_one_call_per_comment_and_are_not_asked_twice():
    fake = fresh()
    app_vq, _ = _vqs()
    fake.pick.update({virtual_questions.spec_id(app_vq["id"]): "yes_negative", "owner_team": "billing",
                      "fix_issue": "billing_error", "category": "senior_management"})
    response_id = _harbour(respondent="Test Contact")
    mine = [c for c in calls(response_id=response_id) if c["module"] == INFERRED_Q2]
    assert len(mine) == 1 and mine[0]["trigger"] == "page.next", \
        f"One call for the comment, made when the skip rule needs it: {[(c['module'], c['trigger']) for c in mine]}"
    sent = db.loads(mine[0]["request"])
    assert sent["metric_score"] == 3 and len(sent["questions"]) == 4, f"Two Virtual Questions, team, issue: {sent}"
    assert len(calls(response_id=response_id, module="free_text_category")) == 1, "The job title is its own answer"
    stored = inferred.stored(response_id, "q2")
    assert stored["owner_team"]["label"] == "billing" and stored["owner_team"]["_log"] == mine[0]["id"]

    offline = _harbour(offline=True)
    assert not inferred.results_for(offline) and not calls(response_id=offline), "Offline Mode makes no call"
    with modules.override({m: False for m in ("virtual_questions", "feedback_owners", "fix_tracker",
                                              "free_text_category")}):
        off = _harbour()
    assert not inferred.results_for(off), "With the modules off nothing is asked"


def test_free_text_counts_confident_matches_only():
    fake = fresh()
    question = engine.question_by_qid(seed.HARBOUR, "q7")
    fake.pick["category"] = "senior_management"
    _harbour()
    fake.pick["category"] = "cant_tell"
    _harbour(title="freelance")
    with gateway.override_faults({"lowconf": "all"}):
        _harbour()
    counts = free_text.counts(seed.HARBOUR, question)
    assert counts["counted"]["senior_management"] == 1 and sum(counts["counted"].values()) == 1, f"Got {counts}"
    assert counts["cant_tell"] == 1, f"Can't tell never counts toward a category: {counts}"
    assert counts["not_matched_yet"] == len(seed.HARBOUR_RESPONSES) + 1, f"Seeded and unsure answers: {counts}"

    ok, message = free_text.save(seed.HARBOUR, "q7", "", "only_one: x")
    assert not ok, message
    free_text.save(seed.HARBOUR, "q7", "", "")
    assert free_text.counts(seed.HARBOUR, question) is None, "Clearing the list removes it"


def test_virtual_question_preview_run_and_summary():
    fake = fresh()
    app_vq, _ = _vqs()
    spec_id = virtual_questions.spec_id(app_vq["id"])
    fake.pick[spec_id] = "yes_negative"
    rows, message = virtual_questions.preview(app_vq)
    assert len(rows) == virtual_questions.PREVIEW_SIZE and not message, f"Got {len(rows)} / {message}"
    assert virtual_questions.summary(app_vq)["answered"] == 0, "A preview stores nothing"

    with gateway.override_faults({"lowconf": "all"}):
        unsure = virtual_questions.run(app_vq)
    assert unsure["stored"] == 0 and unsure["unsure"] == len(seed.HARBOUR_RESPONSES), f"Got {unsure}"
    done = virtual_questions.run(app_vq, limit=10)
    summary = virtual_questions.summary(app_vq)
    assert done["stored"] == 10 and summary["answered"] == 10 and summary["margin"], f"Got {done} / {summary}"
    assert next(s for s in summary["shares"] if s["label"] == "yes_negative")["count"] == 10
    done = virtual_questions.run(app_vq, limit=None)
    summary = virtual_questions.summary(app_vq)
    assert done["sent"] == len(seed.HARBOUR_RESPONSES) - 10 and summary["margin"] is None, f"Got {done} / {summary}"
    assert virtual_questions.run(app_vq, limit=None)["sent"] == 0, "Answered comments are not sent again"

    ok, message = virtual_questions.add(seed.HARBOUR, "q2", "Did they mention parking?", "yes: mentions parking")
    added = virtual_questions.for_project(seed.HARBOUR)[-1]
    assert ok and virtual_questions.NOT_MENTIONED in added["classes"], f"Got {message} / {added}"
    assert not virtual_questions.add(seed.HARBOUR, "q1", "x?", "yes: y")[0], "The source must be a Text Box"


def test_virtual_question_drives_an_alert_and_say_it_once():
    fake = fresh()
    app_vq, staff_vq = _vqs()
    quiet = _harbour()
    assert "q4" in engine.shown_qids(quiet) and not _fired(quiet), "Not mentioned: asked as usual, no alert"

    fake.pick[virtual_questions.spec_id(app_vq["id"])] = "yes_negative"
    fake.pick[virtual_questions.spec_id(staff_vq["id"])] = "staff_good"
    said = _harbour()
    assert _fired(said) == {"Loyalty app complaint"}, f"Got {_fired(said)}"
    assert "q4" not in engine.shown_qids(said), "The staff rating is skipped when the comment covers it"
    assert virtual_questions.answers_for(said, seed.HARBOUR)[1]["effective"] == "staff_good"

    with gateway.override_faults({"lowconf": "all"}):
        unsure = _harbour()
    assert "q4" in engine.shown_qids(unsure) and not _fired(unsure), "Nothing is skipped on an unsure answer"
    with gateway.override_faults({"down": "all"}):
        down = _harbour()
    assert "q4" in engine.shown_qids(down) and engine.response(down)["submitted_at"]
    with modules.override({"virtual_questions": False}):
        off = _harbour()
    assert "q4" in engine.shown_qids(off) and not _fired(off), "Module off behaves like today"

    virtual_questions.delete(staff_vq["id"])
    assert not [r for r in engine.logic_rules(seed.HARBOUR) if r["op"] == virtual_questions.SKIP_OP]


def test_feedback_owners_routes_confident_comments_and_writes_digests():
    fake = fresh()
    fake.pick["owner_team"] = "facilities"
    routed = _harbour("The car park lighting is terrible.")
    with gateway.override_faults({"lowconf": "all"}):
        _harbour("Hard to say what this is about.")
    box = owners.inbox()
    assert [c["response_id"] for c in box["teams"]["facilities"]["comments"]] == [routed], f"Got {box['teams']}"
    assert box["teams"]["facilities"]["low"] == 1 and not box["unsure"], "A forced-unsure answer is not kept"
    assert owners.send_digests() == 1
    digest = db.loads(db.one("SELECT payload FROM sink_log WHERE rule_id IS NULL ORDER BY id DESC")["payload"])
    assert digest["to"] == "facilities-lead@example.test" and "car park" in digest["body"], f"Got {digest}"

    owners.set_projects([])
    assert not inferred.stored(_harbour("Another comment."), "q2").get("owner_team"), "Only chosen projects are read"
    assert not owners.save_teams("just_one: x")[0], "Routing needs at least two teams"


def test_fix_tracker_opens_a_case_and_reads_the_later_answer():
    fake = fresh()
    fake.pick["fix_issue"] = "billing_error"
    _harbour()
    assert not fix_tracker.cases(), "A response with no contact opens no case"
    _harbour(respondent="Imani Test")
    case = fix_tracker.cases()[0]
    assert case["issue"] == "billing_error" and case["status"] == "open", f"Got {case}"

    fake.pick[f"{fix_tracker.STATUS_PREFIX}{case['id']}"] = "fixed"
    later = _harbour("The refund came through, thank you.", respondent="Imani Test")
    sent = db.loads(next(c for c in calls(response_id=later) if "fix_tracker" in c["module"])["request"])
    status = next(q for q in sent["questions"] if q["id"].startswith(fix_tracker.STATUS_PREFIX))
    assert "billing error" in status["instructions"], f"The stored issue is named in the question: {status}"
    found = fix_tracker.cases()
    assert len(found) == 1 and found[0]["status"] == "fixed" and found[0]["status_response_id"] == later, f"Got {found}"
    line = next(r for r in fix_tracker.report() if r["issue"] == "billing_error")
    assert line["answered_again"] == 1 and line["fix_rate"] == 100, f"Got {line}"

    with gateway.override_faults({"lowconf": "all"}):
        _harbour(respondent="Unsure Contact")
    assert len(fix_tracker.cases()) == 1, "An unsure answer opens no case"


def test_backfill_reads_seeded_responses_oldest_first():
    fake = fresh()
    fake.pick["fix_issue"] = "app_login"
    counts = inferred.backfill(seed.HARBOUR)
    assert counts["failed"] == 0 and counts["answers"] >= len(seed.HARBOUR_RESPONSES), f"Got {counts}"
    contacts = {c["contact"] for c in fix_tracker.cases()}
    assert len(fix_tracker.cases()) == len(contacts) == 20, "One case per contact, also for those who answered twice"
    before = len(calls())
    inferred.backfill(seed.HARBOUR)
    repeat = [c for c in calls()[before:] if "fix_tracker" in c["module"] and "feedback_owners" in c["module"]]
    assert not repeat, "A second backfill only asks what changed"


def test_survey_coach_hints_wait_for_the_author_and_stay_dismissed():
    fake = fresh()
    fake.pick.update({"design.wording_flaw": "leading", "design.question_type": "nps"})
    survey_no = engine.create_project("Coach", "CX Project")
    question_id = engine.add_question(survey_no, engine.pages(survey_no)[0]["id"], "text",
                                      "How likely are you to recommend our great store?")
    events.emit("question.saved", question=engine.question(question_id))
    hints = {h["kind"]: h for h in survey_coach.pending(question_id)}
    assert set(hints) == {"wording", "qtype"}, f"Got {set(hints)}"
    mine = calls(module="survey_coach+qtype_pick")
    sent = db.loads(mine[0]["request"])
    assert len(mine) == 1 and sent["tasks"] == ["design.wording_flaw", "design.question_type"], f"Got {sent}"
    assert sent["context"] == {"question": "How likely are you to recommend our great store?"}, f"Got {sent}"
    assert {h["kind"] for h in suggestions.question_hints(question_id)} >= {"wording", "qtype"}

    suggestions.accept(hints["wording"]["id"])
    assert engine.question(question_id)["type"] == "text", "A wording hint changes nothing"
    suggestions.accept(hints["qtype"]["id"])
    changed = engine.question(question_id)
    assert changed["type"] == "metric" and changed["metric_kind"] == "nps", f"Got {changed}"

    events.emit("question.saved", question=changed)
    assert [h["kind"] for h in survey_coach.pending(question_id)] == ["wording"], "NPS already fits; the type hint is gone"
    suggestions.dismiss(survey_coach.pending(question_id)[0]["id"])
    before = len(calls())
    events.emit("question.saved", question=engine.question(question_id))
    assert not survey_coach.pending(question_id) and len(calls()) == before, "A dismissed hint stays dismissed"

    with gateway.override_faults({"down": "all"}):
        other = engine.add_question(survey_no, engine.pages(survey_no)[0]["id"], "radio", "Pick one",
                                    options=[("0–5", 0), ("5–10", 0)])
        events.emit("question.saved", question=engine.question(other))
    assert not survey_coach.pending(other) and engine.question(other), "On a fallback the question is still saved"


def test_blind_spot_counts_comments_no_question_asks_about():
    fake = fresh()
    fake.pick.update({"about_question": "none_of_these", "topic": "parking"})
    ok, message = survey_coach.run_blind_spot(seed.HARBOUR, "q2", "parking: car park\nloyalty_app: rewards app")
    run = survey_coach.latest_blind_spot(seed.HARBOUR)
    total = len(seed.HARBOUR_RESPONSES)
    assert ok and run["result"]["none"] == total and run["suggest"], f"Got {message} / {run}"
    assert run["suggested_topics"] == ["parking"] and run["result"]["topics"]["parking"]["count"] == total

    fake.pick["about_question"] = "q4"
    survey_coach.run_blind_spot(seed.HARBOUR, "q2")
    run = survey_coach.latest_blind_spot(seed.HARBOUR)
    assert run["result"]["matched"]["q4"] == total and not run["suggest"], f"Got {run}"
    with gateway.override_faults({"down": "all"}):
        ok, _ = survey_coach.run_blind_spot(seed.HARBOUR, "q2")
    assert not ok and db.val("SELECT COUNT(*) FROM coach_run") == 2, "A failed check is not recorded"
    assert not survey_coach.run_blind_spot(seed.HARBOUR, "q1")[0], "The source must be a Text Box"


def test_identity_warning_only_in_anonymous_projects_and_ties_nothing_to_the_response():
    fake = fresh()
    fake.pick["ex.identity_risk"] = "identifies_writer"
    provided = {"q3": {"option_ids": [], "text": "As the only night-shift nurse in Ward 4.", "number": None}}
    resp = engine.response(engine.start_response(seed.STAFF))
    question = [engine.question_by_qid(seed.STAFF, "q3")]
    notes = identity.check(resp, engine.project(seed.STAFF), question, provided)
    assert notes == {"q3": identity.RISKS["identifies_writer"]}, f"Got {notes}"
    mine = calls(module="identity_warning")
    assert len(mine) == 1 and mine[0]["response_id"] is None and mine[0]["kind"] == "respondent", f"Got {mine}"

    retail = engine.response(engine.start_response(seed.RETAIL))
    assert not identity.check(retail, engine.project(seed.RETAIL), [engine.question_by_qid(seed.RETAIL, "q2")],
                              {"q2": provided["q3"]}), "Only Anonymous projects are checked"
    with gateway.override_faults({"lowconf": "all"}):
        assert not identity.check(resp, engine.project(seed.STAFF), question, provided)
    with modules.override({"identity_warning": False}):
        assert not identity.check(resp, engine.project(seed.STAFF), question, provided)
    assert len(calls(module="identity_warning")) == 2, "Not anonymous and module off make no call"


def test_the_live_page_shows_the_identity_note_once_and_never_blocks():
    fake = fresh()
    fake.pick["ex.identity_risk"] = "identifies_writer"
    client = TestClient(app)
    response_id = engine.start_response(seed.STAFF)
    form = {"q_q1": "3", "q_q2": "3", "q_q3": "As the only night-shift nurse in Ward 4.", "q_q4": ""}
    option = engine.question_by_qid(seed.STAFF, "q4")["options"][0]["id"]
    form["q_q4"] = str(option)
    first = client.post(f"/take/r/{response_id}", data=form)
    assert first.status_code == 200 and "may identify you" in first.text, first.text[:300]
    assert not engine.response(response_id)["submitted_at"], "The note is shown before the page is saved"
    second = client.post(f"/take/r/{response_id}", data={**form, "identity_ack": "1"}, follow_redirects=False)
    assert second.status_code == 303 and engine.response(response_id)["submitted_at"], "Next again sends it as it is"


def test_invitation_hold_and_replies():
    fake = fresh()
    fake.pick["invite.timing"] = "hold"
    counts = distribution.queue(seed.HARBOUR)
    with_note = sum(1 for _, _, note in seed.CONTACTS if note)
    assert counts == {"sent": len(seed.CONTACTS) - with_note, "held": with_note, "not_sent": 0}, f"Got {counts}"
    assert len(calls(module="invite_hold")) == with_note, "Only contacts with a note are read"
    sent = db.loads(calls(module="invite_hold")[0]["request"])
    assert sent["context"] == {"activity_note": seed.CONTACTS[0][2]} and sent["tasks"] == ["invite.timing"]

    with gateway.override_faults({"down": "all"}):
        assert distribution.queue(seed.HARBOUR)["sent"] == len(seed.CONTACTS), "Note not read: sent as today"
    with modules.override({"invite_hold": False}):
        assert distribution.queue(seed.HARBOUR)["sent"] == len(seed.CONTACTS)

    contact = distribution.contacts()[0]
    fake.pick["invite.reply_type"] = "unsubscribe"
    message = distribution.receive_reply(contact["id"], "Please take me off your list.")
    assert "unsubscribed" in message and distribution.contacts()[0]["unsubscribed"] == 1, message
    before = len(calls(module="invite_hold"))
    counts = distribution.queue(seed.HARBOUR)
    assert counts["not_sent"] == 1 and len(calls(module="invite_hold")) == before + with_note - 1, f"Got {counts}"

    fake.pick["invite.reply_type"] = "feedback"
    other = distribution.contacts()[1]
    assert "forwarded" in distribution.receive_reply(other["id"], "The store was lovely.")
    with gateway.override_faults({"down": "all"}):
        assert "for a person" in distribution.receive_reply(other["id"], "Remove me.")
    assert distribution.contacts()[1]["unsubscribed"] == 0 and len(distribution.replies()) == 3


def test_the_feature_pages_render():
    fake = fresh()
    app_vq, _ = _vqs()
    fake.pick.update({virtual_questions.spec_id(app_vq["id"]): "yes_negative", "owner_team": "billing",
                      "fix_issue": "billing_error", "category": "director", "about_question": "none_of_these",
                      "invite.timing": "hold", "invite.reply_type": "unsubscribe"})
    response_id = _harbour(respondent="Page Test")
    survey_coach.run_blind_spot(seed.HARBOUR, "q2")
    distribution.queue(seed.HARBOUR)
    distribution.receive_reply(distribution.contacts()[0]["id"], "Remove me.")
    client = TestClient(app)
    title = engine.question_by_qid(seed.HARBOUR, "q7")
    for path, expected in ((f"/design/{seed.HARBOUR}/virtual", "inferred"), (f"/design/{seed.HARBOUR}/coach", "Blind spot"),
                           ("/owners?team=billing", "Page Test"), ("/owners", "For a person to route"),
                           ("/fix-tracker", "billing_error"), ("/distribute", "Held"),
                           (f"/responses/{seed.HARBOUR}", "Categories for"),
                           (f"/responses/{seed.HARBOUR}/r/{response_id}", "Inferred answers"),
                           (f"/design/{seed.HARBOUR}/q/{title['id']}", "Category list"),
                           (f"/design/{seed.HARBOUR}/logic?src=q2", "meaning.unresolved_problem"),
                           ("/settings", "Virtual Questions"), ("/sinks", "Outbox"), ("/scenarios", f"{len(scenarios.SCENARIOS)} checks")):
        page = client.get(path)
        assert page.status_code == 200 and expected in page.text, f"{path}: {page.status_code}, no {expected!r}"
    preview = client.post(f"/design/{seed.HARBOUR}/virtual/{app_vq['id']}", data={"action": "preview"})
    assert preview.status_code == 200 and "nothing was stored" in preview.text
    posted = client.post(f"/design/{seed.HARBOUR}/virtual/{app_vq['id']}", data={"action": "sample"},
                         follow_redirects=False)
    assert posted.status_code == 303 and "stored" in posted.headers["location"]


def test_feature_scenarios_hold_their_fallbacks():
    """The fault columns say whether a fallback is right, which needs no model."""
    fresh()
    forced = {m: True for m in modules.MODULE_IDS}
    for col, faults in (("down", {"down": "all"}), ("lowconf", {"lowconf": "all"})):
        ctx: dict = {}
        with modules.override(forced), gateway.override_faults(faults):
            for scenario in scenarios.FEATURE_SCENARIOS:
                status, detail = scenario.fn(col, ctx)
                assert status in ("pass", "na"), f"{scenario.id} [{col}]: {detail}"
        if "scratch" in ctx:
            engine.delete_project(ctx["scratch"])
