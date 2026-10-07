# Jev on our tasks: how well it answers, task by task

**Date:** 2026-10-06
**Model:** `jev-1.13.0` (the `jev-latest` alias on the TypeSafe API), called through the PoC gateway with `BACKEND=jev`
**Test set:** 19 tasks, 10 hand-labelled examples each, 190 calls

---

## 1. Summary

On this small set Jev gave the expected answer on **184 of 190 examples (97%)**, and
every call returned an answer.

- **The ten ready-made gateway tasks:** 99 of 100 correct.
- **The nine questions the Design scopes ask:** 85 of 90 correct. All five misses
  are in three places: quiz scoring (four), and one follow-up question it did not
  recognise.
- **Deliberately awkward examples** (idioms, sarcasm, near misses): 42 of 44 correct.
- **Confidence was usable.** When Jev was confident enough for the platform to act
  on its own, it was right 122 times out of 125. The three wrong ones are all in
  one task, quiz key points.
- **Speed is the constraint, not accuracy.** A call took about 340 ms and never
  less than 304 ms. That is fine at submit and in the builder, and too slow for
  the 300 ms budget on the live survey page.

What this supports: the meaning-based features (alerts, tagging, builder hints,
template pick) look worth building on Jev. Quiz scoring needs prompt work before a
grader would trust it. Logic on text answers works on accuracy and fails on the
live-page time budget as it is set today.

What it does not support: any claim about real survey data. Read section 2 before
quoting a number from this document.

---

## 2. How far to trust these numbers

- **Ten examples per task.** One answer moves a task's score by 10 points. "10 of
  10" means "no problem showed up in ten tries", not "100% accurate".
- **The examples are invented and tidy.** They are short, in English and correctly
  spelled. Real comments are longer, messier, multilingual and often about several
  things at once. Expect lower scores on real data.
- **One labeller.** The expected answers are one person's judgement. A few are
  arguable, and those are named in section 4.
- **The examples were written by someone who knew the task wording.** That tends to
  produce examples a model finds easy.
- **Laya was not compared.** The Laya gateway failed every inference call during
  this work (`500: inference failed`), so there is no Laya column.

The next step that would turn this into evidence is in section 6.

---

## 3. How the test was run

Each example is one `POST /v1/classify` call through the gateway, one task per
call, with a 20 s timeout so that slow answers are measured, not cut off. The
gateway's default thresholds were left alone: an answer at or above 0.85 is banded
`act` (0.90 for safety concern and urgent), at or above 0.60 `suggest`, and below
that `none`.

What the platform does depends on the band, so each answer is sorted into an outcome:

| Outcome | Meaning | Count |
|---|---|---|
| Acted, right | Band `act` and correct: applied automatically, and right | 122 |
| Quiet, right | Nothing was due and nothing was raised | 59 |
| Offered, right | Band `suggest` and correct: shown for a person to accept | 3 |
| **Acted, wrong** | Band `act` and wrong: applied automatically, and wrong | **3** |
| Offered, wrong | Band `suggest` and wrong: a person would have to reject it | 2 |
| Missed | Something was due, and Jev stayed quiet | 1 |

"Acted, wrong" is the costly one: an alert that should not fire, a tag applied in
error. All three are in quiz key points.

---

## 4. Results by task

"Awkward" is how many of the task's deliberately hard examples were right.

### 4.1 The gateway's ready-made tasks

| Task | Used for | Correct | Awkward | Acted (right / total) | Median ms |
|---|---|---|---|---|---|
| `comment.sentiment` | Tone of a comment; shared classes | 10 / 10 | 4 / 4 | 8 / 8 | 338 |
| `comment.response_type` | Complaint, suggestion, praise or question | 10 / 10 | 2 / 2 | 8 / 8 | 329 |
| `comment.quality` | Specific, vague, gibberish or no answer | 10 / 10 | 2 / 2 | 10 / 10 | 343 |
| `comment.actionable` | Something the organisation could fix | 10 / 10 | 3 / 3 | 5 / 5 | 336 |
| `comment.urgent` | Needs attention now | 10 / 10 | 2 / 2 | 5 / 5 | 339 |
| `meaning.may_leave` | Alert: the customer may leave | 9 / 10 | 3 / 4 | 5 / 5 | 325 |
| `meaning.safety_concern` | Alert: a health or safety risk | 10 / 10 | 3 / 3 | 5 / 5 | 321 |
| `meaning.complaint` | Alert or case: the comment is a complaint | 10 / 10 | 3 / 3 | 5 / 5 | 333 |
| `meaning.callback_request` | Alert: wants to be contacted | 10 / 10 | 3 / 3 | 5 / 5 | 368 |
| `column.field_type` | Import column mapping | 10 / 10 | none | 10 / 10 | 345 |

Things worth knowing:

- **Sarcasm and idiom were read correctly.** "Oh great, another price rise. Just
  what I wanted." came back negative. "The queue was killing me" and "Prices are
  dangerously close to your competitor's" were not flagged as safety concerns. "I
  feel safe shopping here" was not flagged either.
- **Mixed sentiment was separated from negative.** "Great selection, but the
  checkout queue was painfully slow" came back mixed.
- **The one miss, `meaning.may_leave`:** "I left the store without finding what I
  wanted." was read as a sign the customer may leave, at 0.75. That is band
  `suggest`, so an alert rule set to the default would not have fired. The label is
  arguable: a wasted trip is a mild churn signal.
- **Where it was right but cautious.** Two correct answers came back at `suggest`,
  not `act`: "I bought a kettle on Tuesday." as neutral (0.70), and the rhetorical
  "Why does nobody ever answer the phone at your store?" as a complaint (0.76).
  Lower confidence on the borderline cases is the behaviour we want.
- **Junk was handled.** "asdfgh" and "n/a" went to the escape option, so no class
  was forced on them.

### 4.2 The questions the Design scopes ask

| Task | Scope | Correct | Awkward | Acted (right / total) | Median ms |
|---|---|---|---|---|---|
| Answer is about price | 3 Logic on text answers | 10 / 10 | 1 / 1 | 5 / 5 | 337 |
| Answer is about delivery | 3 Logic on text answers | 10 / 10 | 2 / 2 | 5 / 5 | 339 |
| Question is sensitive | 4 Builder hints | 10 / 10 | none | 5 / 5 | 330 |
| Question is a "why" follow-up | 5 Follow-up flag | 9 / 10 | 2 / 3 | 4 / 4 | 332 |
| Tag for an answer option | 6 Tag suggestions | 10 / 10 | 6 / 6 | 8 / 8 | 351 |
| Tags for a question | 6 Tag suggestions | 10 / 10 | 3 / 3 | 9 / 9 | 349 |
| Project type of a prompt | 7 Create with AI | 10 / 10 | 3 / 3 | 10 / 10 | 344 |
| Key points in a quiz answer | 8 Quiz scoring | **7 / 10** | none | **2 / 5** | 337 |
| Grade for a quiz answer | 8 Quiz scoring | 9 / 10 | none | 8 / 8 | 351 |

Things worth knowing:

- **Scope 3, topics.** All three paraphrases from the scope page ("too expensive",
  "costs went up", "not worth the money") were recognised as being about price,
  which is exactly what today's `Contains "price"` rule cannot do. The traps held
  too: "Priceless customer service" was not about price, and "They failed to
  deliver on their promises" was not about delivery.
- **Scope 4, sensitive questions.** Income, health, religion, sexual orientation
  and criminal record were flagged; five everyday questions were not.
- **Scope 5, follow-up flag.** The one miss is "What could we have done to earn a
  higher rating?", which Jev said is not a "why" follow-up (0.77). The effect is a
  suggestion that does not appear. Nothing wrong is applied.
- **Scope 6, tags.** Synonyms worked without any shared word: "City Centre Suites"
  to Downtown, "Terminal 2 Express Hotel" to Airport, "Seaview Sands" to
  Beachfront. "Mountain Lodge" and "Not sure / can't remember" correctly got no
  tag. A question covering two departments ("check-out and the cleanliness of the
  lobby") got both tags, and "How likely are you to recommend us?" got none.
- **Scope 7, template pick.** Ten of ten, each at full confidence, including the
  less obvious "Onboarding knowledge test for new cashiers" (Assessment) and "Which
  logo do you prefer, A or B?" (Poll).
- **Scope 8, quiz scoring.** This is the weak spot.
  - *Key points.* Jev never missed a point that was present, and it gave no credit
    to wrong or empty answers. But on all three partial answers it **credited
    points the answer did not make**: "At the bottom of the fridge." was credited
    with "below ready-to-eat food", and "Underneath salads and cooked meats." was
    credited with all three points. Counted as single decisions that is 26 right
    of 30, and every error is in the generous direction.
  - *Grade.* Nine of ten. "Keep it covered." was graded Partly correct when it
    covers none of the points, at 0.76, so it would be offered to the grader, not
    applied.
  - *Why it matters less than it sounds.* A grader confirms every grade, so nothing
    wrong becomes final. But a grader who learns the suggestions are generous will
    stop trusting them, which removes the point of the feature.

---

## 5. What this means for each scope

| Scope | Accuracy on this set | Speed | Reading |
|---|---|---|---|
| 1 Shared classes | Strong (sentiment, response type) | Fits submit time | Ready to try on real comments |
| 2 Alerts on comment meaning | Strong; one arguable miss in 40 | Fits the 2 s submit budget | The clearest win. Runs after submit, so speed is no issue |
| 3 Logic on text answers | Strong | **Does not fit 300 ms** | Blocked on the time budget, not on the model |
| 4 Sensitive-question hints | Strong | Fits the builder | Ready to try; low risk, the author decides |
| 5 Follow-up flag | Good; one miss | Fits the builder | Ready to try; a miss costs nothing |
| 6 Tag suggestions | Strong, including synonyms | Fits a batch | Ready to try; test on a real 500-tag category |
| 7 Create with AI template pick | Strong | Fits the builder | Ready to try |
| 8 Quiz scoring | Weak on partial answers | Fits a batch | Needs prompt work before a grader sees it |

### Speed

| Measure | Value |
|---|---|
| Median for one call, this run (190 calls) | 340 ms |
| Fastest call seen | 304 ms |
| 95th percentile, this run | 400 ms |
| Median in the earlier timing run (200 calls) | 350 ms |
| 95th percentile in the earlier timing run | 656 ms, with one call at 1.2 s |

These are round trips from a developer machine to the TypeSafe API over the
internet. Almost all of the time is the call to Jev; the gateway adds a few
milliseconds. Different tasks took about the same time, so the cost is per call,
not per task.

The scope page asks for an answer "in a fraction of a second" on the live survey
page, and the mock's budget is 300 ms. No Jev call came in under that. In the
Sogo-lite scenario run, 61.5% of live-page calls fell back to the default path for
this reason. Scope 3 needs one of these before it can work on Jev:

- a larger budget on Next (about 800 ms would cover nearly every call seen here);
- classifying the answer while the participant is still on the page, so the result
  is ready when they click Next;
- or a faster route to the model than the public API from this network.

---

## 6. What to do next

1. **Test on real comments.** Take 200 to 300 real, de-identified comments per
   task from a few surveys, have two people label them, and rerun the same script.
   This is the step that decides whether to build. The alert meanings and the
   shared classes should go first, since they scored best here and carry the most
   value.
2. **Decide the live-page budget for scope 3.** This is a product decision
   (section 5), and it does not depend on more testing.
3. **Rework the quiz key-point prompt.** Each key point is currently asked as "is
   the answer about X". Asking "does the answer explicitly state X" with a
   description of what does not count is the first thing to try. Then retest on
   more partial answers.
4. **Settle the two open issues before any real data is sent.** Jev is an external
   service: nothing is masked before text leaves the network, and calls are billed
   per input token.
5. **Compare with Laya** once its inference is working, using the same script, so
   the choice between the self-hosted and the hosted model rests on the same
   examples.

---

## 7. Reproducing this

The examples and their expected answers are in `simulators/task_eval_set.py`. The
scoring script is `simulators/eval_task_quality.py`.

```bash
# gateway with BACKEND=jev and TYPESAFE_API_KEY set in .env
python -m simulators.eval_task_quality --base-url http://127.0.0.1:8010 --label jev
```

It writes two files to `simulators/out/`:

- `task_quality_jev.csv`: one row per example, with the expected answer, Jev's
  answer, its confidence, the band, the outcome and the time taken.
- `task_quality_jev_summary.csv`: one row per task.

The timing figures from the earlier 200-call run are in
`simulators/out/task_latency_jev.csv` and `task_latency_jev_summary.csv`, produced
by `simulators/sim_task_latency.py`.
