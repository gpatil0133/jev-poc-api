# Sogo-lite mock platform

A local stand-in for the Sogolytics Design module, used to see how the platform
would behave if the eight Design classification scopes were built on the
classification gateway in this repo. The plan it follows is
[docs/sogo-lite-mock-platform-plan.md](../docs/sogo-lite-mock-platform-plan.md).

It is a test bench, not a product: one account, no login, no email delivery, no
Salesforce or LLM connection. Everything that would leave the platform is written
to a local log you can inspect.

## Run it

From the repo root, in PowerShell:

```powershell
.venv\Scripts\python -m pip install -r ui\requirements.txt
$env:SOGO_LITE_GATEWAY_TOKEN = "<gateway JWT>"     # a corp_no when the gateway runs with DEV_AUTH_BYPASS
cd ui
..\.venv\Scripts\python -m uvicorn sogo_lite.app:app --port 8020
```

Open http://127.0.0.1:8020. The first start creates `ui/data/sogo_lite.db` and seeds
six example projects.

| Setting | Where | Default |
|---|---|---|
| Gateway base URL | `ui/config.json`, or `SOGO_LITE_GATEWAY_URL` | `http://192.168.0.171:8010` |
| Gateway token | `SOGO_LITE_GATEWAY_TOKEN`, environment only | none: every call falls back with `no_token` |
| Timeouts per call type | `ui/config.json` | respondent 300 ms, submit 2,000, author 1,500, batch 30,000 |
| Database file | `ui/config.json`, or `SOGO_LITE_DB` | `ui/data/sogo_lite.db` |
| Module toggles, fault switches | Settings page (stored in the database) | all modules on, all faults off |

The token is read from the environment and is never written to a file, the
database or the call log.

To try it without the GPU box, run the gateway on the stub model from the repo root
and point the mock at it. The stub's answers are a hash of the input and mean nothing.

```powershell
.venv\Scripts\python -m uvicorn simulators.fake_laya:app --port 8001
$env:LAYA_BASE_URL = "http://127.0.0.1:8001"; $env:DEV_AUTH_BYPASS = "true"
.venv\Scripts\python -m uvicorn app.api.main:app --port 8011
# in the mock's shell:
$env:SOGO_LITE_GATEWAY_URL = "http://127.0.0.1:8011"; $env:SOGO_LITE_GATEWAY_TOKEN = "77245"
```

Tests need neither: `..\.venv\Scripts\python -m pytest tests/` from `ui/`.

## What is where

| Screen | What it is |
|---|---|
| Projects | Project list, new project, Create with AI (scope 7) |
| Design | Pages and questions; the question editor holds the Classes tab (scope 1) and the builder hints (scopes 4, 5) |
| Logic | Answer Display Logic and branching; "answer is about…" (scope 3) |
| Tags | Categories, manual tagging, exact auto-map; "Suggest tags" and its review list (scope 6) |
| Assign Scores | Points per option; model answer and key points for open-ended questions (scope 8) |
| Rules & Alerts | Rules with conditions and actions; "the comment means…" (scope 2) |
| Participate | The live survey page, with an Offline Mode switch |
| Responses | Individual Responses, Post-Population fields, the grading list (scope 8) |
| Outbox & logs | The three local sinks: email outbox, webhook log, Salesforce log |
| Inspector | Every gateway call: request, answers, bands, latency, flags, outcome. Also a side panel |
| Scenarios | Runs every acceptance check in four columns and exports the matrix |
| Settings | Gateway status, test call, module toggles, fault switches, restore seed data |

With every module switched off the mock behaves like today's product and never
calls the gateway.

## Seeded projects

| No. | Project | Exercises |
|---|---|---|
| 9001 | Retail feedback (CX) | Scopes 2, 3 |
| 9002 | Clinic visit (CX) | Scopes 1, 2 |
| 9003 | Hotel stay (CX) | Scope 6 |
| 9004 | Staff pulse (EX, Anonymous) | Scopes 2 (anonymity), 4 |
| 9005 | Food-safety quiz (Assessment) | Scope 8 |
| 9006 | Large tag list (CX) | Scope 6 limit: one category of 500 tags |

Scope 5 and the scope 4 checks run on a scratch project that the scenario runner
creates and deletes.

Class sets are saved to the gateway as bindings under these survey numbers, for
the tenant in the token. The gateway has no endpoint that deletes a binding. If
9001 to 9006 could clash with real bindings for that tenant, change the constants
at the top of `sogo_lite/seed.py` before the first start.

## Scenario runner

"Run all scenarios" replays the scope page's examples through the same code path
as the live pages, once per column: normal, gateway down, slow gateway (2,500 ms)
and forced low confidence. The saved module toggles and fault switches are not
touched. A full run takes a few minutes because the slow column waits out the
timeouts.

- A failed check under **Normal** means the scope does not work as described.
  That is a statement about the model's answers, so it only means something
  against the real model. Against the stub model most Normal checks fail.
- A failed check under a **fault column** means the fallback is wrong, which is a
  platform problem.

## What the gateway source settled

The plan lists things the OpenAPI spec does not say (sections 3.5 and 12.2). This
repo is the gateway, so most are answered by its code:

| Question | Answer | Where |
|---|---|---|
| The 10 catalog tasks | `comment.sentiment`, `.response_type`, `.quality`, `.actionable`, `.urgent`; `meaning.may_leave`, `.safety_concern`, `.complaint`, `.callback_request`; `column.field_type` | `app/registry/tasks/*.yaml` |
| Keys of a binding's `thresholds` | Catalog task ids only. A custom question carries its own `thresholds` | `compiler.py`, `compile_set` |
| A `yesno` answer | `label` is `yes` or `no`, `value` is P(yes), and the band is worked out on P(yes) | `classify.py`, `decode` and `band_for` |
| A `score` answer | `value` is the expected level, `label` is that rounded | same |
| A `labels` answer | One answer per label, id `{id}.{label}` with non-word characters turned into `_` | `compiler.py`, `_label_slug` |
| Job output | A JSONL file under `data/jobs/` on the gateway host. No endpoint returns it | `services/jobs.py` |
| Shortlisting large label lists | Not done for classify. A `choice` over 100 options, or a call over 64 wire questions, is refused with 422 | `compiler.py` limits |
| Cache key | State hash, model and the hash of each compiled question; thresholds are not part of it. In memory, cleared on restart | `classify.py` |
| `context` on inline questions | Ignored unless a catalog task names the field in its `state` | `compiler.py`, `build_state` |

## Where the mock departs from the plan

- **Stack.** Python (FastAPI, Jinja, SQLite) instead of the recommended ASP.NET
  Core: there is no .NET SDK on this machine, and this is the toolchain the gateway
  already uses. The gateway client is one file (`sogo_lite/gateway.py`) written to
  be read as a specification for the .NET port.
- **Location.** `ui/` in this repo, not a separate repository.
- **Health probes are not logged.** The header indicator polls `GET /health`; logging
  each poll would bury the real calls in the Inspector.
- **The three sinks share one table** (`sink_log`, with a `sink` column).
- **Grade suggestions live in `post_populate`** as drafts, not in `Suggestion`.
- **Matching follow-ups on a metric question** are ordinary "answer is about…" rules
  on the follow-up Text Box, added on the Logic page.
- **One attempt per comment at submit.** When the gateway cannot answer, the rules
  that read the same comment share that one failed attempt, so several meaning
  rules do not each wait out the timeout.
- **A grade the gateway bands `none`** is listed with its key points but no
  suggested score.
- **Follow-up context.** The preceding question's wording is sent in `context`, as
  the plan says, but the gateway ignores it for inline questions (see the table
  above), so the follow-up check sees the wording alone.
- **Tag shortlist calls.** A batch call takes one question set for all its items,
  so shortlisted items are grouped by identical shortlist: one call per group,
  not one per category.

## Layout

```
ui/
  config.json            gateway URL, timeouts, database path
  sogo_lite/
    app.py               FastAPI app and start-up
    config.py, db.py     settings; SQLite schema and helpers
    gateway.py           the gateway client: fallback contract, fault switches, call log
    events.py            platform events
    engine.py            baseline platform: questions, Logic, Tags, scores, alerts, the survey flow
    modules/             one file per scope (4 and 5 share design_hints.py)
    create_ai.py         Create with AI stub
    seed.py              the six seeded projects and their responses
    scenarios.py         the acceptance checks and the runner
    routes/              design, participate, tools (Inspector, Settings, Scenarios)
    templates/, static/  server-rendered pages
  tests/                 plain-function tests against an in-process fake gateway
```
