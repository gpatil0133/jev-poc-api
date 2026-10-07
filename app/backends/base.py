"""Backend contract. Laya and Jev share the /v1/systemone wire shape, so one
protocol covers both; a fake implements it for tests."""
from __future__ import annotations

from typing import Any, Optional, Protocol


class BackendError(Exception):
    """The backend could not answer (down, timed out, refused the request)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class DecisionBackend(Protocol):
    # "laya" | "jev" | "fake". Part of the cache key, so answers never cross backends.
    name: str
    # Upstream cap on states per batch call; callers chunk to this.
    max_batch_states: int

    async def predict(
        self, state: Any, questions: dict[str, dict], model: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        """One state. Returns the raw result: {answers, routing, usage, ...}."""
        ...

    async def predict_batch(
        self, states: list[Any], questions: dict[str, dict], model: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Many states, same questions. Results in input order."""
        ...

    async def health(self) -> dict[str, Any]:
        """Never raises: {"status": "ok" | "down", ...}."""
        ...
