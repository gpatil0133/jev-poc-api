# Sogo-lite Mock Platform: Build Plan for the Design Classification PoC

**Date:** 2026-10-06
**Status:** Plan, not yet started
**Sources:** "Sogolytics JEV Scope" page (Design section) and the gateway's OpenAPI spec at `http://192.168.0.171:8010/openapi.json` (title "Laya PoC gateway", version 0.1.0)

---

## 1. Summary

We want to see how the Sogolytics platform would behave if the Design-module classification scopes were built on top of the classification gateway. Testing that inside the real platform is expensive, so this plan describes a small local stand-in, called **Sogo-lite** here.

Sogo-lite does two things:

1. It reproduces today's Design behaviour (questions, Logic, Tags, Assign Scores, Rules & Alerts, Create with AI) in a simplified form, with no classifier involved.
2. It adds each of the eight Design scopes as a separate module that can be switched on and off, so every scope can be compared before and after on the same survey.

The mock is a test bench, not a product. It has one account, no login, no email delivery and no real Salesforce or LLM connection. Everything that would leave the platform is written to a local log that you can inspect.

What the mock should let us answer:

- Does each scope work end to end against the gateway, using the request shapes the gateway accepts today?
- What does the platform do when the gateway is slow, down, or unsure? Each scope has a defined fallback, and the mock must show it happening.
- Is the gateway's latency acceptable on the respondent-facing path (Logic on text answers)?
- Do the limits of the model (about 15 options, short inputs) break any scope in practice? Tag suggestions is the one most at risk.

Decisions still needed before the build starts are listed in [section 12](#12-risks-and-open-questions).

---

## 2. Scope

### 2.1 In scope: the eight Design scopes

| # | Scope | Priority on the scope page | What it adds |
|---|---|---|---|
| 1 | Shared classes for branching and alerts | Not set | Define classes on a text question once; Logic and Rules & Alerts both read the same result |
| 2 | Rules & Alerts on comment meaning | High | A new condition type, "the comment means…" |
| 3 | Logic on text answers | Low | A new condition, "answer is about…", including matching follow-ups after a metric question |
| 4 | Builder hints for sensitive questions | Not set | Flag a sensitive question and suggest Mandatory or Encouraged Response |
| 5 | Follow-up flag on Text Box questions | Low, for discussion | Suggest marking a Text Box as a "why" follow-up |
| 6 | Tag suggestions | Low | Suggest tags for questions and answer options from the account's own tag list |
| 7 | Create with AI: prompt template pick | Not set | Classify the user's prompt into one of the five project types |
| 8 | Open-ended quiz scoring | Low | Suggest correct / partly correct / incorrect for written answers; a grader confirms |

Shared classes is built first even though its priority is not set, because scopes 2 and 3 both depend on it.

### 2.2 Out of scope

- Every scope outside Design: Publish & Track, Participate (nudge), Reports & Data, CX/EX Dashboards, Export, Integrations, Workflows, Activities & Directories, Data imports.
- The gateway's `POST /v1/map/columns` endpoint. It serves the import and directory scopes, not Design.
- Real email, SMS, Salesforce, Zapier or LLM calls.
- Multiple accounts, user types and permissions.
- Offline Mode as a real feature. The mock only has a switch that forces the default path, which is what the scope page says Offline Mode must do.
- Visual fidelity with the real product. The mock uses the real module names and labels but not the real design system.

### 2.3 What "the classifier" is in this test

The scope page describes two models, Laya and Jev. The gateway we are testing against runs **Laya**. Its `/health` response shows the `english` and `multilingual` Laya checkpoints loaded on GPU, and nothing in the spec refers to Jev. The mock therefore tests platform behaviour against Laya through the gateway. If Jev is put behind the same gateway later, the mock should not need to change.

---

## 3. The gateway

Base URL: `http://192.168.0.171:8010`

### 3.1 What was checked live

| Check | Result |
|---|---|
| `GET /openapi.json` | 200, no auth needed |
| `GET /health` | 200, no auth needed. Status ok, both models loaded on `cuda`, 10 tasks registered, cache empty |
| `GET /v1/tasks` | 401. The body asks for `Authorization: Bearer {jwt_token}` |

All `/v1/*` endpoints declare HTTP Bearer auth in the spec. No call that needs a token has been made yet, so everything below about response contents comes from the spec's schemas, not from observed responses.

### 3.2 Endpoints

| Method and path | Purpose | Used by the mock for |
|---|---|---|
| `GET /health` | Gateway and model status | Status indicator in the header |
| `GET /v1/tasks` | Catalog of ready-made tasks (10 registered) | The "ready-made meaning" picker in class sets and alert rules |
| `POST /v1/tasks/compile` | Dry run. Returns the exact request that would go to the model, plus a token budget report. Nothing is sent to the model | Budget check when an author defines classes; debugging |
| `PUT /v1/bindings/{survey_no}/{question_id}` | Save what runs on one survey question: catalog tasks, custom questions, thresholds, model | Saving a class set |
| `GET /v1/bindings/{survey_no}/{question_id}` | Read a binding back | Checking the mock's local copy is in sync |
| `POST /v1/classify` | Classify one text | All live paths: alerts, logic, hints, follow-up flag, Create with AI |
| `POST /v1/classify/batch` | Classify many texts in one call | Tag suggestions, quiz scoring |
| `POST /v1/jobs` | Start an asynchronous batch; returns 202 and a job status | Testing progress polling for large grading runs |
| `GET /v1/jobs/{job_id}` | Job status and progress | Same |
| `POST /v1/map/columns` | Map file columns to field types or survey questions | Not used |

### 3.3 Request shapes

**Classify request** (`POST /v1/classify`). Only `text` is required.

| Field | Type | Notes |
|---|---|---|
| `text` | string, up to 20,000 characters | The text to classify |
| `survey_question` | string, up to 1,000 characters | The wording of the question the text answers |
| `metric_score` | number | For example the NPS score given alongside a comment |
| `context` | object | Free-form |
| `survey_no` + `question_id` | integer + string | Selects a saved binding. `question_id` must match `^[A-Za-z0-9_.\-]{1,64}$` |
| `tasks` | list of strings | Catalog task names to run |
| `questions` | list of question specs | Inline, author-defined questions |
| `model` | `auto`, `english` or `multilingual` | |
| `thresholds` | `{act, suggest}` | Each between 0 and 1 |
| `timeout_ms` | integer, 20 to 30,000 | Per-call time limit |

**Question spec.** This is how a custom classification is defined, either inline or inside a binding.

| Field | Notes |
|---|---|
| `id` | Required. Same pattern as `question_id` |
| `type` | `choice` (default), `labels`, `yesno` or `score` |
| `classes` | Map of label to one-line description. Used by `choice`, `labels` and `yesno` |
| `levels` | Ordered list of level descriptions. Used by `score` |
| `instructions` | Optional, up to 400 characters. The gateway writes instructions itself when this is omitted |
| `thresholds` | Per-question `{act, suggest}` |
| `rotations` | 1 to 8 |

The four types behave as follows, according to the spec:

- `choice`: pick one of `classes`. The gateway adds an escape option itself.
- `labels`: multi-label. One yes/no per entry in `classes`. Answer ids become `{id}.{label}`.
- `yesno`: `classes` is `{"true": "...", "false": "..."}`.
- `score`: pick a level from `levels`.

**Binding** (`PUT /v1/bindings/...`). Fields: `survey_no`, `question_id`, `survey_question`, `tasks`, `custom` (list of question specs), `thresholds` (a map of thresholds), `model`. The response contains the saved binding, a `question_hash`, and a budget report.

**Batch request** (`POST /v1/classify/batch` and `POST /v1/jobs`). Same selectors as classify (`survey_no`, `question_id`, `tasks`, `questions`, `model`, `thresholds`, `rotations`) plus `items`, a list of at least one item. Each item has `text` and optionally `id`, `survey_question`, `metric_score`, `context` and its own `question_id`.

### 3.4 Response shapes

**Classify response.**

| Field | Notes |
|---|---|
| `answers` | Map of answer id to an answer object |
| `fallback` | `true` when the gateway could not produce a real answer |
| `fallback_reason` | Text, when `fallback` is true |
| `latency_ms`, `backend_ms` | Total time and model time |
| `model` | Which model answered |
| `truncated` | `true` when the input was cut to fit |
| `question_hash` | Identifies the exact set of questions that was asked |

**Answer object.**

| Field | Notes |
|---|---|
| `label` | The chosen label, or null |
| `confidence` | 0 to 1 |
| `probabilities` | Map of label to probability |
| `value` | Number or null (relevant to `score`) |
| `band` | `act`, `suggest` or `none` |
| `cached` | `true` when served from the gateway's cache |

**Batch response.** `results` (one per item, each with `id`, `answers`, `model`, `truncated`), plus `latency_ms`, `groups`, `backend_calls`, `cache_hits` and `items`.

**Job status.** `job_id`, `status` (`queued`, `running`, `done`, `failed`), `total`, `processed`, `failed_items`, `cache_hits`, `output_path`, `error`, `started_at`, `finished_at`, `items_per_s`.

**Budget report** (from compile and from saving a binding). `model`, `estimated`, `ok`, `wire_questions`, `warnings`, and one line per question with `options`, `option_tokens`, `instruction_tokens`, `head_tokens`, `budget` and a `status` of `ok`, `warn` or `over`.

### 3.5 Things the spec does not tell us

These need one authenticated call each to settle. They are repeated in section 12.

- The names and contents of the 10 catalog tasks.
- How job results are retrieved. The job status has an `output_path` but the spec has no endpoint that returns job output. If the path is a file on the gateway host, the mock cannot read it.
- What the keys of a binding's `thresholds` map are (most likely task or question ids).
- What `label` contains for a `yesno` answer (`"true"` / `"false"` is the likely reading) and how `value` is filled for `score`.
- How long the JWT lives and how it is issued.

---

## 4. Architecture

```
 Browser
   |
   v
+-----------------------------------------------------------------+
| Sogo-lite web app                                               |
|                                                                 |
|  Baseline platform            Scope modules (toggle each)       |
|  - Projects                   1 Shared classes                  |
|  - Design: questions          2 Alerts on meaning               |
|  - Design: Logic              3 Logic on text answers           |
|  - Design: Tags               4 Sensitive-question hints        |
|  - Design: Assign Scores      5 Follow-up flag                  |
|  - Design: Rules & Alerts     6 Tag suggestions                 |
|  - Create with AI (stub)      7 Create with AI template pick    |
|  - Participate, Responses     8 Quiz scoring                    |
|         |                            |                          |
|         +-----> Platform events <----+                          |
|                                      |                          |
|                              Gateway client  <--- Fault switches|
|                                      |                          |
|   Local sinks: email outbox,         +--> Call log (Inspector)  |
|   webhook log, Salesforce log                                   |
|                                                                 |
|   SQLite database                                               |
+--------------------------------------|--------------------------+
                                       v
                      Laya PoC gateway  http://192.168.0.171:8010
```

### 4.1 Parts

**Baseline platform.** The simplified Sogolytics features in section 5. It never calls the gateway. With every module switched off, the mock behaves like today's product.

**Platform events.** The baseline raises an event at each point where a scope could act. Modules subscribe to events; the baseline does not know which modules exist.

| Event | Raised when | Carries |
|---|---|---|
| `question.saved` | An author saves a question in Design | Project, question, wording, type, Mandatory/Encouraged setting |
| `prompt.submitted` | A user submits a Create with AI prompt | The prompt text |
| `page.next` | A participant clicks Next on the live survey | Response in progress, the answers on that page |
| `response.submitted` | A participant submits the survey | The complete response |
| `grading.opened` | A grader opens the grading list for an Assessment | Project, the open-ended scored questions |

Two more actions are started by a button, not an event: "Suggest tags" in Design → Tags, and "Check budget" in the class editor.

**Scope modules.** One module per scope. Each module is made of up to three pieces:

- a design-time panel (where the author configures or reviews it),
- one or more event handlers,
- a result view (where the outcome shows up).

Each module has an on/off toggle on a single settings page. Turning a module off removes its panel and stops its handlers; its stored data stays.

**Gateway client.** The only code that talks to the gateway. Section 7 defines its contract.

**Inspector.** A page, also available as a side panel, that lists every gateway call. Section 9 defines it.

**Local sinks.** Rules & Alerts actions write to three local logs instead of sending anything: an email outbox, a webhook log and a Salesforce log.

### 4.2 Stack

Recommended: **ASP.NET Core with Razor Pages and SQLite**. The real platform is .NET, so the gateway client, the timeout and fallback handling, and the event handlers can be carried over to it with little rewriting. That portability is the main thing the mock produces besides test results.

If the goal is only to get test results fast and nothing will be reused, a Node or Python app is quicker to stand up. This is an open decision (section 12). The rest of this document does not depend on the choice.

### 4.3 Configuration

| Setting | Source | Notes |
|---|---|---|
| Gateway base URL | Config file | Default `http://192.168.0.171:8010` |
| Gateway JWT | Environment variable only | Never written to a file, the database or the call log |
| Default timeouts | Config file | Per call type, see section 7 |
| Module toggles and fault switches | Database | Changed from the UI |

---

## 5. Baseline platform

This is today's behaviour as the scope page describes it, cut down to what the eight scopes need.

### 5.1 Projects

- A project list and a "new project" form.
- Each project has a number (`survey_no`), a name, one of the five project types (Survey, CX Project, EX Project, Assessment, Poll) and an Anonymous flag.

### 5.2 Design → questions

- Pages, with questions on each page.
- Question types: Radio Button, Check Box, a metric rating question (NPS, CSAT or CES), and Text Box.
- Per question: Mandatory, Encouraged Response, or neither.
- A Text Box can be marked as a follow-up by hand. A metric question can have up to two follow-up questions, triggered by the score only.
- Each question gets a stable `question_id` that fits the gateway's pattern (letters, digits, `_`, `.`, `-`, up to 64 characters), for example `q12`.

### 5.3 Design → Logic

- Answer Display Logic and Multi-Question Branching.
- Conditions on fixed-option questions: an option is picked or not picked.
- Conditions on open-ended answers: Contains, Does not contain, starts with, is exactly, is answered, is not answered.
- Actions: show a question, or jump to a page.

### 5.4 Design → Tags

- Tag categories with tags inside them. The real limits are 10 categories per account and 500 tags per category; the mock allows the same so the limit can be tested.
- Question-level tags (what a rating measures) and response-level tags (one tag per answer option).
- Auto-map: an answer option gets a tag only when its text exactly matches the tag name.

### 5.5 Design → Assign Scores (Assessment projects)

- Points per answer option for fixed-option questions, scored automatically.
- Open-ended questions are not scored automatically. A grader opens Individual Responses and types a score and feedback into Post-Population fields.

### 5.6 Design → Rules & Alerts

- A rule has conditions and actions.
- Conditions: a picked answer, a count of answers, or a metric score reaching a number.
- Actions: send an email (written to the outbox), call a webhook (written to the webhook log), push to Salesforce (written to the Salesforce log).
- Rules run when a response is submitted.
- The real product's limit of 5,000 alert emails per day per account is not enforced. The outbox shows a daily count so alert volume is visible.

### 5.7 Create with AI

- A prompt box and a "generate" button.
- No LLM is called. The stub holds one canned question set per project type, plus a generic one.
- The scope page says how the template is chosen today is still to be confirmed. Until it is, the baseline always uses the generic template. This is an assumption of the mock, not a statement about the product.

### 5.8 Participate and responses

- A live survey page with paging and a Next button, applying the Logic rules.
- A switch on the page that simulates Offline Mode.
- An Individual Responses list per project, with a detail view that shows answers, scores and Post-Population fields.

---

## 6. Data model

One SQLite database. Names are indicative.

### 6.1 Baseline tables

| Table | Key fields |
|---|---|
| `Project` | `survey_no`, name, project type, anonymous |
| `Page` | project, order |
| `Question` | project, page, `question_id`, type, wording, required mode (none / Mandatory / Encouraged), is follow-up, parent metric question |
| `AnswerOption` | question, text, order, points |
| `TagCategory`, `Tag` | category name; tag name |
| `QuestionTag`, `AnswerOptionTag` | links |
| `LogicRule` | project, source question, operator, operand, action, target |
| `AlertRule` | project, name, conditions (JSON), actions (JSON), enabled |
| `Response` | project, started at, submitted at, offline flag |
| `ResponseAnswer` | response, question, selected option or text or number |
| `PostPopulateValue` | response, question, score, feedback, status (draft / confirmed), source (grader / suggestion) |
| `OutboxEmail`, `WebhookLog`, `SalesforceLog` | rule, response, payload, created at |

### 6.2 Tables added for the scopes

| Table | Key fields | Used by |
|---|---|---|
| `ClassSet` | project, question, catalog tasks, custom question specs (JSON), thresholds, model, `question_hash`, last budget report (JSON), synced at | Scopes 1, 2, 3 |
| `ClassificationResult` | response, question, `question_hash`, answers (JSON), fallback, created at | Scopes 1, 2, 3 |
| `Suggestion` | kind (tag / sensitive hint / follow-up flag / grade), target, proposed value, confidence, band, status (pending / accepted / dismissed) | Scopes 4, 5, 6, 8 |
| `KeyPointSet` | question, model answer, key points (list) | Scope 8 |
| `GatewayCallLog` | time, module, endpoint, request (JSON), response (JSON), HTTP status, latency, fallback, fault applied | Inspector |
| `ModuleToggle`, `FaultSetting` | name, value | Settings |

`ClassSet` is the mock's local copy of a gateway binding. The gateway is the source of truth for what runs; the local copy exists so the UI can render without a round trip and so Logic and alert rules can refer to class names.

`ClassificationResult` is what makes "classify once, both read" work. It is keyed by response, question and `question_hash`, so a result is reused only while the class set is unchanged.

---

## 7. Gateway client contract

Every module goes through this one client.

### 7.1 Behaviour

- Adds `Authorization: Bearer <token>` from the environment variable. If the variable is missing, every call returns a fallback with reason `no_token` and the header shows a warning.
- Sets `timeout_ms` in the request body and also enforces a slightly longer limit on the HTTP call itself, so a hung connection cannot outlast the gateway's own timeout.
- Never throws to the caller. It always returns a result object with a `fallback` flag.
- Treats all of the following as a fallback: connection failure, HTTP timeout, any non-2xx status, an unparseable body, or a 200 response whose own `fallback` field is true.
- Writes one row to `GatewayCallLog` per call, with the token removed.
- Applies the fault switches (section 9) before sending.

### 7.2 Default timeouts

These are starting values to tune during testing, not measured numbers.

| Call type | Default `timeout_ms` | Why |
|---|---|---|
| Respondent-facing (Logic on text answers at `page.next`) | 300 | The scope page requires an answer "in a fraction of a second" |
| Submit-time (alerts at `response.submitted`) | 2,000 | The participant is not waiting on it |
| Author-facing (hints, follow-up flag, template pick) | 1,500 | An author can wait briefly |
| Batch (tags, grading) | 30,000 | The maximum the gateway accepts |

### 7.3 How modules use bands

The gateway marks each answer `act`, `suggest` or `none` from the thresholds in force. The mock uses them the same way everywhere:

| Band | Author-facing suggestion | Automatic decision (branching, alerts) |
|---|---|---|
| `act` | Pre-selected for the author to accept | Applied |
| `suggest` | Shown, not pre-selected | Not applied |
| `none` | Not shown | Not applied |
| Fallback | Nothing shown; the module's fallback runs | The module's fallback runs |

One exception: alert rules and Logic rules that read a shared class each store their own minimum probability and compare it against the answer's `probabilities`. This is how an alert can be stricter than a branch while both read a single classification. When a rule has no minimum of its own, it uses the band.

### 7.4 Token handling

- Read from an environment variable at start-up.
- Never stored in the config file, the database or the call log.
- A 401 from the gateway is shown in the header as "gateway token rejected" and counted as a fallback.

---

## 8. Scope modules

Each module is described the same way: what the author sees, what triggers it, what it sends, what happens when the gateway cannot answer, and how to tell it works.

The probabilities shown in the scope page's examples are illustrations, not measurements. Acceptance checks below test the outcome (the alert fires, the branch is taken), not an exact number.

### 8.1 Shared classes for branching and alerts

**Where:** Design → question editor, on a Text Box question → "Classes" tab.

**Author experience**
- Add ready-made tasks from the catalog (`GET /v1/tasks`).
- Add custom classes: a name, a type (`choice`, `labels`, `yesno`), and labels with a one-line description each. The scope page's example uses sentiment (negative, neutral, positive) and response type (complaint, suggestion, praise, question).
- Set `act` and `suggest` thresholds.
- "Check budget" calls `POST /v1/tasks/compile` and shows each line's status. A line marked `over` blocks saving; `warn` shows a warning.
- "Save" calls `PUT /v1/bindings/{survey_no}/{question_id}` and stores the returned `question_hash` and budget report in `ClassSet`.

**Runtime**
- When a text answer needs classifying, the module calls `POST /v1/classify` with `text`, `survey_no`, `question_id`, the question wording as `survey_question`, and `metric_score` when the Text Box follows a metric question.
- The result is stored in `ClassificationResult`.
- Any later reader (a Logic rule, an alert rule) uses the stored result if its `question_hash` matches the current class set. Otherwise it classifies again.

**Fallback:** if saving the binding fails, the class set stays as an unsynced local draft and cannot be used in rules. If classification fails, readers get "no result" and run their own fallback.

**Acceptance checks**
- A class set saved in the mock can be read back with `GET /v1/bindings/...` and matches.
- For one response, the Inspector shows exactly one classify call for a question that both a Logic rule and an alert rule read.
- Editing the class set changes the `question_hash`, and a new response is classified again.
- The clinic example, "The nurse was rude and nobody called me back.", yields response type Complaint and sentiment Negative, the branch shows the service-recovery question, and the "Complaint and Negative" rule writes an email to the outbox.

### 8.2 Rules & Alerts on comment meaning

**Where:** Design → Rules & Alerts → rule editor.

**Author experience**
- A new condition type, "the comment means…".
- The author picks a Text Box question, then either a class from that question's class set, or a ready-made meaning (may leave, safety concern, complaint about staff, wants a callback), or writes a one-line description of their own. A ready-made or written meaning is added to the question's class set as a `yesno` question, so there is still one binding per question.
- The author sets a minimum probability for the rule.
- Meaning conditions can be combined with the existing conditions (for example, "NPS is 9 or 10 and the comment means may leave").

**Runtime**
- On `response.submitted`, the rule engine evaluates the existing conditions as before.
- For each meaning condition it reads the stored `ClassificationResult` or classifies now, using the submit-time timeout.
- When the probability meets the rule's minimum, the rule's actions run through the existing sinks.
- The outbox email shows the match, for example "matched 0.84".
- In an Anonymous project, the email states that a concern was raised and leaves out who raised it.

**Fallback:** a meaning condition with no result counts as not met. Rules without meaning conditions run exactly as in the baseline. The fallback is recorded against the response so it is visible that a check was skipped.

**Acceptance checks**
- NPS 9 with "Love it, but if prices go up again I'm switching." fires the "may leave" rule and does not fire "safety concern" or "wants a callback". With the module off, no alert fires for the same response.
- A rule with a webhook action writes a webhook log entry only when the comment is classified as a complaint. This stands in for the scope page's "create a Salesforce Case only when a comment is classified as a complaint".
- With the gateway-down fault on, score-based rules still fire and meaning-based rules do not, and submission is not delayed or blocked.
- In an Anonymous project, the outbox email contains no respondent identifier.

### 8.3 Logic on text answers

**Where:** Design → Logic; runs on the live survey page.

**Author experience**
- A new condition on open-ended answers, "answer is about…", next to the existing Contains / starts with / is exactly operators.
- The author types a topic (price, delivery, staff) or picks a class from the question's class set. A typed topic is added to the class set as a `yesno` question.
- Matching follow-ups: on a metric question, the author maps topics to targeted follow-up questions (delivery → the delivery question). Today's score-only follow-up remains the default.
- Only "show a question" is offered as the action for a meaning condition. The scope page's rule is: show a follow-up question, never skip one.

**Runtime**
- On `page.next`, the module classifies the page's text answers that have meaning conditions, using the respondent-facing timeout.
- The result is stored in `ClassificationResult` so alerts at submit can reuse it.
- When the condition is met, the extra question is shown on the next page. Otherwise the default path is taken.

**Fallback:** on timeout, error, low confidence, or when the Offline Mode switch is on, the survey follows the default path. The participant sees no error, no spinner beyond the normal page load, and no difference.

**Acceptance checks**
- With the rule "if the answer is about price, show the pricing question", the answers "too expensive", "costs went up" and "not worth the money" all show the pricing question. The baseline rule `Contains "price"` shows it for none of them.
- NPS 3 with "The sofa arrived two weeks late and the driver left it outside in the rain." shows the delivery follow-up on the next page.
- With the slow-gateway fault set above the timeout, Next still completes and the default path is taken. The Inspector shows the call as a fallback.
- With the Offline Mode switch on, no gateway call is made at all.
- The Inspector reports latency for every `page.next` call, so the share of calls finishing inside the timeout can be read off after a test run.

### 8.4 Builder hints for sensitive questions

**Where:** Design → question editor.

**Author experience**
- After saving a question, a hint may appear under it: "This looks sensitive. Consider Encouraged Response."
- The hint has two buttons: apply the suggested setting, or dismiss.

**Runtime**
- On `question.saved`, the module calls `POST /v1/classify` with the question wording as `text` and one inline `yesno` question (is this question sensitive?). If the task catalog has a sensitivity task, that is used instead.
- A hint is created only when the question is sensitive and its current setting differs from the suggestion.
- The mock's rule for the suggestion: a sensitive question set to Mandatory gets "consider Encouraged Response". This follows the scope page's only example. What to suggest for a sensitive question with no setting is an open question.

**Fallback:** no hint. Saving the question is never delayed; the call runs after the save completes.

**Acceptance checks**
- "What is your annual household income?" set to Mandatory produces a hint.
- "Which branch did you visit?" produces none.
- Only the question wording is sent. The Inspector's request body contains no respondent data.
- Dismissing a hint keeps it dismissed when the question is saved again without changes.

### 8.5 Follow-up flag on Text Box questions

**Where:** Design → Text Box question editor.

**Author experience**
- After saving a Text Box that is not marked as a follow-up, a suggestion may appear: "This looks like a 'why' follow-up." Accept marks it; dismiss hides it.

**Runtime**
- On `question.saved` for an unmarked Text Box, one inline `yesno` question on the wording: does this look like a "why" follow-up? The preceding question's wording is passed in `context`.
- This shares the call with 8.4 when both modules are on: one classify call with two inline questions.

**Fallback:** no suggestion.

**Acceptance checks**
- After an NPS question, "What is the main reason for your score?" is suggested as a follow-up.
- "Please enter your order number" is not.
- With modules 4 and 5 both on, saving a Text Box produces one gateway call, not two.

### 8.6 Tag suggestions

**Where:** Design → Tags → "Suggest tags" button.

**Author experience**
- A review list with two parts: answer options with a suggested tag each, and questions with one or more suggested tags.
- `act` suggestions are pre-selected. `suggest` ones are listed but not selected. The author clicks Accept to apply the selected ones.
- Exact-name auto-map still runs first; only items it leaves untagged go to the gateway.

**Runtime**
- One `POST /v1/classify/batch` call per tag category.
- Answer options: a `choice` question whose `classes` are that category's tags. The gateway adds the escape option, which covers "none".
- Questions: a `labels` question, one yes/no per tag, so a question can receive more than one tag.
- Only question and answer wording is sent.

**The option limit.** The scope page's fit test says a decision should have no more than about 15 options. A tag category can hold 500 tags. The mock handles this in two steps:
1. Before the batch call, `POST /v1/tasks/compile` is run for the category. If no line is `over`, the full list is sent.
2. If a line is `over`, the mock shortlists up to 14 tags per item by simple word overlap between the item's text and the tag names, and sends only those.

Whether a word-overlap shortlist is good enough, or the gateway should shortlist tags itself (it already does this for columns in `/v1/map/columns`), is an open question. The mock should record which path each item took so the two can be compared.

**Fallback:** the review list shows "no suggestions available"; manual tagging and exact auto-map are unaffected.

**Acceptance checks**
- Answer "Grand – Downtown" is suggested the tag Downtown, where exact auto-map finds no match.
- "How clean was your room?" is suggested Housekeeping; "Rate the breakfast buffet" is suggested Restaurant.
- Nothing is applied until the author clicks Accept.
- With a seeded category of 500 tags, the module completes without a gateway validation error, and the Inspector shows whether the shortlist was used.

### 8.7 Create with AI: prompt template pick

**Where:** Create with AI.

**Author experience**
- The user types a prompt and clicks generate, as today.
- The result page shows which project type was detected and which template was used, with the confidence.

**Runtime**
- On `prompt.submitted`, one `POST /v1/classify` call with the prompt as `text` and one inline `choice` question with five classes: Survey, CX Project, EX Project, Assessment, Poll. No binding is used, because no survey exists yet.
- When the band is `act`, the stub generator uses that type's canned template. Otherwise it uses the generic template.

**Fallback:** the generic template, which is the mock's stand-in for the current path.

**Acceptance checks**
- "See how staff feel about the new hybrid-work policy" picks the EX template.
- "Feedback after a support call, with NPS" picks the CX template.
- "Food-safety quiz with a pass mark" picks the Assessment template.
- "Quick vote on the offsite date" picks the Poll template.
- With the forced-low-confidence fault on, all four use the generic template.

### 8.8 Open-ended quiz scoring

**Where:** Assessment project → Design → Assign Scores (set-up); Individual Responses → grading list (review).

**Author experience**
- In Assign Scores, an open-ended question gets a model answer or a list of key points, saved in `KeyPointSet`.
- The grader opens a grading list for the question. Each row shows the answer, the key points found, the suggested grade and its confidence. Rows are ordered with the lowest confidence first.
- The grader confirms or changes each grade. Only a confirmed grade counts as final.

**Runtime**
- On `grading.opened`, the module sends the ungraded answers in one `POST /v1/classify/batch` call with two inline questions:
  - a `labels` question, one entry per key point (is this point present in the answer?),
  - a `choice` question for the overall grade: correct, partly correct, incorrect, not a real answer.
- The question wording goes in `survey_question`.
- Each suggestion is written to `PostPopulateValue` with status draft and source suggestion.
- `POST /v1/jobs` is exercised separately, on a seeded set of a few hundred answers, to test progress polling. It is not the default path until it is known how job output is retrieved (section 12).

**Fallback:** the grading list shows the answers with no suggestion, and the grader grades by hand as today.

**Acceptance checks**

For the question "How should raw chicken be stored in the fridge, and why?" with key points bottom shelf, below ready-to-eat food, stops juices dripping:

| Answer | Expected suggestion |
|---|---|
| "Bottom shelf, under cooked food, so juices can't drip on it." | 3 of 3 key points, Correct |
| "Put it at the bottom so it doesn't drip on things." | 2 of 3, Partly correct, shown near the top of the list for review |
| "Keep it covered." | 0 of 3, Incorrect |

- No suggested grade becomes final without a grader action.
- Suggested and confirmed scores are shown next to the automatic score and are not added into it. The scope page lists whether post-populated scores can feed the overall score as still to confirm.

---

## 9. Inspector and fault injection

### 9.1 Inspector

One row per gateway call, newest first, filterable by module, project and response.

| Column | Source |
|---|---|
| Time, module, endpoint | Client |
| Trigger | The event or button that caused the call |
| Request | Full JSON body, token removed |
| Answers | Each answer id with label, confidence and band |
| Latency | `latency_ms` and `backend_ms` from the response, plus the round-trip time measured by the client |
| Flags | `cached`, `truncated`, `fallback` with its reason |
| `question_hash` | From the response |
| Outcome | What the module did with it: alert fired, branch taken, suggestion created, default path |
| Fault applied | Which switch was on, if any |

Opening a row shows the raw request and response. For bound questions, a "show compiled request" button calls `POST /v1/tasks/compile` to show what was sent to the model.

A summary strip at the top shows, for the current filter: number of calls, fallback rate, cache-hit rate, and median and 95th-percentile latency.

### 9.2 Fault switches

Set from the settings page. Each applies to all modules or to one.

| Switch | Effect | What it tests |
|---|---|---|
| Gateway down | The client skips the call and returns a connection-failure fallback | Every module's fallback path |
| Slow gateway | The client waits a set number of milliseconds before sending | Timeout handling, especially `page.next` |
| Forced low confidence | The client rewrites every answer's band to `none` after the response arrives | Behaviour when the model is unsure |
| Bad token | The client sends an invalid token | 401 handling |
| Module off | The module's panel and handlers are removed | The baseline comparison |

"Forced low confidence" changes the response after it arrives and is marked as such in the Inspector, so a rewritten result is never mistaken for a real one.

---

## 10. Seed data and test scenarios

### 10.1 Seeded projects

The seed uses the scope page's own examples so results can be compared with it.

| Project | Type | Contents | Exercises |
|---|---|---|---|
| Retail feedback | CX Project | NPS question, a "main reason for your score" Text Box, a pricing question, a delivery follow-up with options Late / Damaged / Driver / Wrong item, "Which branch did you visit?" | Scopes 2, 3, 5 |
| Clinic visit | CX Project | Rating question, comment Text Box with sentiment and response-type classes, a service-recovery question | Scopes 1, 2 |
| Hotel stay | CX Project | "How clean was your room?", "Rate the breakfast buffet", a location question with "Grand – Downtown"; tag categories for department and location | Scope 6 |
| Staff pulse | EX Project, Anonymous | Rating questions, a comment Text Box, "What is your annual household income?" set to Mandatory | Scopes 2 (anonymity), 4 |
| Food-safety quiz | Assessment | Fixed-option questions and the raw-chicken open-ended question with its three key points | Scope 8 |
| Large tag list | CX Project | One category with 500 tags | Scope 6 limit |

### 10.2 Seeded responses

Each project has a small set of prepared responses that includes the example comments quoted in section 8, a few paraphrases of each, and a few unrelated comments that should match nothing.

### 10.3 Scenario runner

A page that replays the seeded responses through the real event path and reports a pass or fail per acceptance check.

- Each scenario states the project, the input, the modules that are on, the fault switches that are on, and the expected outcome.
- Every acceptance check in section 8 is a scenario.
- The run produces a matrix: scopes down the side; "normal", "gateway down", "slow gateway" and "low confidence" across the top.
- Results link to the Inspector rows they produced.

A failed check under "normal" means the scope does not work as described. A failed check under a fault column means the fallback is wrong, which is a platform problem, not a model problem.

---

## 11. Build plan

Eight sessions, in order. Each one leaves the app runnable.

### Session 1: Skeleton

**Goal:** an app that starts, talks to the gateway and logs what it does.

**Deliverables**
- App shell with navigation for Projects, Design, Participate, Responses, Inspector, Settings.
- Database with the tables from section 6.
- Gateway client per section 7, with the fault switches.
- Inspector list and detail view.
- Header indicator from `GET /health`.
- A "test call" button in Settings that sends a fixed inline `choice` question to `POST /v1/classify`.

**Done when:** the test call appears in the Inspector with answers, band and latency; with the token unset it appears as a `no_token` fallback; the token appears nowhere in the database.

### Session 2: Baseline platform

**Goal:** today's behaviour, with no gateway calls.

**Deliverables:** everything in section 5, the platform events, the three local sinks, and the seed data from section 10.1.

**Done when:** a seeded survey can be edited, taken and submitted; a score-based alert writes to the outbox; a `Contains` logic rule branches; exact-name auto-map tags an option; a grader can type a Post-Population score; the Inspector stays empty throughout.

### Session 3: Shared classes

**Goal:** scope 1 (section 8.1).

**Deliverables:** the Classes tab, catalog picker, budget check, binding save and sync, `ClassificationResult` storage and reuse.

**Done when:** the acceptance checks in 8.1 that do not depend on rules pass. Also run the 500-tag budget check from 8.6 here, since it only needs `tasks/compile`, to learn early whether the option limit is a real problem.

### Session 4: Rules & Alerts on meaning

**Goal:** scope 2 (section 8.2).

**Deliverables:** the meaning condition in the rule editor, evaluation at `response.submitted`, the match shown in the outbox email, the Anonymous-project wording.

**Done when:** all acceptance checks in 8.2 pass, including the gateway-down case.

### Session 5: Logic on text answers

**Goal:** scope 3 (section 8.3).

**Deliverables:** the "answer is about…" condition, topic-to-follow-up mapping on metric questions, evaluation at `page.next`, the Offline Mode switch.

**Done when:** all acceptance checks in 8.3 pass, and the remaining 8.1 check (one classify call shared by a branch and an alert) passes.

### Session 6: Design-time suggestions

**Goal:** scopes 4, 5, 6 and 7 (sections 8.4 to 8.7).

**Deliverables:** sensitive-question hint, follow-up suggestion, tag suggestion review list with the shortlist path, and template pick in Create with AI.

**Done when:** the acceptance checks in 8.4 to 8.7 pass.

### Session 7: Quiz scoring

**Goal:** scope 8 (section 8.8).

**Deliverables:** key points in Assign Scores, the grading list, draft Post-Population values, and the separate jobs polling test.

**Done when:** the acceptance checks in 8.8 pass, and a job started with `POST /v1/jobs` can be followed to `done` with `GET /v1/jobs/{job_id}`.

### Session 8: Scenario runner and results

**Goal:** a repeatable test run and a results matrix.

**Deliverables:** the scenario runner from section 10.3, with every acceptance check encoded, and an export of the matrix and the latency summary.

**Done when:** one click runs all scenarios in all four columns and the matrix can be exported.

### Rules that hold across all sessions

- No module calls the gateway except through the gateway client.
- No module changes baseline behaviour when it is switched off.
- A gateway failure never blocks or delays a participant and never shows them an error.
- Nothing suggested by the classifier is applied to tags, settings or grades without an author or grader action. Branching and alerts are the only automatic decisions.
- The token never reaches a file, the database or a log.

---

## 12. Risks and open questions

### 12.1 Decisions needed before the build

| # | Decision | Recommendation |
|---|---|---|
| 1 | Stack | ASP.NET Core with Razor Pages and SQLite, so the client and fallback code can move to the real platform |
| 2 | Where the mock's repository lives and who owns it | A new, separate repository |
| 3 | How the mock gets a gateway JWT, and how long it lasts | An environment variable set by the developer; confirm lifetime with whoever runs the gateway |

### 12.2 Questions for the gateway owner

| # | Question | Why it matters |
|---|---|---|
| 1 | What are the 10 catalog tasks, and what does each return? | Decides which scopes use a catalog task and which need custom question specs. Affects 8.1, 8.2 and 8.4 most |
| 2 | How is job output retrieved? `output_path` is in the job status but no endpoint returns the output | Decides whether quiz scoring can use jobs at all |
| 3 | What are the keys of a binding's `thresholds` map? | Needed to save per-class thresholds correctly |
| 4 | What does a `yesno` answer's `label` contain, and how is `value` filled for `score`? | Needed to read results correctly; settled by one live call |
| 5 | Can the gateway shortlist options for a large label list, as it does for columns? | Would replace the mock's word-overlap shortlist for tags |
| 6 | Does the gateway's cache key include the binding, and when is it cleared? | Affects whether repeated test runs measure real latency |

### 12.3 Questions for the product team

| # | Question | Source |
|---|---|---|
| 1 | Is real-time classification on respondent pages acceptable at all? | The scope page asks for this confirmation under Logic on text answers |
| 2 | How is the Create with AI prompt template chosen today? | Marked "to confirm" on the scope page. The mock assumes a generic template |
| 3 | Can post-populated scores feed the overall score and the Results Page? | Marked "still to confirm" on the scope page. The mock keeps them separate |
| 4 | What should the hint suggest for a sensitive question that is neither Mandatory nor Encouraged? | The scope page gives only the Mandatory example |

### 12.4 Risks

| Risk | Effect | How the plan handles it |
|---|---|---|
| The mock tests Laya, not Jev | Results may not carry over to Jev | Stated up front. The gateway hides the model, so a re-run after a swap needs no mock changes |
| Tag lists exceed the model's option limit | Tag suggestions fails or loses accuracy on large accounts | Budget check in session 3, before any tag UI is built; shortlist path in 8.6 |
| Latency on `page.next` is too high for the 300 ms default | Logic on text answers falls back most of the time | The Inspector reports latency per call; the timeout is configurable; the outcome is a finding, not a build failure |
| The gateway address is on the office network | The mock only works where `192.168.0.171` is reachable | Base URL is configurable; the gateway-down switch lets the UI be developed offline |
| The mock's baseline drifts from the real product | A scope works in the mock but not in the platform | The baseline is taken only from the scope page's "what we do today" descriptions; assumptions are marked in this document |
| The scope page's example probabilities are taken as targets | Checks fail for the wrong reason | Acceptance checks test outcomes, not numbers |
