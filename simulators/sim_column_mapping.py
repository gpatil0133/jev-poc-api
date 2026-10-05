"""Simulates import mapping.

  field_type       the headers and sample rows of fixtures/import_columns.csv
  survey_question  columns synthesised from real survey structures in
                   tagging-system/sample-data/merged.csv (short header + answer
                   options as sample values), mapped back onto that survey's
                   questions through the embedding shortlist

    python -m simulators.sim_column_mapping --surveys 5
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from simulators.common import (
    REPO_ROOT, base_parser, clean_text, client, latency_summary, print_summary, write_csv,
)

IMPORT_FIXTURE = Path(__file__).parent / "fixtures" / "import_columns.csv"
MERGED_CSV = REPO_ROOT / "tagging-system" / "sample-data" / "merged.csv"
# Surveys at or under this size skip the shortlist, so they are not interesting here.
MIN_QUESTIONS = 16
HEADER_WORDS = 4
MAX_COLUMNS_PER_SURVEY = 12


def _fixture_columns() -> list[dict[str, Any]]:
    with IMPORT_FIXTURE.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [{"name": name, "samples": [row[name] for row in rows]} for name in rows[0]]


def _surveys(limit: int) -> list[dict[str, Any]]:
    """The first `limit` surveys in merged.csv with enough questions. The file is large; it is streamed."""
    csv.field_size_limit(2 ** 31 - 1)
    surveys = []
    with MERGED_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                data = json.loads(row["SurveyStructureJSON"])["SurveyData"][0]
            except (ValueError, KeyError, IndexError):
                continue
            questions, columns = [], []
            for question in data.get("questionData") or []:
                title = clean_text(question.get("questionTitle"))
                if not title:
                    continue
                questions.append({"id": f"Q{question['questionID']}", "text": title[:1000]})
                options = [o.get("answerText", "") for o in question.get("answerOptions") or []]
                samples = [clean_text(o)[:60] for o in options if clean_text(o)][:5]
                if samples and len(columns) < MAX_COLUMNS_PER_SURVEY:
                    columns.append({"name": " ".join(title.split()[:HEADER_WORDS]), "samples": samples})
            if len(questions) >= MIN_QUESTIONS and columns:
                surveys.append({"title": clean_text(data.get("surveyTitle")), "corp": row["corporate_no"],
                                "survey_no": row["survey_no"], "questions": questions, "columns": columns})
            if len(surveys) >= limit:
                break
    return surveys


def _post(http, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    resp = http.post("/v1/map/columns", json=body)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    resp.raise_for_status()
    return resp.json(), latency_ms


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--surveys", type=int, default=5, help="survey structures to read from merged.csv")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()
    rows: list[dict[str, Any]] = []

    with client(args) as http:
        columns = _fixture_columns()
        data, field_ms = _post(http, {"mode": "field_type", "columns": columns})
        for column, mapping in zip(columns, data["mappings"]):
            rows.append({"mode": "field_type", "survey": "", "column": column["name"],
                         "samples": " | ".join(column["samples"][:3]), "target": mapping["target"],
                         "confidence": mapping["confidence"], "band": mapping["band"], "shortlist": ""})

        surveys = _surveys(args.surveys)
        print(f"Read {len(surveys)} survey structures with {MIN_QUESTIONS}+ questions")
        latencies, embedding, options = [], None, []
        for survey in surveys:
            data, latency_ms = _post(http, {
                "mode": "survey_question", "columns": survey["columns"],
                "survey_questions": survey["questions"], "top_k": args.top_k})
            latencies.append(latency_ms)
            embedding = data["embedding"] or embedding
            for column, mapping in zip(survey["columns"], data["mappings"]):
                options.append(len(mapping["probabilities"]))
                rows.append({"mode": "survey_question", "survey": f"{survey['corp']}/{survey['survey_no']}",
                             "column": column["name"], "samples": " | ".join(column["samples"][:3]),
                             "target": mapping["target"], "confidence": mapping["confidence"],
                             "band": mapping["band"], "shortlist": ";".join(mapping["shortlist"])})

    def bands(mode: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            if row["mode"] == mode:
                counts[row["band"]] = counts.get(row["band"], 0) + 1
        return counts

    question_rows = [row for row in rows if row["mode"] == "survey_question"]
    path = write_csv("column_mapping.csv", rows)
    print_summary("column mapping", {
        "field_type_columns": len(columns), "field_type_ms": round(field_ms, 1),
        "field_type_bands": bands("field_type"),
        "surveys": len(surveys),
        "survey_question_columns": len(question_rows),
        "questions_per_survey": [len(s["questions"]) for s in surveys],
        "options_sent_per_column": sorted(set(options)),
        "embedding": embedding,
        "request_latency": latency_summary(latencies),
        "ms_per_column": round(sum(latencies) / max(1, len(question_rows)), 1),
        "survey_question_bands": bands("survey_question"),
        "mapped_to_none": sum(1 for row in question_rows if row["target"] is None),
    }, path)


if __name__ == "__main__":
    main()
