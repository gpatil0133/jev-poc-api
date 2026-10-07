# Jev on our tasks: how well it answers, task by task

**Date:** 2026-10-07
**Model:** `jev-1.13.0` (the `jev-latest` alias on the TypeSafe API), called through the PoC gateway with `BACKEND=jev`
**Test set:** 1,000 invented texts in seven sets, one call each, 3,551 scored decisions

This replaces the first look of 2026-10-06, which used 190 examples (10 per task).
Section 8 says what changed between the two.

---

## 1. Summary

Every one of the 1,000 calls returned an answer. Jev gave the expected answer on
**3,457 of 3,551 decisions (97.4%)**, and got every question right on 916 of the
1,000 texts.

- **Alerts on comment meaning (scope 2):** 669 of 680 right. It caught every
  comment that should raise an alert (233 of 233). The 11 errors are all false
  alarms, 7 of them on "is a complaint".
- **Logic on text answers (scope 3):** 472 of 480 right, with no false alarm on any
  of the four topics. Speed is still the blocker: 116 of 1,000 calls came back
  inside the 300 ms live-page budget.
- **Tag suggestions (scope 6):** 75 of 75 answer options tagged correctly,
  including a 60-tag list. On questions it adds extra tags: 62 of 75 questions
  came back with exactly the right tags.
- **Builder hints (scopes 4, 5 and the new 9):** follow-up 148 of 150, personal
  data asked 147 of 150, sensitive 133 of 150. All 17 sensitive "errors" are
  questions that ask for personal data, which Jev also calls sensitive.
- **Personal data in answers (new scope 10):** 129 of 130 right, and every answer
  that contained personal data was recognised (83 of 83).
- **Template pick (scope 7):** 99 of 100.
- **Quiz scoring (scope 8):** much better than the first look, and still the
  weakest scope. Key points are right on 598 of 612 decisions; every error on a
  hard answer is credit for a point the answer did not make. The model's own
  grade is right on 163 of 180 answers.
- **The stricter key-point wording is not an improvement.** It cut wrongly credited
  points from 14 to 3 and then missed 15 points that were plainly stated.
- **Asking several questions in one call is safe and nearly free.** Answers were
  identical alone and together, the call took no longer, and each extra question
  added roughly 50 to 80 input tokens to a base of about 450.

What this supports: building scopes 2, 6, 7, 9 and 10 on Jev, and scope 3 once the
live-page budget is settled. What it does not support: any claim about real survey
data. Read section 2 before quoting a number from this document.

---

## 2. How far to trust these numbers

- **The texts are invented.** They were written to be varied (retail, healthcare,
  hotels, banking, software, employee surveys; some typos, some long, about 35 not
  in English), but they are not real responses. Expect lower scores on real data.
- **The texts and labels were written by an AI assistant and have not been reviewed
  by a person.** A sample of each set was spot-checked. Where a result turns on a
  labelling rule that could reasonably go the other way, section 4 says so.
- **The sets are sized for a first decision, not a benchmark.** On a question with
  150 decisions and about 95% right, the true figure could be about 4 points either
  side. Each table gives this margin. It is enough to separate strong from weak,
  not 95% from 97%.
- **Laya was not compared.** Only Jev was run.
- **Shared classes (scope 1) was left out on purpose.** It is switched off for now;
  its examples are kept and can be run with `--include-disabled`.

---

## 3. How the test was run

Each text is one `POST /v1/classify` call through the gateway, carrying every
question that applies to it. A comment in the alerts set is judged on all four
meanings in that one call; a quiz answer is judged on each key point (in two
wordings) and on its grade. The gateway's cache was off, so every answer and every
timing is a real call to Jev.

About 40% of each set is deliberately hard: idiom, sarcasm, negation, near misses
that use the trigger words without the meaning, and meaning that is implied but not
stated.

| Set | Scope | Texts | Scored on |
|---|---|---|---|
| Quiz answers | 8 | 180 | Each key point (two wordings), grade; 15 questions, 12 answers each |
| Comments for alerts | 2 | 170 | May leave, safety concern, complaint, wants a callback |
| Tag suggestions | 6 | 150 | A tag for an answer option (75), tags for a question (75) |
| Builder question wordings | 4, 5, 9 | 150 | Sensitive, asks for personal data, "why" follow-up |
| Answers with personal data | 10 | 130 | Kind of personal data, or none |
| Answers for logic | 3 | 120 | About price, delivery, staff, website or app |
| Create with AI prompts | 7 | 100 | Project type |

The gateway's default thresholds were left alone: an answer at or above 0.85 is
banded `act` (0.90 for safety concern), at or above 0.60 `suggest`, below that
`none`. What the platform does depends on the band, so each decision is sorted
into an outcome:

| Outcome | Meaning | Count |
|---|---|---|
| Quiet, right | Nothing was due and nothing was raised | 2,236 |
| Acted, right | Band `act` and correct: applied automatically, and right | 1,158 |
| Offered, right | Band `suggest` and correct: shown for a person to accept | 57 |
| Offered, wrong | Band `suggest` and wrong: a person would have to reject it | 38 |
| Missed | Something was due, and the answer was below `suggest` | 33 |
| **Acted, wrong** | Band `act` and wrong: applied automatically, and wrong | **29** |

When Jev was confident enough for the platform to act on its own, it was right
1,158 times out of 1,187 (97.6%). The 29 wrong ones are spread over quiz grade (7),
question tags (6), sensitive (6), quiz key points (5), complaint (2) and three
single cases.

By difficulty: easy 98.0% (1,028 of 1,049), medium 99.0% (1,084 of 1,095), hard
95.6% (1,345 of 1,407).

These counts leave out the stricter key-point wording, which is an experiment
(section 4.7).

---

## 4. Results by scope

In the tables, "margin" is how far the accuracy could be off either way, in points.
"Caught" is how many of the cases that should be flagged were given the right
answer. "False alarms" is how many of the cases that should stay quiet were flagged.

### 4.1 Scope 2: alerts on comment meaning

| Meaning | Correct | Margin | Caught | False alarms | Hard tier |
|---|---|---|---|---|---|
| May leave | 169 / 170 (99.4%) | ±1.6 | 48 / 48 | 1 / 122 | 69 / 70 |
| Safety concern | 168 / 170 (98.8%) | ±1.9 | 47 / 47 | 2 / 123 | 69 / 70 |
| Complaint | 163 / 170 (95.9%) | ±3.1 | 91 / 91 | 7 / 79 | 68 / 70 |
| Wants a callback | 169 / 170 (99.4%) | ±1.6 | 47 / 47 | 1 / 123 | 69 / 70 |

- **Nothing that should alert was missed**, including the eight non-English
  comments and the conditional threats ("if the Sunday session gets dropped I'd
  have to look elsewhere").
- **Five of the seven complaint false alarms are churn statements with no stated
  problem** ("I will be switching to another bank."). The labels say leaving alone
  is not a complaint; Jev reads it as one. One of the five was banded `act`. If a
  rule should fire on complaints only, pair it with a minimum probability or accept
  that "I'm leaving" will also trip it.
- **The other false alarms are near misses:** "Cancelled my old provider the day
  your fibre went live" read as may-leave (0.87), "The lift has been broken for a
  month" read as a safety concern (0.80), and a joke about the chef calling with a
  recipe read as a callback request (0.74).

### 4.2 Scope 3: logic on text answers

| Topic | Correct | Margin | Caught | False alarms | Hard tier |
|---|---|---|---|---|---|
| Price | 117 / 120 (97.5%) | ±3.1 | 40 / 43 | 0 / 77 | 48 / 50 |
| Delivery | 120 / 120 (100%) | ±1.6 | 44 / 44 | 0 / 76 | 50 / 50 |
| Staff | 120 / 120 (100%) | ±1.6 | 45 / 45 | 0 / 75 | 50 / 50 |
| Website or app | 115 / 120 (95.8%) | ±3.8 | 38 / 43 | 0 / 77 | 48 / 50 |

- **No false alarm in 305 chances.** The traps held: "poor delivery" of a keynote,
  "Rich flavour, poor portion size", "It cost me an hour of my life".
- **The eight misses are secondary topics.** The app is mentioned in passing in a
  comment about a late parcel (5), or the price is an extra fee paid for a delivery
  that failed (3). The effect is a follow-up question that is not shown.
- **Speed still rules this scope out as it stands.** See section 5.

### 4.3 Scope 6: tag suggestions

| Task | Correct | Margin | Notes |
|---|---|---|---|
| Tag for an answer option | 75 / 75 (100%) | ±2.4 | 13 options that fit no tag were all left untagged |
| Tags for a question | 824 / 844 decisions (97.6%) | ±1.0 | 62 of 75 questions had exactly the right tags |

- **Answer options are the strong case.** Lists of 4, 8, 12, 25 and 60 tags all
  came back right, including near neighbours in the 60-tag furniture list ("Tall
  freestanding dressing mirror" to Floor Mirror, not Wall Mirror or Dressing Table).
- **On questions it over-tags.** It found 81 of the 82 expected tags and added 19
  that were not expected, 15 of them in the 20-tag category. "Were your invoices
  correct and charged on the right date?" got Billing plus Reliability, Pricing,
  Documentation and Reporting. Six extra tags were banded `act`.
- **What that means:** suggestions for answer options can be trusted as they are.
  For questions, show the suggestions for review, which is what the mock does, and
  expect the author to remove one now and then on long tag lists.

### 4.4 Scopes 4, 5 and 9: builder hints

| Question | Correct | Margin | Caught | False alarms | Hard tier |
|---|---|---|---|---|---|
| Sensitive (scope 4) | 133 / 150 (88.7%) | ±5.1 | 46 / 46 | 17 / 104 | 55 / 60 |
| Asks for personal data (scope 9) | 147 / 150 (98.0%) | ±2.5 | 46 / 48 | 1 / 102 | 57 / 60 |
| "Why" follow-up (scope 5) | 148 / 150 (98.7%) | ±2.2 | 41 / 43 | 0 / 107 | 59 / 60 |

- **Sensitive: all 17 false alarms are questions that ask for personal data.**
  Passport number, date of birth, home address, employee number. The labels treat
  those as personal data but not sensitive; Jev treats them as both. On the 102
  questions that ask for no personal data it made no error, and it caught all 46
  sensitive questions. Whether this is a fault depends on the product: a hint to
  use Encouraged Response on "What is your passport number?" is arguably right.
- **Sensitive and personal data are different questions and both are needed.**
  "What is your religion?" is sensitive and identifies nobody. The personal-data
  question also returns the kind (contact details, official ID and so on), which
  the sensitive question cannot.
- **Personal data asked: three misses.** Two questions that ask for someone else's
  name ("Who was your relationship manager at the branch?") came back as none. "In
  what year were you born?" was read as a date of birth.
- **Follow-up: no false alarm, two misses, the same blind spot as before.** "What
  could we have done to earn a higher rating from you?" and "What would it take to
  move your score up by one point?" were not recognised. A miss costs nothing: the
  suggestion does not appear.

### 4.5 Scope 10: personal data in answers

| Task | Correct | Margin | Caught | False alarms | Hard tier |
|---|---|---|---|---|---|
| Kind of personal data | 129 / 130 (99.2%) | ±2.0 | 83 / 83 | 1 / 47 | 46 / 47 |

- **Every answer with personal data got the right kind,** including an email
  written out as words, a card number run together with its expiry, and identifiers
  buried in long complaints in German, Italian and Hindi.
- **Four of the 83 were right but below 0.60,** so the mock would not flag them at
  the default threshold. Lower the threshold for this scope if a miss matters more
  than a false flag.
- **The traps held:** a sofa called Henrietta, "the manager" with no name, "I'm 34",
  a price and a model number were all left alone. The one false alarm is "My
  birthday is 14 March", read as a date of birth with no year given.
- **This scope sends the personal data itself to Jev.** See section 6, point 3.

### 4.6 Scope 7: Create with AI template pick

| Task | Correct | Margin | Hard tier |
|---|---|---|---|
| Project type | 99 / 100 (99.0%) | ±2.6 | 39 / 40 |

The one miss is "Feedback from candidates we interviewed", where Jev chose none of
the five types at 0.45. The platform keeps the generic template in that case, so
nothing wrong is applied. All 92 picks banded `act` were right.

### 4.7 Scope 8: quiz scoring

| Task | Correct | Margin | Notes |
|---|---|---|---|
| Key points, today's wording | 598 / 612 (97.7%) | ±1.2 | No stated point missed (213 / 213); 14 points credited that were not made |
| Key points, stricter wording | 594 / 612 (97.1%) | ±1.4 | 3 points wrongly credited; 15 stated points missed |
| Grade from the model | 163 / 180 (90.6%) | ±4.3 | Hard tier 49 / 63 |
| Grade worked out from today's key points | 174 / 180 (96.7%) | ±2.8 | Hard tier 58 / 63 |

- **Key points are far better than the first look suggested.** All points were
  right on 167 of 180 answers. Easy and medium answers are almost clean (398 of
  400 decisions).
- **The errors are still generous, and they are on the hard answers.** Twelve of
  the 14 wrongly credited points are on answers written to imply a point without
  stating it: "At the bottom of the fridge." credited with "below ready-to-eat
  food". Five were banded `act`. One is a plain reading error: "Not on the bottom
  shelf, that's for vegetables" was credited with "bottom shelf".
- **The stricter wording swaps one fault for another.** It asks "does the answer
  explicitly state this point" for each point. It stopped most false credit, and
  then refused points that were stated: "Within 30 days." was not credited with
  "within 30 days of purchase", and "It pumps blood." was not credited with "pumps
  blood around the body". A grader would see that as the tool being wrong more
  often, not less. It is not a replacement for today's wording.
- **The model's own grade is the weak part.** Of its 17 errors, 13 give more credit
  than the answer earned ("Leave the building." graded partly correct at 0.97), 2
  give less, and 2 call a non-answer a wrong answer. Seven were banded `act`.
- **Working the grade out from the key points beats asking for it.** All points
  found is correct, some is partly correct, none is incorrect. That rule matched
  the expected grade on 174 of 180 answers, against 163 for the model's grade. The
  mock already computes the suggested score this way; the label it shows should
  follow the same rule, with the model's grade kept only to tell a non-answer from
  a wrong one.

---

## 5. Speed and cost

### Speed

| Measure | Value |
|---|---|
| Median for one call (1,000 calls) | 323 ms |
| Fastest call | 265 ms |
| 95th percentile | 398 ms |
| 99th percentile | 553 ms |
| Slowest call | 868 ms |
| Calls inside 300 ms | 116 of 1,000 |
| Calls inside 800 ms | 999 of 1,000 |

These are round trips from a developer machine to the TypeSafe API over the
internet. Almost all of the time is the call to Jev; the gateway adds a few
milliseconds.

**The number of questions in a call does not change the time.** Calls with one
question had a median of 321 ms; calls with 20 questions, 333 ms. So the cost of
speed is per call, and a scope that needs several answers about one text should
ask for them together.

Scope 3 needs an answer on the live survey page, and the mock's budget is 300 ms.
About one call in nine met it. One of these has to change before scope 3 can work
on Jev:

- a larger budget on Next (800 ms would have covered all but one call here);
- classifying the answer while the participant is still on the page;
- or a faster route to the model than the public API from this network.

### Cost

Jev bills on input tokens. The run used 696,535 input tokens for 1,000 calls.

| Set | Questions per call | Input tokens per call |
|---|---|---|
| Create with AI prompts | 1 | 459 |
| Answers with personal data | 1 | 521 |
| Comments for alerts | 4 | 609 |
| Answers for logic | 4 | 608 |
| Builder question wordings | 3 | 661 |
| Tag suggestions | 6.1 on average | 829 |
| Quiz answers | 7.8 on average | 1,016 |

- **A call costs about 450 to 500 tokens before it asks anything useful,** and each
  further question adds roughly 50 to 80. Four alert meanings in one call cost 609
  tokens; four separate calls would cost about 1,900.
- **Long tag lists on questions are the expensive case.** The largest call was
  1,473 tokens, for a question checked against a 20-tag list (one yes/no per tag).
- To turn this into money, multiply by the contract price per token: 1,000 calls
  of this mix are about 0.7 million input tokens. The script takes
  `--usd-per-mtok` and writes the cost per set.

### Asking questions together

Twenty quiz answers were also sent with each part on its own (today's key points,
the stricter key points, the grade) and compared with the combined call. All 160
decisions were the same, and the largest difference in probability was 0.06. Jev
answers each question independently of the others in the call.

---

## 6. What this means for each scope

| Scope | Accuracy on this set | Speed | Reading |
|---|---|---|---|
| 1 Shared classes | Not run | | Switched off for now; examples kept |
| 2 Alerts on comment meaning | Strong; nothing missed, 11 false alarms in 680 | Fits the 2 s submit budget | Ready to try on real comments. Decide whether "I'm leaving" should count as a complaint |
| 3 Logic on text answers | Strong; no false alarm | **Does not fit 300 ms** | Blocked on the time budget, not on the model |
| 4 Sensitive-question hints | Catches every sensitive question; also flags questions that ask for personal data | Fits the builder | Ready to try; decide how it should sit next to scope 9 |
| 5 Follow-up flag | Strong; misses "what would make it higher" wordings | Fits the builder | Ready to try; a miss costs nothing |
| 6 Tag suggestions | Answer options strong; questions get extra tags on long lists | Fits a batch | Ready to try with author review, as designed |
| 7 Create with AI template pick | Strong | Fits the builder | Ready to try |
| 8 Quiz scoring | Key points good, generous on answers that only imply a point; grade weaker | Fits a batch | Usable as a draft for a grader if the grade is worked out from the key points. Not ready to act without one |
| 9 Personal data asked (new) | Strong | Fits the builder | Ready to try |
| 10 Personal data in answers (new) | Strong | Fits the submit budget | Works, but blocked on the data question below |

What to do next:

1. **Test on real comments.** Take 200 to 300 real, de-identified comments per
   scope, have two people label them, and rerun the same script. Scopes 2 and 6
   first.
2. **Decide the live-page budget for scope 3.** This is a product decision and does
   not depend on more testing.
3. **Settle what may be sent to Jev before any real data goes.** Jev is an external
   service and nothing is masked before text leaves the network. Scope 10 makes
   this sharper: its whole job is to read answers that contain personal data.
4. **Change how the quiz grade is produced.** Derive it from the key points. Keep
   today's key-point wording; the stricter one is worse. If false credit on
   implied points matters, the next thing to try is a description per key point
   written by the quiz author, not a stricter instruction.
5. **Have a person review the labels** of the hard tier, starting with the two
   rules that drove results here: "leaving is not a complaint" and "an ID number is
   not sensitive".
6. **Compare with Laya** using the same script, once its inference is working.

---

## 7. Reproducing this

The texts are in `simulators/fixtures/task_eval/` (one `.jsonl` file per set). The
questions each set asks are in `simulators/task_eval_set.py`, and the scoring
script is `simulators/eval_task_quality.py`.

```bash
# gateway with BACKEND=jev, TYPESAFE_API_KEY set, and the cache off
CACHE_MAX_ENTRIES=0 uvicorn app.api.main:app --port 8010
python -m simulators.eval_task_quality --base-url http://127.0.0.1:8010 --label jev --split-check 20
```

It writes to `simulators/out/`:

- `task_quality_jev.csv`: one row per decision, with the expected answer, Jev's
  answer, its confidence, the band and the outcome.
- `task_quality_jev_summary.csv`: one row per question and difficulty tier.
- `task_quality_jev_calls.csv`: one row per call, with the time taken and tokens.
- `task_quality_jev_sets.csv`: one row per set, with latency and token totals.
- `task_quality_jev_split_check.csv`: the alone-versus-together comparison.

`--sets quiz,alerts` runs some sets only, `--limit 5` runs the first few texts of
each, and `--include-disabled` adds the parked sets.

---

## 8. What changed since the first look

| | 2026-10-06 | This run |
|---|---|---|
| Texts | 190, 10 per task | 1,000, 100 to 180 per set |
| Calls | One question per call | Every applicable question in one call |
| Difficulty | "Awkward" flag on about 4 in 10 | Easy, medium, hard; results by tier |
| Scopes | 1 to 8, plus four other gateway tasks | 2 to 8, plus personal data (9 and 10); scope 1 and the other gateway tasks parked |
| Cost | Not measured | Input tokens per call |

Findings that held: alerts and logic are strong; follow-up misses "what would earn
a higher rating" wordings; no call fits 300 ms.

Findings that moved: quiz key points looked weak on ten answers (7 of 10) and are
right on 167 of 180 here, with the same kind of error on the hardest answers. The
first look suggested a stricter wording; this run tried it and it did not help.

The gateway now returns the token counts Jev reports (`usage` on the classify
response), which is where the cost figures come from.
