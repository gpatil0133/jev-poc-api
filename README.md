# Laya PoC API

A research gateway for trying Laya (the open equivalent of Jev) the way Sogolytics
modules would call it: bulk labelling of comments, per-response checks at submit,
single sub-second calls from the live survey page, and column mapping on import.

Laya runs as stock `laya-serve` on a GPU box. This service is a thin FastAPI layer
in front of it that owns the prompts, the cache, the confidence bands and the
fallback behaviour.

**What this cut can and cannot tell you.** It proves the plumbing and the speed.
It does not measure accuracy, and nothing here says whether Laya's answers are
good enough to build on. Public write-ups put the base checkpoints below a
majority-class baseline zero-shot on Laya's own benchmark, with confidences that
need refitting on your data. Every decision is logged (`logs/decisions/`) so an
eval can be added without rework.

## Run it

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.11+
pip install -r requirements.txt
cp .env.example .env                                 # set LAYA_BASE_URL and LAYA_API_KEY
uvicorn app.api.main:app --port 8010
```

Laya itself: `deploy/laya-compose.yaml` on the GPU box, then check
`GET /health` shows both checkpoints loaded on `cuda`.

No GPU to hand? A stub with the same routes and limits stands in for it. Its
answers are a hash of the input and mean nothing; it exists to exercise the plumbing.

```bash
python -m uvicorn simulators.fake_laya:app --port 8000      # terminal 1
LAYA_BASE_URL=http://127.0.0.1:8000 DEV_AUTH_BYPASS=true uvicorn app.api.main:app --port 8010
```

Auth is the usual RS256 JWT (`JWT_PUBLIC_KEY_PATH`). With `DEV_AUTH_BYPASS=true`
the Bearer value is a raw corp_no; the server refuses to start that way unless
`APP_ENV` is dev-like.

Tests need neither Laya nor the stub: `python -m pytest tests/`.

## Endpoints

| Endpoint | Used for | Behaviour |
|---|---|---|
| `POST /v1/classify` | Live survey page at "Next"; per-response at submit | One text. Never errors because of Laya: on timeout, overload or Laya down, every answer is band `none` with `fallback: true`. |
| `POST /v1/classify/batch` | Dashboard labelling, export | Up to `BATCH_MAX_ITEMS` (256) items. Grouped by question set, de-duplicated, sent to Laya 64 states at a time. 503 if Laya is down. |
| `POST /v1/jobs`, `GET /v1/jobs/{id}` | Whole-survey backfill | Async, chunked through the batch path, results as JSONL under `data/jobs/`. In-process: job state is lost on restart. |
| `POST /v1/map/columns` | Directory / Activity / Data import | `field_type` (fixed list) or `survey_question` (embedding shortlist to the top k, then Laya picks). |
| `PUT/GET /v1/bindings/{survey_no}/{question_id}` | Design-time setup | Stores what runs on a question. Rejects a binding whose options do not fit the token budget. |
| `GET /v1/tasks`, `POST /v1/tasks/compile` | Debugging | List templates; dry run showing the exact request a call would send to Laya. |
| `GET /health` | | Gateway status plus Laya's `/health`. |

Every answer has `label`, `confidence`, `probabilities`, `band` and `cached`.
Yes/no answers also carry `value` = P(yes); score answers carry the expected level.

```bash
curl -s localhost:8010/v1/classify -H "Authorization: Bearer 77245" -H "content-type: application/json" -d '{
  "text": "Love it, but if prices go up again I am switching",
  "survey_question": "Anything else you would like to tell us?",
  "tasks": ["meaning.may_leave", "comment.sentiment"]
}'
```

## Prompt setup

Laya has no system prompt and no free text out. A request is a **state** (the
text) plus named **questions**; each question's `instructions` and option
descriptions are the prompt. The gateway builds the questions from three layers,
most specific wins:

| Layer | Owned by | Where |
|---|---|---|
| 1. Task template | Platform | `app/registry/tasks/*.yaml` |
| 2. Question binding | Survey author | `PUT /v1/bindings/...`, stored per tenant + survey + question |
| 3. Inline | The caller | `questions` in the request |

A binding names the templates to run and adds author-defined classes. The author
writes a name and a one-line description; the gateway writes the instructions.

```json
{
  "survey_no": 11, "question_id": "Q5",
  "survey_question": "What is the main reason for your score?",
  "tasks": ["comment.sentiment", "comment.quality", "meaning.may_leave"],
  "custom": [{
    "id": "topic", "type": "choice",
    "classes": {"price": "cost, fees, value for money",
                "delivery": "shipping time, courier, damaged on arrival",
                "staff": "behaviour or helpfulness of employees"},
    "thresholds": {"act": 0.80}
  }]
}
```

Question types an author or caller can use: `choice` (pick one), `labels`
(multi-label: one yes/no per label, answers come back as `{id}.{label}`),
`yesno`, `score`.

What the compiler does (`app/registry/compiler.py`):

- **One read per comment.** All questions bound to a survey question go to Laya in one call.
- **The state carries context**: `{"survey_question": ..., "answer": ...}`, plus the metric score when given. "ok" only means something next to the question asked.
- **Every choice gets an escape option** (`other`) unless the author supplied one. An escape answer always bands `none`.
- **Yes/no compiles to a two-option choice with neutral keys** (`A`/`B`). Laya's own `noul` type can follow its true/false labels instead of the text.
- **The option budget is checked when a binding is saved**: 192 tokens for the English checkpoint, 256 for multilingual. The count is an estimate; the real tokenizer lives with the checkpoint.
- **Thresholds belong to the reader.** One result is stored; each caller can band it with its own `thresholds`. Bands are `act`, `suggest` and `none` (keep today's behaviour). Yes/no is banded on P(yes).
- **Rotations only in batch.** `rotations: n` sends each question under n option orders in the same forward pass and averages them, which evens out Laya's position bias. Live calls always use one pass.
- **Compiled questions are hashed.** Results are cached on (state hash, model, question hash), so the live nudge and a later batch label of the same answer are the same record, and adding a task to a question only asks Laya the new question.

## Simulators

Each script drives the gateway the way one module would and writes a CSV to
`simulators/out/` plus a latency summary. No accuracy numbers.

| Script | Simulates |
|---|---|
| `python -m simulators.sim_batch_labelling --survey 61` | NLP filters / coded comments: label every open-text answer, then again to show the cache. `--job` uses `/v1/jobs`. |
| `python -m simulators.sim_submit_alerts --survey 61 --rate 10` | Rules & Alerts at submit: responses arrive one by one, alert meanings are checked, alerts fire above a threshold. |
| `python -m simulators.sim_live_branching --rate 10` | QDL/ADL/branching and the nudge: author-defined classes, single calls at an arrival rate with the 300 ms budget; reports how often the default path was taken. `--burst 8` lands 8 clicks at once. |
| `python -m simulators.sim_column_mapping --surveys 5` | Import mapping: field types for `fixtures/import_columns.csv`, and column-to-question mapping against real survey structures. |

Survey data is read from `../survey-chat/raw_data/corpno_77245/` and
`../tagging-system/sample-data/merged.csv`. Survey 11 has no open-text question;
61 and 38 do.

### Feature-concept dataset

`simulators/fixtures/feature_concepts/` is a hand-written mock dataset for the JEV
feature concepts (Virtual Questions, Survey Coach, Fix Tracker, Feedback Owners and
the decision-only features): 510 decisions across 13 tasks, each with the expected
answer. `tasks.json` holds the question definitions, `requests/*.jsonl` one
`/v1/systemone` request per row, `ground_truth.json` the planted patterns.
Rebuild with `python -m simulators.build_feature_dataset`. It is for trying ideas
and tuning option wording, not for accuracy numbers.

## Sogo-lite mock platform

`ui/` is a local stand-in for the Sogolytics Design module that calls this gateway
the way the eight Design scopes would: shared classes, alerts on comment meaning,
logic on text answers, builder hints, tag suggestions, Create with AI template
pick and quiz scoring. It has an Inspector for every gateway call, fault switches,
and a scenario runner that reports each acceptance check with the gateway normal,
down, slow and unsure. See [ui/README.md](ui/README.md) and
[docs/sogo-lite-mock-platform-plan.md](docs/sogo-lite-mock-platform-plan.md).

```bash
pip install -r ui/requirements.txt
cd ui && SOGO_LITE_GATEWAY_TOKEN=<jwt> python -m uvicorn sogo_lite.app:app --port 8020
```

## Things to know before reading the numbers

- **Laya runs one forward pass at a time.** Live calls queue behind each other, and a call the gateway gave up on still costs Laya its forward pass. `LIVE_MAX_INFLIGHT` (default 4) caps how many live calls wait on Laya; the rest fall back at once with `fallback_reason: "busy"`. Size it as budget ÷ per-call latency once you have real numbers from the GPU box. Batch and job traffic share the same Laya and will slow live calls while they run.
- **Banding uses `answer_confidence`**, Laya's calibrated probability of the reported answer. Laya's `confidence` on a choice is 1 − normalised entropy and shifts with the number of options; `action.act_probability` carries no signal.
- **The cache and job state are in process memory**, per worker. Run one worker.
- **The column shortlist needs `sentence-transformers`** for real embeddings (commented out in `requirements.txt`). Without it a hashed token embedding is used, which only matches on shared words.
- **`LOG_TEXT=false` by default**: the decision log stores a hash of the text, not the comment.

## Not in this cut

- Accuracy eval, temperature refit, fine-tuning. These decide whether the feature is worth building.
- Value/option matching (pre-fill, recode "Other"), SogoConnect/Workflow scopes, EX anonymity.
- PII masking before the model. Laya is self-hosted; this is needed before any Jev trial.
- Persistent storage of labels in the platform.

## Layout

```
app/
  api/main.py              routes, lifespan
  core/config.py, auth.py  settings, JWT dependency
  backends/                DecisionBackend protocol, laya-serve client, fake backend
  registry/                task templates (YAML), compiler, bindings
  services/                classify (cache, banding, fallback), jobs, mapping, decision log
  models/schemas.py        request/response models
simulators/                one script per calling pattern, plus the Laya stub
tests/                     plain-function tests against the fake backend
deploy/laya-compose.yaml   laya-serve on the GPU box
ui/                        Sogo-lite mock platform (its own README and tests)
```

## Switching between Laya and Jev

`BACKEND=laya | jev` in `.env` picks the model behind the gateway. It is read once
at start-up, so change it and restart. `GET /health` reports the active one under
`backend`, and the result cache is keyed by backend, so answers never cross over.

`BACKEND=jev` calls the TypeSafe API (`https://api.typesafe.ai`) with
`TYPESAFE_API_KEY`. It is an external service billed per input token; nothing is
masked before it is sent. `app/backends/jev.py` absorbs where Jev differs from
laya-serve:

| laya-serve | Jev | What the gateway does |
|---|---|---|
| `/v1/systemone/batch` | no batch route | single calls, `JEV_MAX_CONCURRENT` (8) at a time |
| `answer_confidence` | not returned | bands on the probability of the reported answer |
| `option_order` for rotations | not accepted | sends the rotation with its criteria reordered |
| `english` / `multilingual` | `jev-latest`, `jev-preview` | Laya names map to `TYPESAFE_DEFAULT_MODEL` |
| `/health` | none | `/v1/models` stands in |
| `X-Inference-Time-Ms` | none | `backend_ms` is empty |

The option token budget (192 / 256) is Laya's; on Jev treat the budget report as a
rough guide. A round trip to Jev measured about 370 ms median from the office
network, so the default `LIVE_TIMEOUT_MS=300` falls back on most uncached live calls.
