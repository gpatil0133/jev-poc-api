"""Feature concept: "This might identify you" warning.

In an Anonymous project, before a page is saved, each comment on it is checked
for wording that could reveal who wrote it or that names someone else. The
participant sees a note and can edit the comment or send it as it is.

It never blocks and it stores nothing about the response: only the comment text
is sent. The participant is waiting, so the call uses the respondent-facing
timeout, and a timeout, an error or an unsure answer shows no note.
"""
from __future__ import annotations

from typing import Any

from sogo_lite import engine, gateway, modules

MODULE = "identity_warning"
TASK = "ex.identity_risk"
RISKS = {"identifies_writer": "could show who wrote it", "names_someone_else": "names or points to someone else"}


def check(resp: dict[str, Any], proj: dict[str, Any], visible: list[dict[str, Any]],
          provided: dict[str, engine.Answer]) -> dict[str, str]:
    """Question id -> why its comment is risky, for the comments on this page."""
    if not modules.is_on(MODULE) or not proj["anonymous"] or resp["offline"]:
        return {}
    found: dict[str, str] = {}
    for q in visible:
        text = ((provided.get(q["qid"]) or {}).get("text") or "").strip()
        if q["type"] != "text" or not text:
            continue
        # No response id on the call: the check is not tied to the response it came from.
        result = gateway.call("POST", "/v1/classify",
                              {"text": text, "survey_question": q["wording"][:1000], "tasks": [TASK]},
                              module=MODULE, trigger="before page.next", kind="respondent",
                              survey_no=resp["survey_no"])
        if result.fallback:
            gateway.set_outcome(result.log_id, "no note shown (fallback)")
            continue
        answer = result.answers.get(TASK) or {}
        risky = answer.get("label") in RISKS and answer.get("band") in ("act", "suggest") \
            and not answer.get("_forced")
        if risky:
            found[q["qid"]] = RISKS[answer["label"]]
        gateway.set_outcome(result.log_id, f"note shown: {answer['label']}" if risky else "no note shown")
    return found
