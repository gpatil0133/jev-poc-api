"""A small hand-labelled set for judging answer quality: 10 examples per task.

Two groups:
  catalog  the gateway's ten ready-made tasks (app/registry/tasks/*.yaml)
  design   the inline questions the Sogo-lite Design scopes send (ui/sogo_lite/modules);
           their wording is copied here, so keep the two in step

Every text is invented. Labels are one person's judgement, and about four in ten
examples per task are deliberately awkward (idioms, sarcasm, near misses). With 10
examples a task's accuracy moves 10 points per answer: read the results as a first
look, not a benchmark.
"""
from __future__ import annotations

from typing import Any, Optional

REASON = "What is the main reason for your score?"
VISIT = "Tell us about your visit."


def _yn(yes: list[Any], no: list[Any]) -> list[dict[str, Any]]:
    """Examples for a yes/no task. An item is a text, or (text, extras) such as a score or a `tricky` flag."""
    out = []
    for expected, items in (("yes", yes), ("no", no)):
        for item in items:
            text, extra = item if isinstance(item, tuple) else (item, {})
            out.append({"text": text, "expected": expected, **extra})
    return out


def _ch(*items: tuple) -> list[dict[str, Any]]:
    """Examples for a choice or labels task: (text, expected) or (text, expected, extras)."""
    return [{"text": i[0], "expected": i[1], **(i[2] if len(i) > 2 else {})} for i in items]


def _task(task_id: str, group: str, use: str, kind: str, examples: list[dict[str, Any]], *,
          spec: Optional[dict[str, Any]] = None, survey_question: Optional[str] = None) -> dict[str, Any]:
    return {"id": task_id, "group": group, "use": use, "kind": kind, "spec": spec,
            "survey_question": survey_question, "examples": examples}


T = {"tricky": True}

CATALOG = [
    _task("comment.sentiment", "catalog", "Shared classes; alert and filter on tone", "choice", _ch(
        ("The staff were friendly and my order arrived a day early.", "positive", {"metric_score": 10}),
        ("Terrible experience, the product broke after two days and nobody replied to my emails.", "negative", {"metric_score": 1}),
        ("Great selection, but the checkout queue was painfully slow.", "mixed", {"metric_score": 6}),
        ("I bought a kettle on Tuesday.", "neutral", {"metric_score": 7}),
        ("Absolutely love the new store layout!", "positive", {"metric_score": 9}),
        ("Overpriced and the quality has gone downhill.", "negative", {"metric_score": 3}),
        ("Delivery was quick, though the box was damaged and one item was missing.", "mixed", {"metric_score": 5, **T}),
        ("I shop here about once a month.", "neutral", {"metric_score": 7, **T}),
        ("asdfgh", "other", T),
        ("Oh great, another price rise. Just what I wanted.", "negative", {"metric_score": 2, **T}),
    ), survey_question=REASON),
    _task("comment.response_type", "catalog", "Shared classes; branch to service recovery; open a case", "choice", _ch(
        ("The app crashed three times while I was trying to pay.", "complaint"),
        ("You should add a click-and-collect option at the Riverside branch.", "suggestion"),
        ("Thank you to Maria at the till, she was wonderful.", "praise"),
        ("Do you deliver to postcodes outside the city?", "question"),
        ("My refund still has not arrived after three weeks.", "complaint"),
        ("It would be nice if the loyalty points showed on the receipt.", "suggestion"),
        ("Best customer service I have had in years.", "praise"),
        ("How do I change the email address on my account?", "question"),
        ("n/a", "other", T),
        ("Why does nobody ever answer the phone at your store?", "complaint", T),
    ), survey_question=VISIT),
    _task("comment.quality", "catalog", "Live nudge for a better answer; data cleaning", "choice", _ch(
        ("The self-checkout rejected my card twice and I had to queue again at the till.", "specific"),
        ("good", "vague"),
        ("Bad service.", "vague"),
        ("sdfkjh sdfkj", "gibberish"),
        ("nothing", "no_answer"),
        ("The driver left the parcel with a neighbour without leaving a note.", "specific"),
        ("No comment", "no_answer"),
        ("It was okay I guess.", "vague", T),
        ("test test 123", "gibberish", T),
        ("Prices on the shelf did not match what I was charged for the coffee and the bread.", "specific"),
    ), survey_question=REASON),
    _task("comment.actionable", "catalog", "Route comments someone can act on", "yesno", _yn(
        ["The card reader at till 3 has been broken for weeks.",
         "Please add more vegetarian options to the cafe menu.",
         "The website shows items as in stock when they are not.",
         ("Parking signs are confusing, I got fined even though I was a customer.", T),
         "The changing rooms were dirty and had no hooks."],
        ["Great shop, love it.", ("I just don't like shopping in general.", T), "Everything was fine.",
         "My friend told me about you.", ("Not sure, I rarely think about it.", T)],
    ), survey_question=REASON),
    _task("comment.urgent", "catalog", "Alert on comments that cannot wait", "yesno", _yn(
        ["There is a gas smell near the cafe kitchen right now.",
         "I have been charged four times for one order and my account is now overdrawn.",
         "My order with my medication has not arrived and I run out tomorrow.",
         "Your site is showing other customers' addresses when I log in.",
         ("I need this resolved today or I will dispute the charge with my bank.", T)],
        ["It would be nice to have more seating.", "The music was a bit loud.",
         ("Delivery was a day late last month but it was fine in the end.", T),
         "Love the new colours on the website.", "Prices have crept up over the year."],
    ), survey_question=REASON),
    _task("meaning.may_leave", "catalog", "Rules & Alerts: the comment means \"may leave\"", "yesno", _yn(
        [("Love it, but if prices go up again I'm switching.", {"metric_score": 9}),
         ("This is the last time I order from you.", {"metric_score": 2}),
         ("I have already started looking at other providers.", {"metric_score": 4}),
         ("One more missed delivery and I am cancelling my subscription.", {"metric_score": 5}),
         ("Happy so far, though one more price rise and I'll shop elsewhere.", {"metric_score": 9, **T})],
        [("The delivery was late but the driver apologised.", {"metric_score": 6}),
         ("I will keep coming back, great value.", {"metric_score": 10}),
         ("I left the store without finding what I wanted.", {"metric_score": 5, **T}),
         ("Could you cancel the duplicate order I placed by mistake?", {"metric_score": 7, **T}),
         ("My colleague switched to you last year and recommended you.", {"metric_score": 9, **T})],
    ), survey_question=REASON),
    _task("meaning.safety_concern", "catalog", "Rules & Alerts: the comment means \"safety concern\"", "yesno", _yn(
        ["The loading bay floor is slippery and someone is going to get hurt.",
         "A shelf collapsed next to my child in aisle four.",
         "My manager shouts and threatens people on the night shift.",
         "The kettle I bought sparked and tripped the electrics.",
         "There was broken glass in the car park all week."],
        [("The queue was killing me, far too slow.", T), ("Prices are dangerously close to your competitor's.", T),
         "The staff were helpful and the store was clean.", "My parcel arrived late.",
         ("I feel safe shopping here, the car park is well lit.", T)],
    ), survey_question="Is there anything you would like us to know?"),
    _task("meaning.complaint", "catalog", "Rules & Alerts: the comment is a complaint", "yesno", _yn(
        [("The nurse was rude and nobody called me back.", {"metric_score": 1}),
         ("I waited over an hour and the receptionist ignored me.", {"metric_score": 2}),
         "Wrong item sent twice now.",
         ("Not impressed, the food was cold when it arrived.", T),
         ("Why does nobody ever answer the phone at your store?", T)],
        [("Everyone was kind and the visit was quick.", {"metric_score": 5}),
         ("It would help to have more parking spaces.", T), "Is the clinic open on Sundays?",
         "No problems at all.", "Nothing to add."],
    ), survey_question=VISIT),
    _task("meaning.callback_request", "catalog", "Rules & Alerts: the comment means \"wants a callback\"", "yesno", _yn(
        ["Please have someone call me about my order.",
         "I would like a manager to email me regarding the refund.",
         "Can somebody get back to me on this? My number is on the account.",
         "Still waiting for a reply, please contact me.",
         ("Happy overall but I'd appreciate a follow-up call about the warranty.", T)],
        ["Your call centre staff were very polite.", ("I called twice last week and it was sorted quickly.", T),
         ("No need to contact me, just passing on feedback.", T), "Great service.",
         "The email newsletter is too frequent."],
    ), survey_question=REASON),
    _task("column.field_type", "catalog", "Import mapping (outside the Design scopes)", "choice", [
        {"text": name, "expected": expected, "context": {"column_name": name, "sample_values": samples}}
        for name, expected, samples in [
            ("Cust ID", "identifier", "C-10021, C-10022, C-10023"),
            ("First Name", "first_name", "Asha, Daniel, Mei"),
            ("Surname", "last_name", "Verma, Okafor, Tanaka"),
            ("E-mail", "email", "asha.verma@example.com, d.okafor@example.org, mei.tanaka@example.co.jp"),
            ("Mobile", "phone", "+91 98200 11223, +44 7700 900123, +81 90 1234 5678"),
            ("Joined On", "date", "2023-04-12, 2022-11-03, 2024-01-27"),
            ("City", "location", "Pune, Leeds, Osaka"),
            ("Dept", "category", "Sales, Finance, Engineering"),
            ("Lifetime Spend", "number", "18450.00, 920.50, 0.00"),
            ("Notes", "free_text", "Prefers email contact after 6pm, Asked about the annual plan twice, "
                                   "New signup from the spring webinar"),
        ]]),
]


def _about(topic: str, description: str) -> dict[str, Any]:
    return {"id": f"about_{topic}", "type": "yesno", "instructions": f"Is this survey answer about {topic}?",
            "classes": {"true": f"the answer is about {topic}: {description}",
                        "false": f"the answer is not about {topic}"}}


SOFA = "The sofa arrived two weeks late and the driver left it outside in the rain."
CHICKEN = "How should raw chicken be stored in the fridge, and why?"
KEY_POINTS = ["bottom shelf", "below ready-to-eat food", "stops juices dripping"]
EXPECTED_POINTS = "; ".join(KEY_POINTS)
DEPARTMENTS = ["Housekeeping", "Restaurant", "Front Desk", "Maintenance", "Spa"]
QUIZ_ANSWERS = [
    ("Bottom shelf, under cooked food, so juices can't drip on it.", KEY_POINTS, "correct"),
    ("Put it at the bottom so it doesn't drip on things.", ["bottom shelf", "stops juices dripping"], "partly_correct"),
    ("Keep it covered.", [], "incorrect"),
    ("On the lowest shelf below ready-to-eat food to stop raw juices dripping onto it.", KEY_POINTS, "correct"),
    ("At the bottom of the fridge.", ["bottom shelf"], "partly_correct"),
    ("In the freezer.", [], "incorrect"),
    ("I don't know", [], "not_an_answer"),
    ("asdf", [], "not_an_answer"),
    ("Underneath salads and cooked meats.", ["below ready-to-eat food"], "partly_correct"),
    ("On the top shelf so it stays coldest.", [], "incorrect"),
]

DESIGN = [
    _task("scope3.about_price", "design", "Scope 3: Logic \"answer is about price\"", "yesno", _yn(
        ["too expensive", "costs went up", "not worth the money", "The fees are ridiculous for what you get.",
         "Cheaper elsewhere."],
        [SOFA, "Staff were rude.", ("Priceless customer service, thank you!", T), "The app keeps logging me out.",
         "Great quality products."],
    ), spec=_about("price", "cost, fees, too expensive, value for money"), survey_question=REASON),
    _task("scope3.about_delivery", "design", "Scope 3: matching follow-up after a metric question", "yesno", _yn(
        [SOFA, "Courier never showed up.", "Box was crushed when it got here.", "Took ten days to arrive.",
         ("The driver was lovely and even carried it upstairs.", T)],
        ["too expensive", ("They failed to deliver on their promises about quality.", T),
         "The store was easy to find.", "Checkout on the website was confusing.", "Love the product."],
    ), spec=_about("delivery", "shipping time, courier, driver, damaged or late arrival"), survey_question=REASON),
    _task("scope4.sensitive_question", "design", "Scope 4: builder hint for a sensitive question", "yesno", _yn(
        ["What is your annual household income?", "Do you have any long-term health conditions?",
         "What is your religion?", "What is your sexual orientation?",
         "Have you ever been convicted of a criminal offence?"],
        ["Which branch did you visit?", "How satisfied were you with your visit?", "How did you hear about us?",
         "What is your favourite product category?", "How often do you shop with us?"],
    ), spec={"id": "sensitive", "type": "yesno",
             "instructions": "Does this survey question ask for sensitive personal information?",
             "classes": {"true": "asks about income, finances, health, religion, sexuality, politics, criminal "
                                 "record or another private matter people may not want to disclose",
                         "false": "an everyday question people answer without discomfort"}}),
    _task("scope5.followup_question", "design", "Scope 5: follow-up flag on a Text Box", "yesno", _yn(
        ["What is the main reason for your score?", "Why did you give that rating?",
         "Please tell us what influenced your score.", ("What could we have done to earn a higher rating?", T),
         "Tell us more about why you scored us this way."],
        ["Please enter your order number", "Which branch did you visit?", "What is your email address?",
         ("How likely are you to recommend us to a friend?", T), ("Any other comments?", T)],
    ), spec={"id": "followup", "type": "yesno",
             "instructions": "Does this survey question ask someone to explain the reason for a score or rating they gave?",
             "classes": {"true": "asks why, or for the main reason behind, a score or rating",
                         "false": "asks for a fact, a detail or anything other than the reason for a score"}}),
    _task("scope6.tag_answer_option", "design", "Scope 6: suggest a tag for an answer option", "choice", _ch(
        ("Grand – Downtown", "Downtown"), ("Grand – Airport", "Airport"), ("Beachfront Resort & Spa", "Beachfront"),
        ("The Riverside Inn", "Riverside"), ("City Centre Suites", "Downtown", T),
        ("Terminal 2 Express Hotel", "Airport", T), ("Seaview Sands", "Beachfront", T),
        ("Quayside Lodge by the river", "Riverside", T), ("Mountain Lodge", "other", T),
        ("Not sure / can't remember", "other", T),
    ), spec={"id": "tag", "type": "choice",
             "instructions": "Which of these tags best describes this survey answer option?",
             "classes": {"Downtown": "", "Airport": "", "Riverside": "", "Beachfront": ""}},
        survey_question="Which hotel did you stay at?"),
    _task("scope6.tag_question", "design", "Scope 6: suggest tags for a question", "labels", _ch(
        ("How clean was your room?", ["Housekeeping"]), ("Rate the breakfast buffet", ["Restaurant"]),
        ("How friendly was the check-in staff?", ["Front Desk"]),
        ("Was the air conditioning working properly?", ["Maintenance"]),
        ("How would you rate your massage treatment?", ["Spa"]),
        ("Were your towels and bed linen changed daily?", ["Housekeeping"]),
        ("How was the quality of dinner in the hotel restaurant?", ["Restaurant"]),
        ("How quickly was the broken shower fixed?", ["Maintenance"], T),
        ("How likely are you to recommend us?", [], T),
        ("How was check-out and the cleanliness of the lobby?", ["Front Desk", "Housekeeping"], T),
    ), spec={"id": "tag", "type": "labels", "classes": {name: "" for name in DEPARTMENTS}}),
    _task("scope7.project_type", "design", "Scope 7: Create with AI template pick", "choice", _ch(
        ("See how staff feel about the new hybrid-work policy", "EX Project"),
        ("Feedback after a support call, with NPS", "CX Project"),
        ("Food-safety quiz with a pass mark", "Assessment"),
        ("Quick vote on the offsite date", "Poll"),
        ("Research study on commuting habits in our city", "Survey"),
        ("Employee engagement pulse for the warehouse team", "EX Project"),
        ("Post-purchase satisfaction for online orders", "CX Project"),
        ("Onboarding knowledge test for new cashiers", "Assessment", T),
        ("Which logo do you prefer, A or B?", "Poll", T),
        ("Annual census of alumni careers and salaries", "Survey", T),
    ), spec={"id": "project_type", "type": "choice",
             "instructions": "What kind of project is this request asking to create?",
             "classes": {
                 "Survey": "a general questionnaire or research study that fits none of the other types",
                 "CX Project": "customer feedback on a purchase, product, service or support contact; NPS, CSAT or CES",
                 "EX Project": "employee or staff feedback: engagement, workplace, policies, how staff feel",
                 "Assessment": "a quiz, test or exam with right and wrong answers, scores or a pass mark",
                 "Poll": "a quick vote or single question to choose between options"}}),
    _task("scope8.quiz_key_points", "design", "Scope 8: which key points an answer contains", "labels",
          _ch(*[(text, points) for text, points, _ in QUIZ_ANSWERS]),
          spec={"id": "kp", "type": "labels", "instructions": "Does this quiz answer state this point?",
                "classes": {point: "" for point in KEY_POINTS}}, survey_question=CHICKEN),
    _task("scope8.quiz_grade", "design", "Scope 8: overall grade for a written answer", "choice",
          _ch(*[(text, grade) for text, _, grade in QUIZ_ANSWERS]),
          spec={"id": "grade", "type": "choice",
                "instructions": "How well does this quiz answer match the expected answer?",
                "classes": {
                    "correct": f"covers every expected point: {EXPECTED_POINTS}",
                    "partly_correct": f"covers some but not all of the expected points: {EXPECTED_POINTS}",
                    "incorrect": "a real attempt that covers none of the expected points or gets them wrong",
                    "not_an_answer": "blank, off-topic, a joke or declines to answer"}},
          survey_question=CHICKEN),
]

TASKS = CATALOG + DESIGN
