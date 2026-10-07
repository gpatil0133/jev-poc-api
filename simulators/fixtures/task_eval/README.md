# Task evaluation samples

1,000 invented texts for judging answer quality, one gateway call each. The sets,
the questions each text is scored on and the scoring are defined in
`simulators/task_eval_set.py`; the runner is `simulators/eval_task_quality.py`.

Every file is JSON Lines, one sample per line:

| Field | Meaning |
|---|---|
| `id` | `{set}-{nnn}`, unique |
| `text` | what is sent as the text of the call |
| `tier` | `easy` (direct wording), `medium` (indirect, longer, mixed in with other content, typos) or `hard` (idiom, sarcasm, negation, near miss, implied, conditional) |
| `expected` | the expected answer per question; its shape is set-specific, see `task_eval_set.py` |
| `note` | why the label is what it is; present on every `hard` sample of the seven active sets |
| `survey_question`, `metric_score` | optional context sent with the text |

`shared_classes.jsonl` and `other_gateway.jsonl` are the parked sets (60 texts, not
part of the 1,000). `tag_categories.json` and `quiz_questions.json` hold the tag
lists and the quiz questions with their key points.

All texts are invented and labelled by one author. No real person, customer or
company appears in them; personal data in `pii_answers.jsonl` is made up.
