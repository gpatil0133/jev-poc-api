"""The Evaluation page: runs the labelled sets against the gateway from the browser.

It is the same code as `python -m simulators.eval_task_quality` (the sets, the
scoring and the output files), started on a background thread with the number of
texts per set chosen on the page. The gateway it calls is the one this mock is
configured for, so the model is whichever BACKEND that gateway runs.

These calls are not written to the Inspector: a full run is 1,000 calls and would
bury everything else. Each run keeps its own files under data/evaluations/.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

from sogo_lite import db
from sogo_lite.config import UI_ROOT, gateway_token, get_settings

# The sets and the runner live with the simulators, next to ui/ in the repo.
if str(UI_ROOT.parent) not in sys.path:
    sys.path.append(str(UI_ROOT.parent))

from simulators import eval_task_quality as runner  # noqa: E402
from simulators.task_eval_set import SETS, TIERS, rows  # noqa: E402

logger = logging.getLogger(__name__)

TIMEOUT_MS = 20000
MAX_SPLIT_CHECK = 40
# A run whose first calls all come back empty is stopped: the gateway or the token is wrong.
GIVE_UP_AFTER = 10
FILE_NAMES = {"decisions": "Every decision", "summary": "By question and tier", "calls": "Every call",
              "sets": "By set", "split": "Alone versus together"}

STATE: dict[str, Any] = {"running": False, "stopping": False, "done": 0, "total": 0, "current": "",
                         "run_id": None}
_lock = threading.Lock()


def out_dir(run_id: int) -> Path:
    return get_settings().db_file.parent / "evaluations" / f"run_{run_id}"


def set_options() -> list[dict[str, Any]]:
    """The sets with their sizes, for the form."""
    out = []
    for eval_set in SETS:
        data = rows(eval_set["id"])
        out.append({"id": eval_set["id"], "scope": eval_set["scope"], "use": eval_set["use"],
                    "enabled": eval_set["enabled"], "texts": len(data),
                    "tiers": {tier: sum(1 for r in data if r["tier"] == tier) for tier in TIERS}})
    return out


def parse_config(form: Any) -> tuple[Optional[dict[str, Any]], str]:
    """Read the form: texts per set, the split check and the token price."""
    counts: dict[str, int] = {}
    for option in set_options():
        raw = (form.get(f"n_{option['id']}") or "0").strip()
        if not raw.isdigit():
            return None, f"Texts for {option['id']} must be a whole number."
        if int(raw):
            counts[option["id"]] = min(int(raw), option["texts"])
    if not counts:
        return None, "Give at least one set a number of texts above zero."
    split = (form.get("split_check") or "0").strip()
    price = (form.get("usd_per_mtok") or "").strip()
    try:
        usd = float(price) if price else None
    except ValueError:
        return None, "The price must be a number."
    return {"counts": counts, "split_check": min(MAX_SPLIT_CHECK, int(split)) if split.isdigit() else 0,
            "usd_per_mtok": usd if usd is None or usd >= 0 else None}, ""


def _client() -> httpx.Client:
    return httpx.Client(base_url=get_settings().gateway_base_url, timeout=TIMEOUT_MS / 1000.0 + 10,
                        headers={"Authorization": f"Bearer {gateway_token()}"})


def _run(run_id: int, config: dict[str, Any]) -> None:
    chosen = [(s, runner.sample(rows(s["id"]), config["counts"][s["id"]]))
              for s in SETS if s["id"] in config["counts"]]
    result = runner.new_result()
    error: Optional[str] = None

    def _progress(done: int, total: int, set_id: str) -> None:
        STATE.update(done=done, total=total, current=set_id)
        if done == GIVE_UP_AFTER and all(c["fallback"] for c in result["calls"]):
            STATE["stopping"] = True
            STATE["gave_up"] = result["calls"][-1]["fallback_reason"] or "no answer"

    try:
        with _client() as http:
            runner.run(http, chosen, result, timeout_ms=TIMEOUT_MS, split_check=config["split_check"],
                       usd_per_mtok=config["usd_per_mtok"], progress=_progress,
                       stop=lambda: STATE["stopping"])
        if STATE.get("gave_up"):
            error = (f"Stopped after {GIVE_UP_AFTER} calls with no answer ({STATE['gave_up']}). "
                     "Check the gateway address and token in Settings.")
    except Exception as exc:
        logger.exception("Evaluation run %s failed", run_id)
        error = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        try:
            files = [key for key in runner.OUTPUTS if result[key]]
            runner.write_outputs(result, f"run{run_id}", out_dir(run_id))
            stored = {"headline": runner.headline(result), "summary": result["summary"], "sets": result["sets"],
                      "files": files}
        except Exception as exc:
            logger.exception("Evaluation run %s could not be saved", run_id)
            stored, error = {}, error or f"{type(exc).__name__}: {exc}"[:300]
        db.run("UPDATE eval_run SET finished_at=?, results=?, error=? WHERE id=?", db.now(), db.dumps(stored),
               error, run_id)
        STATE.update(running=False, stopping=False, current="")
        STATE.pop("gave_up", None)


def start(config: dict[str, Any]) -> tuple[bool, str]:
    """Start a run on a background thread. One run at a time."""
    if not gateway_token():
        return False, "No gateway token is set, so every call would come back empty."
    with _lock:
        if STATE["running"]:
            return False, "A run is already in progress."
        run_id = db.run("INSERT INTO eval_run(started_at, config) VALUES(?,?)", db.now(), db.dumps(config))
        STATE.update(running=True, stopping=False, done=0, total=sum(config["counts"].values()),
                     current="starting", run_id=run_id)
    threading.Thread(target=_run, args=(run_id, config), name="evaluation-run", daemon=True).start()
    return True, ""


def stop() -> None:
    """Ask the running evaluation to stop after the call in flight. What it has is kept."""
    if STATE["running"]:
        STATE["stopping"] = True


def _load(run: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if run:
        run["config"] = db.loads(run["config"], {})
        run["results"] = db.loads(run["results"], {})
    return run


def get(run_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """One finished run, or the latest one."""
    if run_id is not None:
        return _load(db.one("SELECT * FROM eval_run WHERE id=? AND finished_at IS NOT NULL", run_id))
    return _load(db.one("SELECT * FROM eval_run WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"))


def history(limit: int = 15) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM eval_run WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT ?", limit)
    return [_load(run) for run in found]


def by_question(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The summary regrouped for the page: one row per question, with a cell per tier."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for line in run["results"].get("summary") or []:
        entry = grouped.setdefault((line["set"], line["question"]), {"set": line["set"], "scope": line["scope"],
                                                                     "question": line["question"], "tiers": {}})
        if line["tier"] == "all":
            entry["all"] = line
        else:
            entry["tiers"][line["tier"]] = line
    return [entry for entry in grouped.values() if "all" in entry]


def file_path(run_id: int, key: str) -> Optional[Path]:
    if key not in runner.OUTPUTS:
        return None
    path = out_dir(run_id) / f"task_quality_run{run_id}{runner.OUTPUTS[key]}.csv"
    return path if path.exists() else None
