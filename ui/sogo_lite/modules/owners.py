"""Feature concept: Feedback Owners.

The account keeps a list of teams, each with a one-line description. Every
comment in a project the feature is switched on for is assigned to one team when
the response is submitted, and each team gets an inbox of its own comments.

The mock has one account and no login, so who may open an inbox is not modelled.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db, engine
from sogo_lite.modules import free_text, inferred

MODULE = "feedback_owners"
FEATURE = "owners"
SPEC_ID = "owner_team"
NO_TEAM = "none"
MAX_TEAMS = 30


def teams() -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM owner_team ORDER BY id")


def save_teams(lines: str) -> tuple[bool, str]:
    """Replace the team list from `team: description | lead email` lines."""
    parsed: list[tuple[str, str, Optional[str]]] = []
    for line in (lines or "").splitlines():
        head, _, lead = line.partition("|")
        name, _, description = head.partition(":")
        if name.strip() and name.strip() != NO_TEAM and name.strip() not in {p[0] for p in parsed}:
            parsed.append((name.strip(), description.strip(), lead.strip() or None))
    if len(parsed) == 1 or len(parsed) > MAX_TEAMS:
        return False, f"Give between 2 and {MAX_TEAMS} teams, or none to switch routing off."
    db.run("DELETE FROM owner_team")
    for name, description, lead in parsed:
        db.run("INSERT INTO owner_team(name, description, lead) VALUES(?,?,?)", name, description, lead)
    return True, f"{len(parsed)} team(s) saved. Comments already assigned keep their team until they are read again."


def enabled_projects(feature: str = FEATURE) -> set[int]:
    return {r["survey_no"] for r in db.rows("SELECT survey_no FROM project_feature WHERE feature=?", feature)}


def set_projects(survey_nos: list[int], feature: str = FEATURE) -> None:
    db.run("DELETE FROM project_feature WHERE feature=?", feature)
    for survey_no in dict.fromkeys(survey_nos):
        db.run("INSERT INTO project_feature(survey_no, feature) VALUES(?,?)", survey_no, feature)


def spec() -> Optional[dict[str, Any]]:
    found = teams()
    if len(found) < 2:
        return None
    return {"id": SPEC_ID, "type": "choice", "instructions": "Which team should own this comment?",
            "classes": {**{t["name"]: t["description"] for t in found},
                        NO_TEAM: "nothing a team could act on, or not a real answer"}}


def _specs(resp: dict[str, Any], question: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = spec()
    if wanted is None or resp["survey_no"] not in enabled_projects() \
            or free_text.has_list(resp["survey_no"], question["qid"]):
        return []
    return [wanted]


def _low_score(question: dict[str, Any], by_qid: dict[str, dict[str, Any]], score: Optional[float]) -> bool:
    """A score in the lower half of its scale stands in for a negative comment."""
    parent = by_qid.get(question.get("parent_qid") or "")
    if parent is None or score is None:
        return False
    low, high = engine.metric_range(parent)
    return score <= (low + high) / 2


def inbox() -> dict[str, Any]:
    """Assigned comments by team. A comment the gateway was unsure of goes to nobody
    and is listed for a person to route."""
    wanted = spec()
    out: dict[str, Any] = {"teams": {t["name"]: {"team": t, "comments": [], "low": 0} for t in teams()},
                           "unsure": [], "unassigned": 0}
    if wanted is None:
        return out
    for survey_no in sorted(enabled_projects()):
        proj = engine.project(survey_no)
        if not proj:
            continue
        by_qid = {q["qid"]: q for q in engine.questions(survey_no)}
        for row in db.rows("SELECT i.response_id, i.qid, i.answers, r.respondent, r.submitted_at FROM inferred_result i"
                           " JOIN response r ON r.id=i.response_id WHERE r.survey_no=? ORDER BY i.response_id DESC",
                           survey_no):
            answer = db.loads(row["answers"], {}).get(SPEC_ID)
            question = by_qid.get(row["qid"])
            if not answer or not question or answer.get("_spec") != inferred.spec_hash(wanted):
                continue
            given = engine.answers(row["response_id"])
            score = inferred.metric_score(row["response_id"], question)
            comment = {"survey_no": survey_no, "project": proj["name"], "response_id": row["response_id"],
                       "text": (given.get(row["qid"]) or {}).get("text") or "", "score": score,
                       "low": _low_score(question, by_qid, score), "confidence": answer.get("confidence"),
                       "label": answer.get("label"), "submitted_at": row["submitted_at"],
                       # In an Anonymous project the inbox shows the comment and never who wrote it.
                       "respondent": None if proj["anonymous"] else row["respondent"]}
            team = inferred.counted_label(answer)
            if team in out["teams"]:
                out["teams"][team]["comments"].append(comment)
                out["teams"][team]["low"] += 1 if comment["low"] else 0
            elif inferred.confident(answer):
                out["unassigned"] += 1
            else:
                out["unsure"].append(comment)
    return out


def send_digests() -> int:
    """Write one digest per team with comments to the email outbox. Nothing leaves the mock."""
    sent = 0
    for name, box in inbox()["teams"].items():
        if not box["comments"]:
            continue
        lines = [f"- {c['text']} ({c['project']}, response #{c['response_id']})" for c in box["comments"][:20]]
        payload = {"to": box["team"]["lead"] or f"{_slug(name)}-lead@example.test",
                   "subject": f"Feedback for {name}: {len(box['comments'])} comment(s)",
                   "body": f"{name}: {len(box['comments'])} comment(s), {box['low']} with a low score.\n"
                           + "\n".join(lines)}
        db.run("INSERT INTO sink_log(sink, survey_no, rule_id, response_id, payload, created_at)"
               " VALUES('email', NULL, NULL, NULL, ?, ?)", db.dumps(payload), db.now())
        sent += 1
    return sent


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "team"


inferred.register(MODULE, _specs)
