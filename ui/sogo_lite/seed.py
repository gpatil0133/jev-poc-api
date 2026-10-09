"""Seed data (plan section 10): the scope page's own examples.

Seeding never calls the gateway. Seeded class sets start as local drafts and are
saved to the gateway by `sync_class_sets()` (at start-up, from Settings, and at the
start of a scenario run).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sogo_lite import db, engine
from sogo_lite.modules import distribution, fix_tracker, owners, quiz_scoring, shared_classes, virtual_questions

logger = logging.getLogger(__name__)

RETAIL, CLINIC, HOTEL, STAFF, QUIZ, LARGE = 9001, 9002, 9003, 9004, 9005, 9006
HARBOUR = 9007      # the feature concepts
ALERT_MIN_PROB = 0.70
LOGIC_MIN_PROB = 0.60

# One canned question set per project type, plus a generic one (plan 5.7). No LLM is called.
AI_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "generic": [
        {"type": "radio", "wording": "How would you describe your overall experience?",
         "options": ["Very good", "Good", "Fair", "Poor"]},
        {"type": "text", "wording": "Is there anything else you would like to tell us?"},
    ],
    "Survey": [
        {"type": "radio", "wording": "How often do you use our service?",
         "options": ["Daily", "Weekly", "Monthly", "Rarely"]},
        {"type": "checkbox", "wording": "Which of these matter most to you?",
         "options": ["Price", "Quality", "Speed", "Support"]},
        {"type": "text", "wording": "What should we look at next?"},
    ],
    "CX Project": [
        {"type": "metric", "metric_kind": "nps", "wording": "How likely are you to recommend us to a friend or colleague?"},
        {"type": "text", "wording": "What is the main reason for your score?", "followup": True},
        {"type": "metric", "metric_kind": "csat", "wording": "How satisfied were you with the support you received?"},
    ],
    "EX Project": [
        {"type": "metric", "metric_kind": "csat", "wording": "How satisfied are you with your work-life balance?"},
        {"type": "radio", "wording": "Do you have what you need to do your job well?",
         "options": ["Always", "Mostly", "Sometimes", "Rarely"]},
        {"type": "text", "wording": "What one change would improve your working week?"},
    ],
    "Assessment": [
        {"type": "radio", "wording": "Sample question: which answer is correct?",
         "options": ["Option A|1", "Option B|0", "Option C|0"]},
        {"type": "text", "wording": "Explain your reasoning in one or two sentences."},
    ],
    "Poll": [
        {"type": "radio", "wording": "Which option do you prefer?", "options": ["Option 1", "Option 2", "Option 3"]},
    ],
}


def create_from_template(name: str, template: str) -> int:
    """The Create with AI stub: build a project from a canned question set."""
    ptype = template if template in engine.PROJECT_TYPES else "Survey"
    survey_no = engine.create_project(name, ptype)
    page_id = engine.pages(survey_no)[0]["id"]
    previous_metric: Optional[str] = None
    for spec in AI_TEMPLATES.get(template, AI_TEMPLATES["generic"]):
        question_id = engine.add_question(
            survey_no, page_id, spec["type"], spec["wording"],
            options=engine.parse_option_lines("\n".join(spec.get("options", []))),
            metric_kind=spec.get("metric_kind"), is_followup=bool(spec.get("followup")),
            parent_qid=previous_metric if spec.get("followup") else None)
        if spec["type"] == "metric":
            previous_metric = engine.question(question_id)["qid"]
    return survey_no


# ── helpers ─────────────────────────────────────────────────────────────────

def _q(survey_no: int, page_id: int, qtype: str, wording: str, options: Optional[list[str]] = None,
       **kwargs: Any) -> str:
    question_id = engine.add_question(survey_no, page_id, qtype, wording,
                                      options=engine.parse_option_lines("\n".join(options or [])), **kwargs)
    return engine.question(question_id)["qid"]


def _logic(survey_no: int, source: str, op: str, operand: str, target: str,
           min_prob: Optional[float] = None) -> None:
    db.run("INSERT INTO logic_rule(survey_no, source_qid, op, operand, action, target, min_prob)"
           " VALUES(?,?,?,?,?,?,?)", survey_no, source, op, operand, "show", target, min_prob)


def _alert(survey_no: int, name: str, conditions: list[dict[str, Any]], actions: list[dict[str, Any]]) -> None:
    db.run("INSERT INTO alert_rule(survey_no, name, conditions, actions) VALUES(?,?,?,?)",
           survey_no, name, db.dumps(conditions), db.dumps(actions))


def _means(qid: str, ref: str, label: str, min_prob: Optional[float] = ALERT_MIN_PROB) -> dict[str, Any]:
    return {"type": "meaning", "qid": qid, "ref": ref, "min_prob": min_prob,
            "label": f"comment on {qid} means \"{label}\" (min {min_prob:.2f})"}


def _score(qid: str, op: str, value: float, name: str) -> dict[str, Any]:
    return {"type": "score", "qid": qid, "op": op, "value": value, "label": f"{name} {op} {value:g}"}


def _class_set(survey_no: int, qid: str, tasks: list[str], custom: list[dict[str, Any]]) -> None:
    db.run("INSERT INTO class_set(survey_no, qid, tasks, custom, dirty) VALUES(?,?,?,?,1)",
           survey_no, qid, db.dumps(tasks), db.dumps(custom))


def _about(topic: str, description: str) -> dict[str, Any]:
    return {"id": f"about_{shared_classes.slug(topic)}", "type": "yesno",
            "instructions": f"Is this survey answer about {topic}?",
            "classes": {"true": f"the answer is about {topic}: {description}",
                        "false": f"the answer is not about {topic}"}}


def respond(survey_no: int, values: dict[str, Any], respondent: Optional[str] = None) -> int:
    """Insert a prepared, submitted response without raising any event."""
    response_id = db.run(
        "INSERT INTO response(survey_no, started_at, submitted_at, respondent, origin, shown)"
        " VALUES(?,?,?,?,?,?)", survey_no, db.now(), db.now(), respondent, "seed", db.dumps(list(values)))
    for qid, value in values.items():
        answer = engine.to_answer(engine.question_by_qid(survey_no, qid), value)
        db.run("INSERT INTO response_answer(response_id, qid, option_ids, text, number) VALUES(?,?,?,?,?)",
               response_id, qid, db.dumps(answer["option_ids"]), answer["text"], answer["number"])
    return response_id


def _category(name: str, tags: list[str]) -> None:
    if not db.val("SELECT 1 FROM tag_category WHERE name=?", name):
        engine.add_tag_category(name)
    engine.add_tags(db.val("SELECT id FROM tag_category WHERE name=?", name), tags)


# ── the six seeded projects (10.1) and their responses (10.2) ───────────────

def _retail() -> None:
    sn = engine.create_project("Retail feedback", "CX Project", survey_no=RETAIL)
    p1 = engine.pages(sn)[0]["id"]
    p2 = engine.add_page(sn)
    nps = _q(sn, p1, "metric", "How likely are you to recommend us to a friend or colleague?", metric_kind="nps")
    why = _q(sn, p1, "text", "What is the main reason for your score?", is_followup=True, parent_qid=nps)
    pricing = _q(sn, p2, "radio", "How do you feel about our prices?",
                 ["Too expensive", "About right", "Good value"])
    delivery = _q(sn, p2, "radio", "What went wrong with your delivery?",
                  ["Late", "Damaged", "Driver", "Wrong item"])
    _q(sn, p2, "radio", "Which branch did you visit?", ["Downtown", "Riverside", "Airport", "Online only"])
    _class_set(sn, why, ["meaning.may_leave", "meaning.safety_concern", "meaning.callback_request"], [
        _about("price", "cost, fees, too expensive, value for money"),
        _about("delivery", "shipping time, courier, driver, damaged or late arrival"),
    ])
    _logic(sn, why, "contains", "price", pricing)                       # today's keyword rule
    _logic(sn, why, "about", "about_price|yes", pricing, LOGIC_MIN_PROB)
    _logic(sn, why, "about", "about_delivery|yes", delivery, LOGIC_MIN_PROB)
    email = [{"type": "email", "target": "cx-team@example.test"}]
    _alert(sn, "Detractor score", [_score(nps, "<=", 6, "NPS")], email)
    _alert(sn, "Promoter may leave", [_score(nps, ">=", 9, "NPS"),
                                      _means(why, "meaning.may_leave|yes", "may leave")], email)
    _alert(sn, "Safety concern", [_means(why, "meaning.safety_concern|yes", "safety concern")], email)
    _alert(sn, "Wants a callback", [_means(why, "meaning.callback_request|yes", "wants a callback")], email)
    for score, comment in [
        (9, "Love it, but if prices go up again I'm switching."),
        (9, "Happy so far, though one more price rise and I'll shop elsewhere."),
        (3, "The sofa arrived two weeks late and the driver left it outside in the rain."),
        (2, "Delivery took forever and the box was soaked through."),
        (6, "too expensive"), (5, "costs went up"), (4, "not worth the money"),
        (7, "Please have someone call me about my order."),
        (10, "Great service and friendly staff."), (8, "The store was easy to find."),
    ]:
        respond(sn, {nps: score, why: comment})


def _clinic() -> None:
    sn = engine.create_project("Clinic visit", "CX Project", survey_no=CLINIC)
    p1 = engine.pages(sn)[0]["id"]
    p2 = engine.add_page(sn)
    rating = _q(sn, p1, "metric", "How satisfied were you with your visit?", metric_kind="csat")
    comment = _q(sn, p1, "text", "Tell us about your visit.", is_followup=True, parent_qid=rating)
    recovery = _q(sn, p2, "text", "We are sorry to hear that. What could we do to put this right?")
    _q(sn, p2, "radio", "Would you visit us again?", ["Yes", "Not sure", "No"])
    _class_set(sn, comment, ["comment.sentiment", "comment.response_type"], [])
    _logic(sn, comment, "about", "comment.response_type|complaint", recovery, LOGIC_MIN_PROB)
    complaint = _means(comment, "comment.response_type|complaint", "response type is complaint")
    _alert(sn, "Complaint and Negative",
           [complaint, _means(comment, "comment.sentiment|negative", "sentiment is negative")],
           [{"type": "email", "target": "practice-manager@example.test"}])
    _alert(sn, "Complaint: open a case", [complaint],
           [{"type": "webhook", "target": "https://example.test/hooks/case"},
            {"type": "salesforce", "target": "Case"}])
    for score, text in [
        (1, "The nurse was rude and nobody called me back."),
        (2, "I waited over an hour and the receptionist ignored me."),
        (5, "Everyone was kind and the visit was quick."),
        (4, "It would help to have more parking spaces."),
        (3, "Is the clinic open on Sundays?"),
    ]:
        respond(sn, {rating: score, comment: text})


def _hotel() -> None:
    sn = engine.create_project("Hotel stay", "CX Project", survey_no=HOTEL)
    p1 = engine.pages(sn)[0]["id"]
    clean = _q(sn, p1, "metric", "How clean was your room?", metric_kind="csat")
    food = _q(sn, p1, "metric", "Rate the breakfast buffet", metric_kind="csat")
    where = _q(sn, p1, "radio", "Which hotel did you stay at?",
               ["Grand – Downtown", "Grand – Airport", "Riverside", "Beachfront Resort & Spa"])
    _category("Department", ["Housekeeping", "Restaurant", "Front Desk", "Maintenance", "Spa"])
    _category("Location", ["Downtown", "Airport", "Riverside", "Beachfront"])
    respond(sn, {clean: 4, food: 5, where: "Grand – Downtown"})
    respond(sn, {clean: 2, food: 3, where: "Riverside"})


def _staff() -> None:
    sn = engine.create_project("Staff pulse", "EX Project", anonymous=True, survey_no=STAFF)
    p1 = engine.pages(sn)[0]["id"]
    balance = _q(sn, p1, "metric", "How satisfied are you with your work-life balance?", metric_kind="csat")
    manager = _q(sn, p1, "metric", "How well does your manager support you?", metric_kind="csat")
    comment = _q(sn, p1, "text", "Is there anything you would like leadership to know?")
    _q(sn, p1, "radio", "What is your annual household income?",
       ["Under 30,000", "30,000 to 60,000", "60,000 to 100,000", "Over 100,000"], required="mandatory")
    _class_set(sn, comment, ["meaning.safety_concern", "meaning.may_leave"], [])
    email = [{"type": "email", "target": "people-team@example.test"}]
    _alert(sn, "Safety concern raised", [_means(comment, "meaning.safety_concern|yes", "safety concern")], email)
    _alert(sn, "Low support score", [_score(manager, "<=", 2, "Manager support")], email)
    for a, b, text in [
        (2, 2, "The loading bay floor is slippery and someone is going to get hurt."),
        (2, 3, "I am thinking about leaving if the workload does not change."),
        (5, 5, "No complaints, the team is great."),
    ]:
        respond(sn, {balance: a, manager: b, comment: text, "q4": "30,000 to 60,000"})


def _quiz() -> None:
    sn = engine.create_project("Food-safety quiz", "Assessment", survey_no=QUIZ)
    p1 = engine.pages(sn)[0]["id"]
    temp = _q(sn, p1, "radio", "What is the minimum safe core temperature for cooked chicken?",
              ["55°C|0", "65°C|0", "75°C|1", "95°C|0"])
    time_ = _q(sn, p1, "radio", "How long can cooked food safely stay at room temperature?",
               ["Up to 2 hours|1", "Up to 6 hours|0", "Overnight|0"])
    chicken = _q(sn, p1, "text", "How should raw chicken be stored in the fridge, and why?", max_points=3)
    quiz_scoring.save_key_points(
        sn, chicken, "On the bottom shelf, below ready-to-eat food, so raw juices cannot drip onto it.",
        ["bottom shelf", "below ready-to-eat food", "stops juices dripping"])
    for a, b, text in [
        ("75°C", "Up to 2 hours", "Bottom shelf, under cooked food, so juices can't drip on it."),
        ("75°C", "Up to 6 hours", "Put it at the bottom so it doesn't drip on things."),
        ("65°C", "Up to 2 hours", "Keep it covered."),
        ("75°C", "Up to 2 hours", "On the lowest shelf below ready-to-eat food to stop raw juices dripping onto it."),
        ("95°C", "Overnight", "At the bottom of the fridge."),
        ("55°C", "Overnight", "In the freezer."),
        ("65°C", "Up to 6 hours", "I don't know"),
        ("75°C", "Up to 2 hours", "asdf"),
    ]:
        respond(sn, {temp: a, time_: b, chicken: text})


def _large() -> None:
    sn = engine.create_project("Large tag list", "CX Project", survey_no=LARGE)
    p1 = engine.pages(sn)[0]["id"]
    product = _q(sn, p1, "radio", "Which product did you buy?",
                 ["Oak dining table", "Leather corner sofa", "Wool rug", "Glass coffee table", "Gift card"])
    quality = _q(sn, p1, "metric", "How satisfied are you with the quality of your sofa or chair?",
                 metric_kind="csat")
    materials = ["Oak", "Pine", "Walnut", "Ash", "Beech", "Bamboo", "Rattan", "Leather", "Velvet", "Linen",
                 "Wool", "Cotton", "Glass", "Marble", "Steel", "Brass", "Chrome", "Concrete", "Cork", "Acrylic"]
    items = ["Table", "Chair", "Sofa", "Bench", "Stool", "Desk", "Shelf", "Cabinet", "Wardrobe", "Bed",
             "Headboard", "Mirror", "Lamp", "Rug", "Cushion", "Throw", "Curtain", "Blind", "Vase", "Planter",
             "Tray", "Clock", "Frame", "Hook", "Basket"]
    _category("Products", [f"{m} {i}" for m in materials for i in items])     # 20 x 25 = 500 tags
    respond(sn, {product: "Leather corner sofa", quality: 4})


# ── the feature concepts: one fictional retailer, Harbour & Pine ────────────
# The lists and comments are a small cut of simulators/fixtures/feature_concepts.

TEAMS = [
    ("billing", "invoices, charges, refunds, payment, prices at the till", "billing-lead@example.test"),
    ("delivery", "couriers, delivery dates, tracking, items damaged or missing on arrival", "delivery-lead@example.test"),
    ("store_staff", "behaviour, helpfulness or availability of store employees", "stores-lead@example.test"),
    ("facilities", "car park, building, lifts, lighting, signage", "facilities-lead@example.test"),
    ("digital", "website, loyalty app, login, online account", "digital-lead@example.test"),
    ("product", "product quality, defects, assembly, how the item matches its description", "product-lead@example.test"),
]
ISSUES = [
    ("late_delivery", "order arrived after the promised date, or the whole order is overdue"),
    ("billing_error", "charged the wrong amount, charged twice, or the invoice or statement is wrong"),
    ("damaged_item", "item arrived broken, cracked, dented or chipped"),
    ("app_login", "cannot sign in to the loyalty app, or keeps being logged out"),
]
SENIORITY = {
    "individual_contributor": "does the work directly, no people management",
    "manager": "manages a team, shift or store",
    "senior_management": "senior manager or head of a function",
    "director": "director level, including assistant or associate director",
    "executive": "C-level, vice president, managing director, owner or founder",
}
LOYALTY_APP = ("Did the respondent mention the Harbour & Pine loyalty app?", {
    "yes_negative": "mentions the loyalty or rewards app and is unhappy with it",
    "yes_positive": "mentions the loyalty or rewards app and is happy with it",
    "not_mentioned": "does not talk about the loyalty or rewards app"})
STAFF_SAID = ("Did the respondent say how they found the store staff?", {
    "staff_good": "says the store staff were helpful, friendly or good",
    "staff_poor": "says the store staff were unhelpful, rude or hard to find",
    "not_mentioned": "does not talk about the store staff"})
# (contact, NPS, comment, job title). A contact who appears twice answered again later.
HARBOUR_RESPONSES = [
    ("Ana Ruiz", 3, "The new app keeps logging me out before I can claim my points.", "Sr. Eng. Manager"),
    ("Ben Okafor", 2, "I have reset my password four times and still cannot sign in to the rewards app.", "Nurse"),
    ("Chloe Martin", 10, "Love collecting points on the app, it saves me money every week.", "Asst. Director of Ops"),
    ("Dev Patel", 9, "Got a birthday voucher through the rewards app, lovely touch.", "freelance"),
    ("Erin Walsh", 4, "Spent twenty minutes circling for a parking space and nearly gave up.", "Store manager"),
    ("Farid Aziz", 5, "The car park lighting is terrible, I did not feel safe walking back after dark.", "CFO"),
    ("Grace Liu", 6, "Nowhere to leave the car on a Saturday. Please sort this out.", "Teacher"),
    ("Hugo Brandt", 3, "Ticket machine swallowed my coins and the barrier would not lift.", "Head of Finance"),
    ("Imani Cole", 2, "I was charged twice for the same rug and my refund still has not arrived after three weeks.",
     "Accountant"),
    ("Jack Byrne", 4, "The invoice shows a price higher than the shelf label, nobody has corrected it.", "Owner"),
    ("Kira Novak", 1, "My order is ten days overdue and tracking has not moved since Monday.", "Shift supervisor"),
    ("Leo Santos", 3, "The sofa arrived two weeks late and the driver left it outside in the rain.", "VP Sales"),
    ("Maya Singh", 2, "The table arrived with a cracked leg and I am still waiting for a replacement.", "Designer"),
    ("Noah Kim", 9, "Staff in the Riverside store were friendly and knew the products well.", "Team lead"),
    ("Olga Petrov", 10, "The assistant who helped me pick a rug was brilliant.", "Director of Marketing"),
    ("Paul Reed", 5, "Could not find anyone on the shop floor to ask about fabric samples.", "Driver"),
    ("Quinn Hayes", 8, "Good quality cushions, exactly as described.", "Analyst"),
    ("Rosa Diaz", 7, "The wardrobe took three hours to assemble and one door does not close.", "Managing Director"),
    ("Sam Turner", 9, "Quick and easy, thanks.", "n/a"),
    ("Tara Bose", 6, "ok", "Consultant"),
    ("Imani Cole", 8, "The refund for the rug came through last week, thank you for sorting it.", "Accountant"),
    ("Kira Novak", 2, "Still no sign of my order and nobody answers the phone. Now I have had to buy elsewhere.",
     "Shift supervisor"),
    ("Maya Singh", 9, "Replacement table arrived in perfect condition.", "Designer"),
    ("Ana Ruiz", 4, "The app still signs me out every time I open it.", "Sr. Eng. Manager"),
]
CONTACTS = [
    ("Ana Ruiz", "ana.ruiz@example.test", "Renewed annual trade account, no issues raised."),
    ("Ben Okafor", "ben.okafor@example.test", "Complaint escalated to regional manager, awaiting response."),
    ("Chloe Martin", "chloe.martin@example.test", "Customer's husband passed away last week, account on pause."),
    ("Dev Patel", "dev.patel@example.test", "Asked not to be contacted for any reason other than open orders."),
    ("Erin Walsh", "erin.walsh@example.test", "Collected click-and-collect order, all fine."),
    ("Farid Aziz", "farid.aziz@example.test",
     "Solicitor's letter received regarding injury claim in store, all contact via legal."),
    ("Grace Liu", "grace.liu@example.test", ""),
]


def _harbour() -> None:
    sn = engine.create_project("Harbour & Pine feedback", "CX Project", survey_no=HARBOUR)
    p1 = engine.pages(sn)[0]["id"]
    p2, p3 = engine.add_page(sn), engine.add_page(sn)
    nps = _q(sn, p1, "metric", "How likely are you to recommend Harbour & Pine to a friend?", metric_kind="nps")
    why = _q(sn, p1, "text", "What is the main reason for your score?", is_followup=True, parent_qid=nps)
    contact = _q(sn, p2, "radio", "Would you like someone to contact you about this?", ["Yes, please", "No, thanks"])
    staff = _q(sn, p2, "metric", "How would you rate our store staff?", metric_kind="csat")
    _q(sn, p2, "metric", "How would you rate the delivery of your order?", metric_kind="csat")
    _q(sn, p2, "metric", "How would you rate the quality of the product?", metric_kind="csat")
    title = _q(sn, p3, "text", "What is your job title?")
    # Callback offer: the contact question is shown only for an unresolved problem.
    _class_set(sn, why, ["meaning.unresolved_problem"], [])
    _logic(sn, why, "about", "meaning.unresolved_problem|yes", contact, LOGIC_MIN_PROB)
    db.run("INSERT INTO category_list(survey_no, qid, instructions, classes) VALUES(?,?,?,?)", sn, title,
           "Which seniority level does this job title belong to?", db.dumps(SENIORITY))
    for wording, classes in (LOYALTY_APP, STAFF_SAID):
        db.run("INSERT INTO virtual_question(survey_no, source_qid, wording, classes, created_at) VALUES(?,?,?,?,?)",
               sn, why, wording, db.dumps(classes), db.now())
    app_vq, staff_vq = virtual_questions.for_project(sn)
    # Say it once: the staff rating is not asked when the comment already covers it.
    db.run("INSERT INTO logic_rule(survey_no, source_qid, op, operand, action, target) VALUES(?,?,?,?,?,?)",
           sn, why, virtual_questions.SKIP_OP, str(staff_vq["id"]), virtual_questions.SKIP_ACTION, staff)
    condition, _ = virtual_questions.build_condition(app_vq, "yes_negative", virtual_questions.DEFAULT_MIN_PROB)
    _alert(sn, "Loyalty app complaint", [condition], [{"type": "email", "target": "digital-lead@example.test"}])
    for feature in (owners.FEATURE, fix_tracker.FEATURE):
        db.run("INSERT INTO project_feature(survey_no, feature) VALUES(?,?)", sn, feature)
    for respondent, score, comment, job in HARBOUR_RESPONSES:
        respond(sn, {nps: score, why: comment, title: job}, respondent=respondent)


def _account() -> None:
    """Account-level lists: the teams, the known issues and the Distribute contacts."""
    for name, description, lead in TEAMS:
        db.run("INSERT INTO owner_team(name, description, lead) VALUES(?,?,?)", name, description, lead)
    for name, description in ISSUES:
        db.run("INSERT INTO known_issue(name, description) VALUES(?,?)", name, description)
    for name, email, note in CONTACTS:
        distribution.add_contact(name, email, note)


def seed_if_empty() -> bool:
    if db.val("SELECT COUNT(*) FROM project"):
        return False
    for build in (_retail, _clinic, _hotel, _staff, _quiz, _large, _harbour, _account):
        build()
    logger.info("Seeded %s projects", db.val("SELECT COUNT(*) FROM project"))
    return True


def reseed() -> None:
    """Drop everything except the settings and seed again."""
    tables = [r["name"] for r in db.rows("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table not in ("setting", "eval_run"):
            db.run(f"DELETE FROM {table}")
    seed_if_empty()


def sync_class_sets() -> dict[str, int]:
    """Save every unsynced class set to the gateway."""
    counts = {"saved": 0, "failed": 0}
    for cs in db.rows("SELECT survey_no, qid FROM class_set WHERE dirty=1"):
        ok, _ = shared_classes.sync(cs["survey_no"], cs["qid"], trigger="sync class sets")
        counts["saved" if ok else "failed"] += 1
    return counts
