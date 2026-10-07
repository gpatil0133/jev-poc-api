"""Builds the mock dataset for the JEV feature concepts (Virtual Questions, Survey
Coach, Fix Tracker, Feedback Owners and the decision-only features).

Everything is hand-written for one fictional retailer, Harbour & Pine, so the
files link up: comments carry a contact and a branch, Fix Tracker follow-ups
point back at the complaint they follow. Each row has the answer a careful
person would give, so a playground or gateway run can be checked against it.

    python -m simulators.build_feature_dataset

Writes to simulators/fixtures/feature_concepts/:
  *.csv                 the data, one file per source, expected labels included
  tasks.json            one question definition per feature (gateway QuestionSpec shape)
  requests/*.jsonl      one /v1/systemone request per row: {id, state, questions, expected}
  ground_truth.json     label counts and the planted patterns
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from app.registry.compiler import HEAD_BUDGET, MIN_INSTRUCTION_TOKENS, estimate_tokens

OUT_DIR = Path(__file__).parent / "fixtures" / "feature_concepts"
COMPANY = "Harbour & Pine"
REASON_QUESTION = "What is the main reason for your score?"
EX_QUESTION = "What would make this a better place to work?"

E, P, T = "easy", "paraphrase", "tricky"
NM = "not_mentioned"
NOQ = "none_of_these"
OPEN, CLOSED = "unresolved", "not_unresolved"

# ── CX comments ─────────────────────────────────────────────────────────────
# (branch, nps, text, loyalty app, survey question it fits, blind-spot topic,
#  owning team, callback, known issue, difficulty)
COMMENTS: list[tuple[str, int, str, str, str, str, str, str, str, str]] = [
    # loyalty app, unhappy
    ("Online", 3, "The new app keeps logging me out before I can claim my points.",
     "yes_negative", NOQ, "loyalty_app", "digital", OPEN, "app_login", E),
    ("Downtown", 6, "Tried to scan my rewards code at the till and the app froze, so I lost the discount that day.",
     "yes_negative", NOQ, "loyalty_app", "digital", CLOSED, "none", T),
    ("Online", 2, "I have reset my password four times and still cannot sign in to the rewards thing on my phone.",
     "yes_negative", NOQ, "loyalty_app", "digital", OPEN, "app_login", P),
    ("Riverside", 5, "Points from my last two purchases never showed up in the app.",
     "yes_negative", NOQ, "loyalty_app", "digital", OPEN, "none", E),
    ("Northgate", 4, "Honestly the old paper stamp card was better than this app, it is slow and confusing.",
     "yes_negative", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    ("Online", 1, "App says my account does not exist, but I have been a member for three years. Nobody has replied to my email about it.",
     "yes_negative", NOQ, "loyalty_app", "digital", OPEN, "app_login", E),
    ("Downtown", 7, "Nice store, helpful people. Only gripe is the loyalty app asking me to log in every single time.",
     "yes_negative", "staff", "", "digital", CLOSED, "app_login", T),
    ("Online", 3, "Why does the app need my location just to show my points? Uninstalled it.",
     "yes_negative", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    ("Riverside", 5, "The barcode in the app would not scan at self checkout, had to give my phone number instead.",
     "yes_negative", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    # loyalty app, happy
    ("Downtown", 10, "Love collecting points on the app, it saves me money every week.",
     "yes_positive", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    ("Online", 9, "The app made reordering my usual cushions really quick.",
     "yes_positive", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    ("Northgate", 9, "Got a birthday voucher through the rewards app, lovely touch.",
     "yes_positive", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    ("Riverside", 8, "Checking stock in your app before driving over is so handy.",
     "yes_positive", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    ("Online", 10, "Signed up to the loyalty app last month and already had two free deliveries from it.",
     "yes_positive", NOQ, "loyalty_app", "digital", CLOSED, "none", T),
    ("Downtown", 9, "App works great now after the update, points appear straight away.",
     "yes_positive", NOQ, "loyalty_app", "digital", CLOSED, "none", E),
    # parking: nothing in the survey asks about it
    ("Northgate", 4, "Spent twenty minutes circling for a parking space and nearly gave up.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", E),
    ("Riverside", 5, "The car park lighting is terrible, I did not feel safe walking back with my bags after dark.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", E),
    ("Northgate", 6, "Nowhere to leave the car on a Saturday. Please sort this out.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", P),
    ("Downtown", 7, "Shop is great but the multi-storey next door charges a fortune.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", T),
    ("Riverside", 3, "Ticket machine swallowed my coins and the barrier would not lift, I was stuck for ten minutes.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", P),
    ("Northgate", 5, "The spaces are too narrow for a family car, someone scratched my door.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", P),
    ("Riverside", 8, "Plenty of parking and it was free, which made a nice change.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", E),
    ("Northgate", 4, "No disabled bays near the entrance, my mother had to walk the whole length of the lot.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", P),
    ("Downtown", 6, "Could not find where to park the van for collection, signage is poor.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", E),
    ("Riverside", 5, "Lift from the car park level has been out of order for weeks.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", T),
    ("Northgate", 6, "Friendly team inside, but getting in and out of the car park on a weekend is a nightmare.",
     NM, NOQ, "parking", "facilities", CLOSED, "none", T),
    # delivery
    ("Online", 2, "The sofa arrived two weeks late and the driver left it outside in the rain.",
     NM, "delivery", "", "delivery", CLOSED, "late_delivery", E),
    ("Online", 3, "Still waiting for the dining table I ordered in August. No update, no tracking.",
     NM, "delivery", "", "delivery", OPEN, "late_delivery", E),
    ("Online", 4, "Courier turned up a day after the slot I paid extra for.",
     NM, "delivery", "", "delivery", CLOSED, "late_delivery", P),
    ("Northgate", 5, "Wardrobe came with a cracked door panel, box looked like it had been dropped, and I am still waiting for a replacement panel.",
     NM, "delivery", "", "delivery", OPEN, "damaged_item", E),
    ("Online", 9, "Delivery guys were brilliant, carried everything upstairs and took the packaging away.",
     NM, "delivery", "", "delivery", CLOSED, "none", E),
    ("Online", 8, "Arrived a day early and well packed.",
     NM, "delivery", "", "delivery", CLOSED, "none", E),
    ("Riverside", 3, "Promised by Friday, came the following Thursday. I took a day off work for nothing.",
     NM, "delivery", "", "delivery", CLOSED, "late_delivery", P),
    ("Online", 2, "Mirror was smashed when I opened the box. Third time I am writing about this.",
     NM, "delivery", "", "delivery", OPEN, "damaged_item", E),
    ("Online", 6, "Tracking link never worked but the order did show up on time.",
     NM, "delivery", "", "delivery", CLOSED, "none", T),
    ("Downtown", 4, "Two of the six chairs are missing from my order and nobody can tell me where they are.",
     NM, "delivery", "", "delivery", OPEN, "none", T),
    # billing
    ("Northgate", 2, "I was charged twice for the same rug and my refund still has not arrived after three weeks.",
     NM, "billing", "", "billing", OPEN, "billing_error", E),
    ("Northgate", 3, "Invoice shows the full price even though the sale discount was applied at the till. I need a corrected invoice for my expenses.",
     NM, "billing", "", "billing", OPEN, "billing_error", E),
    ("Online", 4, "Delivery fee was added twice at checkout. It was refunded quickly once I called.",
     NM, "billing", "", "billing", CLOSED, "billing_error", T),
    ("Riverside", 9, "Paying was quick and the receipt was emailed straight away.",
     NM, "billing", "", "billing", CLOSED, "none", E),
    ("Northgate", 3, "They took the deposit and then the whole amount as well. Can someone ring me?",
     NM, "billing", "", "billing", OPEN, "billing_error", P),
    ("Downtown", 8, "Refund for the returned lamp came through in two days, no fuss.",
     NM, "billing", "", "billing", CLOSED, "none", E),
    ("Online", 3, "Finance option at checkout quoted 0% and my statement shows interest.",
     NM, "billing", "", "billing", OPEN, "billing_error", P),
    ("Northgate", 6, "Prices on the shelf did not match what scanned at the till, had to get the manager over.",
     NM, "billing", "", "billing", CLOSED, "billing_error", T),
    ("Riverside", 7, "No complaints about the bill, just wish you took Amex.",
     NM, "billing", "", "billing", CLOSED, "none", E),
    # staff
    ("Downtown", 10, "Everyone I spoke to was patient and helpful.",
     NM, "staff", "", "store_staff", CLOSED, "none", E),
    ("Riverside", 2, "The assistant rolled her eyes when I asked for help and walked off.",
     NM, "staff", "", "store_staff", CLOSED, "none", E),
    ("Northgate", 9, "Maya at the design desk spent an hour helping us plan the kitchen.",
     NM, "staff", "", "store_staff", CLOSED, "none", E),
    ("Downtown", 4, "Could not find anyone to help me on the shop floor.",
     NM, "staff", "", "store_staff", CLOSED, "none", P),
    ("Riverside", 3, "Was told the item was in stock, drove forty minutes, and it was not. Nobody apologised.",
     NM, "staff", "", "store_staff", CLOSED, "none", T),
    ("Northgate", 8, "Lovely welcome at the door.",
     NM, "staff", "", "store_staff", CLOSED, "none", P),
    ("Downtown", 5, "Manager was dismissive when I raised the damaged table, said it was not his department. I still need it sorted.",
     NM, "staff", "", "store_staff", OPEN, "damaged_item", T),
    ("Riverside", 9, "The team at Riverside always remember my name.",
     NM, "staff", "", "store_staff", CLOSED, "none", E),
    # product
    ("Online", 3, "The bookcase is a bit wobbly, you get what you pay for I suppose.",
     NM, "product", "", "product", CLOSED, "none", E),
    ("Downtown", 9, "Really solid oak table, worth every penny.",
     NM, "product", "", "product", CLOSED, "none", E),
    ("Online", 4, "Colour is nothing like the photos on the website.",
     NM, "product", "", "product", CLOSED, "none", T),
    ("Northgate", 5, "Assembly instructions were missing a whole page, had to work it out from a video.",
     NM, "product", "", "product", CLOSED, "none", E),
    ("Riverside", 8, "Mattress is the best sleep I have had in years.",
     NM, "product", "", "product", CLOSED, "none", P),
    ("Online", 2, "Zip on the cushion cover broke the first time I washed it and I want a replacement.",
     NM, "product", "", "product", OPEN, "none", T),
    ("Downtown", 7, "Decent quality for the price.",
     NM, "product", "", "product", CLOSED, "none", E),
    # nothing to read
    ("Riverside", 7, "ok", NM, NOQ, "", "none", CLOSED, "none", E),
    ("Online", 8, "Nothing to add.", NM, NOQ, "", "none", CLOSED, "none", E),
    ("Northgate", 5, "asdf test", NM, NOQ, "", "none", CLOSED, "none", E),
    ("Downtown", 9, "All good thanks", NM, NOQ, "", "none", CLOSED, "none", E),
]

# ── Fix Tracker: the same contact answers a later survey ────────────────────
# (comment number in COMMENTS or None, branch, issue, earlier complaint when it is
#  not in COMMENTS, new comment, state of the earlier problem, difficulty)
FOLLOWUPS: list[tuple[Optional[int], str, str, Optional[str], str, str, str]] = [
    (37, "Northgate", "billing_error", None,
     "Still no refund for the rug I was charged twice for. This is now two months.", "still_happening", E),
    (38, "Northgate", "billing_error", None, "Got the corrected invoice in the end, thanks.", "fixed", E),
    (41, "Northgate", "billing_error", None,
     "Not only did nobody call, this month you have taken another payment I never agreed to.", "worse", E),
    (44, "Northgate", "billing_error", None,
     "Shelf labels were wrong again on Saturday, same story at the till.", "still_happening", P),
    (None, "Northgate", "billing_error", "My card was debited but the order shows as unpaid.",
     "Order is still marked unpaid and I keep getting reminder emails.", "still_happening", P),
    (39, "Online", "billing_error", None, "Great service this time, the sofa bed is perfect.", NM, E),
    (43, "Online", "billing_error", None,
     "The interest was reversed and the statement is correct now.", "fixed", E),
    (None, "Riverside", "billing_error", "Charged for delivery on an order that qualified for free shipping.",
     "Shipping charge was credited back within the week.", "fixed", P),
    (None, "Downtown", "billing_error", "Receipt total did not match what left my account.",
     "All sorted, the difference was refunded.", "fixed", E),
    (None, "Online", "billing_error", "Voucher code was accepted but the discount never came off the total.",
     "You honoured the voucher after I emailed, appreciated.", "fixed", P),
    (27, "Online", "late_delivery", None,
     "This order came on the day you said it would. Much better.", "fixed", P),
    (28, "Online", "late_delivery", None,
     "It is November and the table from August is still not here.", "still_happening", E),
    (29, "Online", "late_delivery", None,
     "Paid for a timed slot again and again the driver missed it.", "still_happening", E),
    (33, "Riverside", "late_delivery", None,
     "New order, same problem, except this time it was nearly three weeks overdue.", "worse", T),
    (None, "Online", "late_delivery", "Bed frame was ten days late.",
     "Second order turned up bang on time.", "fixed", E),
    (None, "Online", "late_delivery", "Delivery date was moved three times.",
     "Lovely lamp, very happy with it.", NM, E),
    (None, "Downtown", "late_delivery",
     "Click and collect order was not ready when I arrived, had to come back the next day.",
     "Collection was ready and waiting when I walked in.", "fixed", P),
    (None, "Online", "late_delivery", "Waited in all day and nobody came.",
     "Once again I stayed home for a delivery that did not show up.", "still_happening", P),
    (30, "Northgate", "damaged_item", None,
     "Replacement door panel arrived and was fitted, wardrobe looks fine now.", "fixed", E),
    (34, "Online", "damaged_item", None,
     "The replacement mirror turned up cracked as well.", "still_happening", T),
    (52, "Downtown", "damaged_item", None,
     "Still stuck with the damaged table, and now I am told the warranty has run out because of the delay.",
     "worse", E),
    (None, "Riverside", "damaged_item", "Chest of drawers arrived with a dented corner.",
     "They swapped the dented drawers within a few days.", "fixed", E),
    (None, "Online", "damaged_item", "Glass shelf was chipped out of the box.",
     "Friendly driver this time.", NM, T),
    (1, "Online", "app_login", None,
     "Since the update I stay logged in and my points are all there.", "fixed", E),
    (3, "Online", "app_login", None, "I gave up on the app, still cannot sign in.", "still_happening", E),
    (6, "Online", "app_login", None,
     "Now it has locked me out completely and wiped my points balance.", "worse", E),
    (7, "Downtown", "app_login", None,
     "No longer asks for my password every visit, much better.", "fixed", P),
    (None, "Riverside", "app_login", "The app throws me out every time I open the rewards tab.",
     "The store smelled lovely, nice candles.", NM, E),
]

# ── Survey Coach (before launch) and question type pick ─────────────────────
# (question, answer options, wording problem, question type, difficulty)
SURVEY_QUESTIONS: list[tuple[str, str, str, str, str]] = [
    ("How great was our friendly staff?", "1 to 5 stars", "leading", "rating", E),
    ("Was the store clean and easy to navigate?", "Yes; No", "double_barrelled", "yes_no", E),
    ("How long have you been a customer?", "0-5 years; 5-10 years; 10+ years", "overlapping_options", "multiple_choice", E),
    (f"How likely are you to recommend {COMPANY} to a friend or colleague?", "0 to 10", "fine", "nps", E),
    ("Do you shop with us regularly?", "Yes; No", "vague_time_frame", "yes_no", E),
    ("How would you rate the omnichannel fulfilment experience of your BOPIS order?", "1 to 5", "jargon", "rating", E),
    ("What is your annual household income?", "Under 30k; 30k-60k; 60k-100k; Over 100k", "sensitive", "multiple_choice", E),
    ("How would you rate our staff?", "1 to 5 stars", "fine", "rating", E),
    ("Don't you agree that our new store layout is much better?", "Yes; No", "leading", "yes_no", E),
    ("How satisfied are you with the price and quality of your purchase?", "1 to 5", "double_barrelled", "rating", E),
    ("Which of these did you use today?", "Click and collect; Design desk; Cafe; Returns counter", "fine", "multiple_choice", E),
    (REASON_QUESTION, "(text box)", "fine", "open_text", E),
    ("Have you visited us recently?", "Yes; No", "vague_time_frame", "yes_no", E),
    ("What is your age?", "Under 18; 18-25; 25-35; 35-50; 50+", "overlapping_options", "multiple_choice", T),
    ("Please rate each of the following aspects of your visit.",
     "Rows: Staff, Cleanliness, Range, Checkout. Scale: Poor to Excellent", "fine", "matrix", E),
    ("Do you have any health conditions that affect how you shop?", "Yes; No", "sensitive", "yes_no", E),
    ("How did our award-winning delivery service exceed your expectations?", "(text box)", "leading", "open_text", P),
    ("Did the SKU availability on the PDP match in-store inventory?", "Yes; No", "jargon", "yes_no", E),
    ("Was your order delivered on the date you were given?", "Yes; No", "fine", "yes_no", E),
    ("How easy was it to find what you needed and pay for it?", "1 to 5", "double_barrelled", "rating", P),
    ("How much do you spend with us?", "Under $50; $50 to $100; Over $100", "vague_time_frame", "multiple_choice", T),
    ("How many people live in your household?", "1-2; 2-4; 4 or more", "overlapping_options", "multiple_choice", T),
    ("Is there anything else you would like to tell us?", "(text box)", "fine", "open_text", E),
    ("How satisfied were you with the assembly service?", "Very dissatisfied to Very satisfied", "fine", "rating", E),
    ("What is your religion?", "Christian; Muslim; Hindu; Jewish; None; Other", "sensitive", "multiple_choice", E),
    ("How would you rate the following: delivery speed, packaging, driver courtesy?",
     "Scale: Poor to Excellent for each", "fine", "matrix", P),
    ("Considering our CSAT uplift initiatives, how was your CX?", "1 to 5", "jargon", "rating", E),
    ("Which store did you visit?", "Northgate; Riverside; Downtown", "fine", "multiple_choice", E),
]

# ── Invitation hold: latest Activity note on a contact ──────────────────────
ACTIVITY_NOTES: list[tuple[str, str, str]] = [
    ("Renewed annual trade account, no issues raised.", "send", E),
    ("Called to ask about opening hours, answered.", "send", E),
    ("Order delivered and signed for.", "send", E),
    ("Attended the spring design workshop.", "send", E),
    ("Updated billing address.", "send", E),
    ("Complaint from March closed, customer confirmed they were happy with the replacement.", "send", T),
    ("Bought a gift card in store.", "send", E),
    ("Requested a catalogue by post.", "send", E),
    ("Complaint escalated to regional manager, awaiting response.", "hold", E),
    ("Customer's husband passed away last week, order on pause at her request.", "hold", T),
    ("Refund dispute open with card provider.", "hold", E),
    ("Customer in hospital until end of month, delivery rescheduled.", "hold", E),
    ("Replacement sofa on back order, customer chasing weekly.", "hold", P),
    ("Called very upset about damaged delivery, case open with claims team.", "hold", E),
    ("On maternity leave, asked us to get back in touch in the new year.", "hold", P),
    ("Engineer visit booked for Tuesday to repair the recliner.", "hold", P),
    ("Account under review after a payment failed twice.", "hold", P),
    ("Says she is travelling for six weeks and cannot receive deliveries.", "hold", P),
    ("Customer deceased, account to be closed by family.", "dont_send", E),
    ("Asked not to be contacted for any reason other than open orders.", "dont_send", E),
    ("Solicitor's letter received regarding injury claim in store, all contact via legal.", "dont_send", E),
    ("Threatened to report us for harassment if we email again.", "dont_send", P),
    ("Account closed at customer's request, said remove all my details.", "dont_send", E),
    ("Number belongs to someone else now, previous owner moved abroad.", "dont_send", T),
]

# ── Free text to category ───────────────────────────────────────────────────
JOB_TITLES: list[tuple[str, str, str]] = [
    ("Sr. Eng. Manager", "senior_management", P),
    ("Asst. Director of Ops", "director", P),
    ("freelance", "cant_tell", E),
    ("Software Engineer II", "individual_contributor", E),
    ("VP Sales, EMEA", "executive", P),
    ("CFO", "executive", E),
    ("Team Lead, Customer Support", "manager", E),
    ("Store Mgr", "manager", P),
    ("Head of Procurement", "senior_management", E),
    ("Dir. of Marketing", "director", P),
    ("Founder & CEO", "executive", E),
    ("Nurse", "individual_contributor", E),
    ("Accounts Payable Clerk", "individual_contributor", E),
    ("retired", "cant_tell", E),
    ("Managing Director", "executive", T),
    ("n/a", "cant_tell", E),
    ("Shift Supervisor", "manager", E),
    ("Senior Analyst", "individual_contributor", T),
]
INDUSTRIES: list[tuple[str, str, str]] = [
    ("NHS trust", "healthcare", T),
    ("dental practice", "healthcare", E),
    ("primary school", "education", E),
    ("fintech startup", "financial_services", T),
    ("SaaS", "technology", P),
    ("car parts factory", "manufacturing", E),
    ("boutique hotel", "hospitality", E),
    ("city council", "public_sector", E),
    ("insurance broker", "financial_services", E),
    ("coffee shop", "hospitality", P),
    ("online clothing store", "retail", E),
    ("uni", "education", P),
    ("self employed", "cant_tell", E),
    ("logistics", "cant_tell", T),
    ("credit union", "financial_services", P),
]

# ── Identity warning: anonymous employee survey ─────────────────────────────
EX_COMMENTS: list[tuple[str, str, str]] = [
    ("As the only night-shift supervisor at the Riverside warehouse, I never get a proper break.", "identifies_writer", E),
    ("I am the one who raised the forklift incident last month and nothing has changed.", "identifies_writer", P),
    ("Being the sole wheelchair user in the Downtown store, the staff room is impossible for me.", "identifies_writer", E),
    ("I joined the design desk at Northgate three weeks ago and have had no training.", "identifies_writer", P),
    ("Since I came back from maternity leave in June my hours in visual merchandising were cut.", "identifies_writer", P),
    ("As the longest-serving member of the returns team, twenty-two years, I think we deserve better.", "identifies_writer", E),
    ("I am the only French speaker in customer care so every French call lands on me.", "identifies_writer", E),
    ("After my transfer from Leeds to the Downtown finance team I feel left out.", "identifies_writer", P),
    ("Darren in dispatch shouts at the temps and nobody stops him.", "names_someone_else", E),
    ("The Northgate store manager plays favourites with the rota.", "names_someone_else", P),
    ("Priya Shah deserves a promotion, she carries the whole team.", "names_someone_else", E),
    ("My line manager, the head of visual merchandising, takes credit for our work.", "names_someone_else", T),
    ("The new HR business partner for Riverside ignores every email.", "names_someone_else", P),
    ("Tom from IT fixed my laptop in ten minutes, give him a raise.", "names_someone_else", E),
    ("Our regional director made a sexist joke at the town hall.", "names_someone_else", P),
    ("The Saturday shift lead on tills at Downtown lets her friends leave early.", "names_someone_else", P),
    ("We need more staff on weekends.", "safe", E),
    ("Pay has not kept up with the cost of living.", "safe", E),
    ("The break room microwave has been broken for ages.", "safe", E),
    ("Managers should communicate rota changes earlier.", "safe", E),
    ("I enjoy working here, the team is supportive.", "safe", E),
    ("Training on the new till system was rushed.", "safe", E),
    ("Some supervisors could listen more.", "safe", T),
    ("More flexible hours would help those of us with kids.", "safe", T),
]

# ── Replies to invitation emails ────────────────────────────────────────────
INVITATION_REPLIES: list[tuple[str, str, str]] = [
    ("Please remove me from your mailing list.", "unsubscribe", E),
    ("STOP", "unsubscribe", E),
    ("I never signed up for this, stop emailing me.", "unsubscribe", E),
    ("Take me off whatever list this is.", "unsubscribe", P),
    ("Not interested, do not contact me again.", "unsubscribe", E),
    ("I'm away until Monday but please stop sending these.", "unsubscribe", T),
    ("I think you have the wrong address, I have never shopped with you.", "wrong_person", E),
    ("Sarah left the company in May, this inbox is now monitored by the office team.", "wrong_person", P),
    ("This is a shared mailbox for accounts, not the person you want.", "wrong_person", P),
    ("You want my father, same name. His address is different.", "wrong_person", P),
    ("No idea who Mr Okafor is, this is not him.", "wrong_person", E),
    ("I am out of the office until 14 October with limited access to email.", "out_of_office", E),
    ("Thank you for your message. I am on annual leave and will reply on my return.", "out_of_office", E),
    ("Auto-reply: currently on parental leave. For urgent matters contact reception.", "out_of_office", E),
    ("Automatic reply: I am travelling this week and responses will be delayed.", "out_of_office", E),
    ("Thanks for your email! Our office is closed for the public holiday.", "out_of_office", P),
    ("I would rather just tell you here: the delivery was two weeks late and nobody called.", "feedback", E),
    ("Survey link aside, your Riverside team was wonderful, please pass that on.", "feedback", E),
    ("Not filling in a form. The sofa is falling apart after two months, that is my feedback.", "feedback", E),
    ("Everything was fine, nothing to report.", "feedback", P),
    ("Honestly your prices have gone up too much and I will shop elsewhere.", "feedback", T),
    ("The link does not open on my phone, is there another way to respond?", "question", E),
    ("How long does the survey take?", "question", E),
    (f"Is this email genuinely from {COMPANY}? It looks like phishing.", "question", P),
    ("Will my answers be anonymous?", "question", E),
]

# ── Question definitions ────────────────────────────────────────────────────
ISSUES: dict[str, str] = {
    "late_delivery": "order arrived after the promised date, or the whole order is overdue",
    "billing_error": "charged the wrong amount, charged twice, or the invoice or statement is wrong",
    "damaged_item": "item arrived broken, cracked, dented or chipped",
    "app_login": "cannot sign in to the loyalty app, or keeps being logged out",
}

# id -> (feature, source file, expected column, state fields, instructions, classes)
TASKS: dict[str, dict[str, Any]] = {
    "vq_loyalty_app": {
        "feature": "Virtual Questions", "source": "comments.csv", "expected": "vq_loyalty_app",
        "instructions": f"Did the respondent mention the {COMPANY} loyalty app?",
        "classes": {
            "yes_negative": "mentions the loyalty or rewards app and is unhappy with it",
            "yes_positive": "mentions the loyalty or rewards app and is happy with it",
            "not_mentioned": "does not talk about the loyalty or rewards app",
        }},
    "coach_blind_spot": {
        "feature": "Survey Coach (after launch)", "source": "comments.csv", "expected": "coach_question",
        "instructions": "Which survey question is this comment mainly about?",
        "classes": {
            "staff": "How would you rate our store staff?",
            "delivery": "How would you rate the delivery of your order?",
            "product": "How would you rate the quality of the product?",
            "billing": "How easy was it to pay and get your invoice or refund?",
            "none_of_these": "about something no listed question asks, or not a real answer",
        }},
    "owner_team": {
        "feature": "Feedback Owners", "source": "comments.csv", "expected": "owner_team",
        "instructions": "Which team should own this comment?",
        "classes": {
            "billing": "invoices, charges, refunds, payment, prices at the till",
            "delivery": "couriers, delivery dates, tracking, items damaged or missing on arrival",
            "store_staff": "behaviour, helpfulness or availability of store employees",
            "facilities": "car park, building, lifts, lighting, signage",
            "digital": "website, loyalty app, login, online account",
            "product": "product quality, defects, assembly, how the item matches its description",
            "none": "nothing a team could act on, or not a real answer",
        }},
    "callback_offer": {
        "feature": "Callback offer", "source": "comments.csv", "expected": "callback",
        "instructions": "Does this comment describe a problem that is still unresolved for the person?",
        "classes": {
            "unresolved": "a problem of their own that is still open: waiting for a refund, item, fix or reply",
            "not_unresolved": "no open problem: praise, a past annoyance, a general opinion, or already resolved",
        }},
    "fix_issue": {
        "feature": "Fix Tracker (decision 1)", "source": "comments.csv", "expected": "issue",
        "instructions": "Which known issue does this comment report?",
        "classes": {**ISSUES, "none": "no complaint, or a complaint about something else"}},
    "fix_status": {
        "feature": "Fix Tracker (decision 2)", "source": "fix_tracker_followups.csv", "expected": "expected_status",
        "instructions": "Earlier this customer reported: {issue}. According to this new answer, what is the state of that problem now?",
        "classes": {
            "fixed": "says the earlier problem has been resolved, or did not happen this time",
            "still_happening": "says the same problem is continuing or happened again",
            "worse": "says the problem has grown or caused further harm",
            "not_mentioned": "does not talk about the earlier problem",
        }},
    "coach_wording": {
        "feature": "Survey Coach (before launch)", "source": "survey_questions.csv", "expected": "expected_flaw",
        "instructions": "Does this survey question have a wording problem?",
        "classes": {
            "leading": "wording pushes the respondent toward a particular answer",
            "double_barrelled": "asks about two different things in one question",
            "vague_time_frame": "unclear period or frequency, such as 'recently' or 'regularly'",
            "jargon": "uses internal terms or abbreviations a respondent may not know",
            "sensitive": "asks for private information such as income, health or religion",
            "overlapping_options": "answer options overlap or leave gaps",
            "fine": "clear, neutral, one topic, and the options fit",
        }},
    "question_type": {
        "feature": "Question type pick", "source": "survey_questions.csv", "expected": "expected_type",
        "instructions": "Which question type fits this survey question best?",
        "classes": {
            "rating": "rate one thing on a scale",
            "nps": "likelihood to recommend, from 0 to 10",
            "multiple_choice": "pick from a list of options",
            "matrix": "rate several items on the same scale",
            "open_text": "answer in the respondent's own words",
            "yes_no": "a yes or no answer",
        }},
    "invitation_hold": {
        "feature": "Invitation hold", "source": "activity_notes.csv", "expected": "expected_decision",
        "instructions": "Given this latest note on the contact, should a survey invitation go out now?",
        "classes": {
            "send": "nothing in the note makes this a bad time",
            "hold": "a temporary situation makes this a bad time: open complaint, dispute, illness, bereavement, away",
            "dont_send": "the contact should not be surveyed at all: deceased, asked for no contact, legal action, wrong person",
        }},
    "seniority": {
        "feature": "Free text to category", "source": "free_text_profile.csv", "expected": "expected_category",
        "instructions": "Which seniority level does this job title belong to?",
        "classes": {
            "individual_contributor": "does the work directly, no people management",
            "manager": "manages a team, shift or store",
            "senior_management": "senior manager or head of a function",
            "director": "director level, including assistant or associate director",
            "executive": "C-level, vice president, managing director, owner or founder",
            "cant_tell": "the answer does not show a level, or is not a job title",
        }},
    "industry": {
        "feature": "Free text to category", "source": "free_text_profile.csv", "expected": "expected_category",
        "instructions": "Which industry does this answer describe?",
        "classes": {
            "retail": "shops and online stores",
            "healthcare": "hospitals, clinics, medical and dental care",
            "education": "schools, colleges and universities",
            "financial_services": "banks, insurance, lending and payments",
            "technology": "software and IT services",
            "manufacturing": "factories and production",
            "hospitality": "hotels, restaurants and cafes",
            "public_sector": "government and local authorities",
            "cant_tell": "none of these, or not an industry",
        }},
    "identity_warning": {
        "feature": "Identity warning", "source": "ex_comments.csv", "expected": "expected_label",
        "instructions": "Could this anonymous employee comment reveal who a specific person is?",
        "classes": {
            "identifies_writer": "a unique role, shift, location, tenure or event could reveal who wrote it",
            "names_someone_else": "names or clearly points to another specific person",
            "safe": "no specific person could be identified",
        }},
    "invitation_reply": {
        "feature": "Invitation replies", "source": "invitation_replies.csv", "expected": "expected_label",
        "instructions": "What kind of reply to a survey invitation email is this?",
        "classes": {
            "unsubscribe": "asks to stop receiving emails or to be removed",
            "wrong_person": "says the email reached the wrong person, or the person has left",
            "out_of_office": "automatic reply saying the person is away",
            "feedback": "gives their opinion or experience instead of taking the survey",
            "question": "asks about the survey or how to take it",
        }},
}

FREE_TEXT_QUESTIONS = {"seniority": "What is your job title?", "industry": "What industry do you work in?"}


# ── build ───────────────────────────────────────────────────────────────────

def _contact_id(comment_no: int) -> str:
    return f"C{1000 + comment_no}"


def _comment_rows() -> list[dict[str, Any]]:
    rows = []
    for n, (branch, nps, text, vq, coach, blind, team, callback, issue, difficulty) in enumerate(COMMENTS, start=1):
        rows.append({"comment_id": f"CM{n:03d}", "contact_id": _contact_id(n), "branch": branch, "nps": nps,
                     "text": text, "vq_loyalty_app": vq, "coach_question": coach, "blind_spot_topic": blind,
                     "owner_team": team, "callback": callback, "issue": issue, "difficulty": difficulty})
    return rows


def _followup_rows(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows, extra = [], 0
    for n, (comment_no, branch, issue, earlier, text, status, difficulty) in enumerate(FOLLOWUPS, start=1):
        if comment_no is None:
            extra += 1
            contact, comment_id, earlier_text = f"C{2000 + extra}", "", earlier
        else:
            source = comments[comment_no - 1]
            assert source["issue"] == issue and source["branch"] == branch, f"FOLLOWUPS row {n} does not match CM{comment_no:03d}"
            contact, comment_id, earlier_text = source["contact_id"], source["comment_id"], source["text"]
        rows.append({"followup_id": f"FU{n:03d}", "contact_id": contact, "branch": branch,
                     "original_comment_id": comment_id, "stored_issue": issue, "original_text": earlier_text,
                     "new_text": text, "expected_status": status, "difficulty": difficulty})
    return rows


def _simple_rows(prefix: str, text_key: str, expected_key: str, data: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [{"row_id": f"{prefix}{n:03d}", text_key: text, expected_key: expected, "difficulty": difficulty}
            for n, (text, expected, difficulty) in enumerate(data, start=1)]


def _question_rows() -> list[dict[str, Any]]:
    return [{"row_id": f"SQ{n:03d}", "question_text": text, "answer_options": options,
             "expected_flaw": flaw, "expected_type": qtype, "difficulty": difficulty}
            for n, (text, options, flaw, qtype, difficulty) in enumerate(SURVEY_QUESTIONS, start=1)]


def _profile_rows() -> list[dict[str, Any]]:
    rows = []
    for field, data in (("job_title", JOB_TITLES), ("industry", INDUSTRIES)):
        for typed, expected, difficulty in data:
            rows.append({"row_id": f"FT{len(rows) + 1:03d}", "field": field, "typed_answer": typed,
                         "expected_category": expected, "difficulty": difficulty})
    return rows


def _wire(task_id: str, instructions: Optional[str] = None) -> dict[str, dict]:
    task = TASKS[task_id]
    return {task_id: {"type": "choice", "instructions": instructions or task["instructions"],
                      "criteria": task["classes"]}}


def _request(row_id: str, task_id: str, state: dict[str, Any], expected: str, difficulty: str,
             instructions: Optional[str] = None) -> dict[str, Any]:
    assert expected in TASKS[task_id]["classes"], f"{row_id}: {expected!r} is not an option of {task_id}"
    return {"id": f"{row_id}:{task_id}", "state": state, "questions": _wire(task_id, instructions),
            "expected": expected, "difficulty": difficulty}


def _requests(tables: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in TASKS}
    for row in tables["comments"]:
        state = {"survey_question": REASON_QUESTION, "answer": row["text"], "metric_score": row["nps"]}
        for task_id in ("vq_loyalty_app", "coach_blind_spot", "owner_team", "callback_offer", "fix_issue"):
            out[task_id].append(_request(row["comment_id"], task_id, state, row[TASKS[task_id]["expected"]],
                                         row["difficulty"]))
    for row in tables["fix_tracker_followups"]:
        issue = f"{row['stored_issue'].replace('_', ' ')} ({ISSUES[row['stored_issue']]})"
        out["fix_status"].append(_request(
            row["followup_id"], "fix_status", {"survey_question": REASON_QUESTION, "answer": row["new_text"]},
            row["expected_status"], row["difficulty"],
            instructions=TASKS["fix_status"]["instructions"].format(issue=issue)))
    for row in tables["survey_questions"]:
        out["coach_wording"].append(_request(
            row["row_id"], "coach_wording",
            {"question": row["question_text"], "answer_options": row["answer_options"]},
            row["expected_flaw"], row["difficulty"]))
        out["question_type"].append(_request(
            row["row_id"], "question_type", {"question": row["question_text"]},
            row["expected_type"], row["difficulty"]))
    for row in tables["activity_notes"]:
        out["invitation_hold"].append(_request(
            row["row_id"], "invitation_hold", {"activity_note": row["activity_note"]},
            row["expected_decision"], row["difficulty"]))
    for row in tables["free_text_profile"]:
        task_id = "seniority" if row["field"] == "job_title" else "industry"
        out[task_id].append(_request(
            row["row_id"], task_id,
            {"survey_question": FREE_TEXT_QUESTIONS[task_id], "answer": row["typed_answer"]},
            row["expected_category"], row["difficulty"]))
    for row in tables["ex_comments"]:
        out["identity_warning"].append(_request(
            row["row_id"], "identity_warning", {"survey_question": EX_QUESTION, "answer": row["text"]},
            row["expected_label"], row["difficulty"]))
    for row in tables["invitation_replies"]:
        out["invitation_reply"].append(_request(
            row["row_id"], "invitation_reply", {"email_reply": row["text"]},
            row["expected_label"], row["difficulty"]))
    return out


def _option_tokens(task: dict[str, Any]) -> int:
    """Same estimate the gateway uses when a binding is saved."""
    return sum(1 + estimate_tokens(f"{k}: {v}") for k, v in task["classes"].items())


def _share(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def _ground_truth(tables: dict[str, list[dict[str, Any]]],
                  requests: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    comments, followups = tables["comments"], tables["fix_tracker_followups"]
    unasked = [r for r in comments if r["coach_question"] == NOQ]
    fix: dict[str, Any] = {}
    for issue in ISSUES:
        rows = [r for r in followups if r["stored_issue"] == issue]
        by_branch = {}
        for branch in sorted({r["branch"] for r in rows}):
            at = [r for r in rows if r["branch"] == branch]
            by_branch[branch] = {"followups": len(at),
                                 "fixed_share": _share(sum(r["expected_status"] == "fixed" for r in at), len(at))}
        fix[issue] = {"followups": len(rows), "status": dict(Counter(r["expected_status"] for r in rows)),
                      "fixed_share": _share(sum(r["expected_status"] == "fixed" for r in rows), len(rows)),
                      "by_branch": by_branch}
    return {
        "rows": {name: len(rows) for name, rows in tables.items()},
        "labels": {task_id: dict(Counter(r["expected"] for r in rows)) for task_id, rows in requests.items()},
        "difficulty": {task_id: dict(Counter(r["difficulty"] for r in rows)) for task_id, rows in requests.items()},
        "planted": {
            "blind_spots": {
                "comments": len(comments), "fit_no_question": len(unasked),
                "topics": {topic or "no_content": {"comments": n, "share_of_all": _share(n, len(comments))}
                           for topic, n in Counter(r["blind_spot_topic"] for r in unasked).items()}},
            "loyalty_app": {label: {"comments": n, "share_of_all": _share(n, len(comments))}
                            for label, n in Counter(r["vq_loyalty_app"] for r in comments).items()},
            "team_inbox": {team: {"comments": n, "detractor_comments": sum(
                               1 for r in comments if r["owner_team"] == team and r["nps"] <= 6)}
                           for team, n in Counter(r["owner_team"] for r in comments).items()},
            "callback_offers": {"unresolved": sum(r["callback"] == OPEN for r in comments),
                                "nps_0_to_6": sum(r["nps"] <= 6 for r in comments),
                                "unresolved_with_nps_7_plus": sum(
                                    r["callback"] == OPEN and r["nps"] >= 7 for r in comments)},
            "fix_tracker": fix,
        },
    }


def _write_csv(name: str, rows: list[dict[str, Any]]) -> None:
    with (OUT_DIR / name).open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(name: str, payload: Any) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    comments = _comment_rows()
    tables = {
        "comments": comments,
        "fix_tracker_followups": _followup_rows(comments),
        "survey_questions": _question_rows(),
        "activity_notes": [dict(row, contact_id=f"C{3000 + n}") for n, row in enumerate(
            _simple_rows("AN", "activity_note", "expected_decision", ACTIVITY_NOTES), start=1)],
        "free_text_profile": _profile_rows(),
        "ex_comments": _simple_rows("EX", "text", "expected_label", EX_COMMENTS),
        "invitation_replies": _simple_rows("IR", "text", "expected_label", INVITATION_REPLIES),
    }
    requests = _requests(tables)

    (OUT_DIR / "requests").mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        _write_csv(f"{name}.csv", rows)
    for task_id, rows in requests.items():
        with (OUT_DIR / "requests" / f"{task_id}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _write_json("tasks.json", [
        {"id": task_id, "type": "choice", "feature": task["feature"], "source": task["source"],
         "expected_column": task["expected"], "instructions": task["instructions"], "classes": task["classes"]}
        for task_id, task in TASKS.items()])
    truth = _ground_truth(tables, requests)
    _write_json("ground_truth.json", truth)

    budget = HEAD_BUDGET["english"] - MIN_INSTRUCTION_TOKENS
    print(f"Wrote {OUT_DIR}")
    for name, rows in tables.items():
        print(f"  {name + '.csv':<28} {len(rows):>3} rows")
    print("  task                 requests  options  option tokens (limit ~%d)" % budget)
    for task_id, task in TASKS.items():
        tokens = _option_tokens(task)
        flag = "" if tokens <= budget else "  OVER"
        print(f"  {task_id:<20} {len(requests[task_id]):>8} {len(task['classes']):>8} {tokens:>8}{flag}")
    print(json.dumps(truth["planted"], indent=2))


if __name__ == "__main__":
    main()
