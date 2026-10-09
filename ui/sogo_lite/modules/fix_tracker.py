"""Feature concept: Fix Tracker.

Decision 1, when a comment arrives: which of the account's known issues does it
report? A confident match opens a case against the contact.
Decision 2, when the same contact answers a later survey: is that earlier problem
fixed, still happening or worse? The stored issue is named in the question.

The contact is the response's respondent, which stands in for a Directory
contact. An Anonymous project has none, so nothing is tracked there.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sogo_lite import db
from sogo_lite.modules import free_text, inferred, owners

MODULE = "fix_tracker"
FEATURE = "fix"
ISSUE_SPEC_ID = "fix_issue"
STATUS_PREFIX = "fix_status_"
NO_ISSUE = "none"
MAX_ISSUES = 30
# A contact with more open cases than this has the oldest ones checked first.
MAX_STATUS_CHECKS = 5
STATUSES = {"fixed": "Fixed", "still_happening": "Still happening", "worse": "Worse"}
UNRESOLVED = ("open", "still_happening", "worse")
# Copies of simulators/build_feature_dataset.py TASKS["fix_status"]: keep them in step.
STATUS_INSTRUCTIONS = ("Earlier this customer reported: {issue}. According to this new answer, "
                       "what is the state of that problem now?")
STATUS_CLASSES = {
    "fixed": "says the earlier problem has been resolved, or did not happen this time",
    "still_happening": "says the same problem is continuing or happened again",
    "worse": "says the problem has grown or caused further harm",
    "not_mentioned": "does not talk about the earlier problem",
}


def issues() -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM known_issue ORDER BY id")


def save_issues(lines: str) -> tuple[bool, str]:
    """Replace the issue list from `issue: description` lines."""
    parsed = {k: v for k, v in free_text.parse_lines(lines).items() if k != NO_ISSUE}
    bad = [name for name in parsed if not re.fullmatch(r"[A-Za-z0-9_ \-]{1,40}", name)]
    if bad:
        return False, f"Use letters, digits, spaces, _ and - in an issue name: {', '.join(bad)}"
    if len(parsed) > MAX_ISSUES:
        return False, f"The issue list holds at most {MAX_ISSUES} issues."
    db.run("DELETE FROM known_issue")
    for name, description in parsed.items():
        db.run("INSERT INTO known_issue(name, description) VALUES(?,?)", name, description)
    return True, f"{len(parsed)} issue(s) saved."


def issue_spec() -> Optional[dict[str, Any]]:
    found = issues()
    if not found:
        return None
    return {"id": ISSUE_SPEC_ID, "type": "choice", "instructions": "Which known issue does this comment report?",
            "classes": {**{i["name"]: i["description"] for i in found},
                        NO_ISSUE: "no complaint, or a complaint about something else"}}


def status_spec(case: dict[str, Any], descriptions: dict[str, str]) -> dict[str, Any]:
    named = case["issue"].replace("_", " ")
    if descriptions.get(case["issue"]):
        named += f" ({descriptions[case['issue']]})"
    return {"id": f"{STATUS_PREFIX}{case['id']}", "type": "choice",
            "instructions": STATUS_INSTRUCTIONS.format(issue=named)[:400], "classes": dict(STATUS_CLASSES)}


def unresolved_cases(contact: str, before_response: Optional[int] = None) -> list[dict[str, Any]]:
    found = db.rows(f"SELECT * FROM issue_case WHERE contact=? AND status IN ({','.join('?' * len(UNRESOLVED))})"
                    " ORDER BY id", contact, *UNRESOLVED)
    return [c for c in found if before_response is None or c["response_id"] < before_response]


def _specs(resp: dict[str, Any], question: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = issue_spec()
    if wanted is None or not resp["respondent"] or resp["survey_no"] not in owners.enabled_projects(FEATURE) \
            or free_text.has_list(resp["survey_no"], question["qid"]):
        return []
    descriptions = {i["name"]: i["description"] for i in issues()}
    # Only complaints from an earlier response: a comment is not its own follow-up.
    earlier = unresolved_cases(resp["respondent"], before_response=resp["id"])[:MAX_STATUS_CHECKS]
    return [wanted] + [status_spec(case, descriptions) for case in earlier]


def _on_answer(resp: dict[str, Any], question: dict[str, Any], spec: dict[str, Any],
               answer: dict[str, Any]) -> Optional[str]:
    label = inferred.counted_label(answer)
    if spec["id"] == ISSUE_SPEC_ID:
        if label is None:
            return None
        if any(c["issue"] == label for c in unresolved_cases(resp["respondent"])):
            return f"issue {label}: this contact already has an unresolved case"
        db.run("INSERT INTO issue_case(contact, issue, survey_no, response_id, qid, opened_at, confidence)"
               " VALUES(?,?,?,?,?,?,?)", resp["respondent"], label, resp["survey_no"], resp["id"],
               question["qid"], db.now(), answer.get("confidence"))
        return f"case opened: {label}"
    case_id = spec["id"].removeprefix(STATUS_PREFIX)
    if label not in STATUSES or not case_id.isdigit():
        return None
    db.run("UPDATE issue_case SET status=?, status_response_id=?, status_at=? WHERE id=?",
           label, resp["id"], db.now(), int(case_id))
    return f"case #{case_id}: {label}"


def cases() -> list[dict[str, Any]]:
    return db.rows("SELECT c.*, p.name AS project FROM issue_case c LEFT JOIN project p ON p.survey_no=c.survey_no"
                   " ORDER BY c.id DESC LIMIT 300")


def report() -> list[dict[str, Any]]:
    """Fix rate per issue: of the contacts who complained and answered again, how many say it is fixed."""
    out = []
    for issue in issues():
        mine = db.rows("SELECT c.status, p.name AS project FROM issue_case c LEFT JOIN project p"
                       " ON p.survey_no=c.survey_no WHERE c.issue=?", issue["name"])
        again = [c for c in mine if c["status"] != "open"]
        by_project: dict[str, list[int]] = {}
        for case in again:
            tally = by_project.setdefault(case["project"] or "?", [0, 0])
            tally[0] += 1
            tally[1] += 1 if case["status"] == "fixed" else 0
        fixed = sum(1 for c in again if c["status"] == "fixed")
        out.append({"issue": issue["name"], "description": issue["description"], "cases": len(mine),
                    "answered_again": len(again), "fixed": fixed,
                    "fix_rate": round(100.0 * fixed / len(again)) if again else None,
                    "worse": sum(1 for c in again if c["status"] == "worse"),
                    "by_project": {name: round(100.0 * t[1] / t[0]) for name, t in by_project.items()}})
    return out


inferred.register(MODULE, _specs, _on_answer)
