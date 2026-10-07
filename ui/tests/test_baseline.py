"""Today's behaviour (plan section 5): with every module off the gateway is never called."""
from tests.helpers import calls, fresh

from sogo_lite import db, engine, seed

RETAIL = {"q1": 3, "q3": "Too expensive", "q4": "Late", "q5": "Downtown"}


def _fired(response_id: int) -> set[str]:
    return {r["name"] for r in db.rows(
        "SELECT a.name FROM sink_log s JOIN alert_rule a ON a.id=s.rule_id WHERE s.response_id=?", response_id)}


def test_baseline_never_calls_the_gateway():
    fake = fresh(all_modules_on=False)
    keyword = engine.take(seed.RETAIL, {**RETAIL, "q2": "The price is too high"})
    paraphrase = engine.take(seed.RETAIL, {**RETAIL, "q2": "too expensive"})
    assert "q3" in engine.shown_qids(keyword), "Contains \"price\" should show the pricing question"
    assert "q3" not in engine.shown_qids(paraphrase), "A paraphrase must not match the keyword rule"
    assert "q4" not in engine.shown_qids(keyword), "The delivery follow-up needs the scope 3 module"
    assert _fired(keyword) == {"Detractor score"}, f"Expected only the score rule but got {_fired(keyword)}"
    assert engine.response(keyword)["submitted_at"], "The response should be submitted"
    assert not fake.requests and not calls(), "With every module off the Inspector must stay empty"


def test_automap_tags_exact_names_only():
    fresh(all_modules_on=False)
    assert engine.automap(seed.HOTEL) == 1, "Only \"Riverside\" matches a tag name exactly"
    tagged = {t["name"] for tags in engine.option_tags(seed.HOTEL).values() for t in tags}
    assert tagged == {"Riverside"}, f"Got {tagged}"
    assert engine.automap(seed.HOTEL) == 0, "Auto-map should not tag the same option twice"


def test_tag_limits_match_the_real_product():
    fresh(all_modules_on=False)
    products = db.val("SELECT id FROM tag_category WHERE name='Products'")
    assert db.val("SELECT COUNT(*) FROM tag WHERE category_id=?", products) == 500
    added, refused = engine.add_tags(products, ["One more"])
    assert (added, refused) == (0, 1), f"A full category should refuse a tag but got {(added, refused)}"
    for i in range(engine.MAX_TAG_CATEGORIES):
        engine.add_tag_category(f"Extra {i}")
    assert db.val("SELECT COUNT(*) FROM tag_category") == engine.MAX_TAG_CATEGORIES


def test_anonymous_alert_email_names_no_respondent():
    fresh(all_modules_on=False)
    response_id = engine.take(seed.STAFF, {"q1": 2, "q2": 1, "q3": "Nothing to add", "q4": "Under 30,000"},
                              respondent="Priya Example <priya@example.test>")
    emails = db.rows("SELECT payload FROM sink_log WHERE response_id=? AND sink='email'", response_id)
    assert len(emails) == 1, f"Expected the Low support score email but got {len(emails)}"
    payload = emails[0]["payload"]
    assert "Priya" not in payload and "response #" not in payload, f"The email names the respondent: {payload}"
    assert "anonymous" in payload.lower()


def test_auto_score_covers_fixed_option_questions_only():
    fresh(all_modules_on=False)
    response_id = engine.take(seed.QUIZ, {"q1": "75°C", "q2": "Overnight", "q3": "Bottom shelf."})
    score, available = engine.auto_score(seed.QUIZ, engine.answers(response_id))
    assert (score, available) == (1.0, 2.0), f"Got {(score, available)}"
    engine.save_post_populate(response_id, "q3", 2.5, "Good")
    assert engine.auto_score(seed.QUIZ, engine.answers(response_id))[0] == 1.0, \
        "A post-populated score must not be added into the automatic score"


def test_mandatory_and_score_follow_up():
    fresh(all_modules_on=False)
    survey_no = engine.create_project("Follow-up test", "CX Project")
    p1 = engine.pages(survey_no)[0]["id"]
    p2 = engine.add_page(survey_no)
    engine.add_question(survey_no, p1, "metric", "How likely are you to recommend us?", required="mandatory")
    engine.add_question(survey_no, p2, "text", "What went wrong?", is_followup=True, parent_qid="q1",
                        followup_min=0, followup_max=6)
    engine.add_question(survey_no, p2, "text", "Anything else?")
    visible = engine.current_questions(engine.start_response(survey_no))
    assert engine.missing_required(visible, {})["mandatory"] == ["q1"]
    low = engine.take(survey_no, {"q1": 4, "q2": "Slow", "q3": "No"})
    high = engine.take(survey_no, {"q1": 9, "q2": "Slow", "q3": "No"})
    assert "q2" in engine.shown_qids(low), "A detractor score should show the follow-up"
    assert "q2" not in engine.shown_qids(high), "A promoter score should not"
