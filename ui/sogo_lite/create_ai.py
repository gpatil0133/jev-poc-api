"""Create with AI stub (plan section 5.7).

No LLM is called. The baseline always uses the generic template: how the real
product picks a template is still to be confirmed, so this is an assumption of the
mock. `prompt.submitted` lets a module change the choice.
"""
from __future__ import annotations

from typing import Any

from sogo_lite import events, seed

GENERIC = "generic"


def decide(prompt: str) -> dict[str, Any]:
    decision: dict[str, Any] = {"template": GENERIC, "checked": False}
    events.emit("prompt.submitted", prompt=prompt, decision=decision)
    return decision


def generate(prompt: str) -> tuple[int, dict[str, Any]]:
    decision = decide(prompt)
    name = " ".join(prompt.split())[:60] or "Untitled project"
    return seed.create_from_template(name, decision["template"]), decision
