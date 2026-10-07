"""Scope 7: Create with AI prompt template pick (plan section 8.7).

On `prompt.submitted` the prompt is classified into one of the five project types
with one inline `choice` question. No binding: no survey exists yet. Band `act`
picks that type's template; anything else keeps the generic one.
"""
from __future__ import annotations

from typing import Any

from sogo_lite import events, gateway

MODULE = "template_pick"
SPEC_ID = "project_type"
TYPE_CLASSES = {
    "Survey": "a general questionnaire or research study that fits none of the other types",
    "CX Project": "customer feedback on a purchase, product, service or support contact; NPS, CSAT or CES",
    "EX Project": "employee or staff feedback: engagement, workplace, policies, how staff feel",
    "Assessment": "a quiz, test or exam with right and wrong answers, scores or a pass mark",
    "Poll": "a quick vote or single question to choose between options",
}


def _on_prompt_submitted(prompt: str, decision: dict[str, Any], **_: Any) -> None:
    """`decision` is the baseline's choice of template; this handler may change it."""
    spec = {"id": SPEC_ID, "type": "choice", "classes": TYPE_CLASSES,
            "instructions": "What kind of project is this request asking to create?"}
    result = gateway.call("POST", "/v1/classify", {"text": prompt, "questions": [spec]},
                          module=MODULE, trigger="prompt.submitted", kind="author")
    decision["checked"] = True
    if result.fallback:
        decision["fallback"] = result.reason
        gateway.set_outcome(result.log_id, "generic template (fallback)")
        return
    answer = result.answers.get(SPEC_ID) or {}
    decision.update(detected=answer.get("label"), confidence=answer.get("confidence"),
                    band=answer.get("band"), forced="forced_low_confidence" in result.faults)
    if answer.get("band") == "act" and answer.get("label") in TYPE_CLASSES:
        decision["template"] = answer["label"]
        gateway.set_outcome(result.log_id, f"template picked: {answer['label']}")
    else:
        gateway.set_outcome(result.log_id, f"generic template (band {answer.get('band')})")


events.subscribe("prompt.submitted", MODULE, _on_prompt_submitted)
