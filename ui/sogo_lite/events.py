"""Platform events (plan section 4.1).

The baseline raises an event wherever a scope could act. Modules subscribe; the
baseline does not know which modules exist. A handler failure is logged and never
reaches the participant or the author.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Optional

from sogo_lite import modules

logger = logging.getLogger(__name__)

EVENTS = ("question.saved", "prompt.submitted", "page.next", "response.submitted", "grading.opened")

_handlers: dict[str, list[tuple[Optional[str], Callable[..., Any]]]] = defaultdict(list)


def subscribe(event: str, module_id: Optional[str], handler: Callable[..., Any]) -> None:
    """module_id=None means the handler decides for itself which modules are on."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    _handlers[event].append((module_id, handler))


def emit(event: str, **payload: Any) -> None:
    for module_id, handler in _handlers[event]:
        if module_id is not None and not modules.is_on(module_id):
            continue
        try:
            handler(**payload)
        except Exception:
            logger.exception("Handler for %s (module %s) failed", event, module_id)
