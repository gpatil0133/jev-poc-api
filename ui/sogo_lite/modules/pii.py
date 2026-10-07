"""Scopes 9 and 10: personal data (not on the scope page; added for the PoC).

  9.  a question that asks for personal data -> hint in the builder, with the kind.
      It rides in the builder-hint call of scopes 4 and 5 (see design_hints.py).
  10. a text answer that contains personal data -> flagged on the response at
      `response.submitted`, with the kind.

Personal data here means data that identifies a person. A sensitive topic (scope 4)
is a different thing: "What is your religion?" is sensitive and identifies nobody.

Scope 10 sends the answer itself to the gateway, like every answer-level scope.
With an external backend the personal data leaves the network with it.
"""
from __future__ import annotations

from typing import Any

from sogo_lite import db, engine, events, gateway

QUESTION_MODULE = "pii_question"
ANSWER_MODULE = "pii_answer"
NONE = "none"       # the choice's escape option: the gateway bands it `none`

KINDS = {
    "name": "Name",
    "contact": "Contact details",
    "government_id": "Official ID number",
    "date_of_birth": "Date of birth",
    "financial_account": "Bank or card details",
    "account_id": "Account or login details",
}

QUESTION_SPEC: dict[str, Any] = {
    "id": "pii_q", "type": "choice",
    "instructions": "What personal data that identifies a person does this survey question ask the "
                    "respondent to give?",
    "classes": {
        "name": "the person's own first name, surname or full name",
        "contact": "an email address, phone number, or home or postal address",
        "government_id": "a passport, national ID, social security, tax or driving licence number",
        "date_of_birth": "a full date of birth",
        "financial_account": "a bank account, card number or other payment details",
        "account_id": "an employee, customer, patient, student or membership number, a username or a password",
        NONE: "asks for nothing that identifies the person: an opinion, rating, choice, or a broad category "
              "such as age band, city, department or job role",
    },
}
ANSWER_SPEC: dict[str, Any] = {
    "id": "pii_a", "type": "choice",
    "instructions": "What personal data that identifies a person does this survey answer contain?",
    "classes": {
        "name": "the name of a specific person, the respondent or someone else such as a staff member",
        "contact": "an email address, phone number, or home or postal address",
        "government_id": "a passport, national ID, social security, tax or driving licence number",
        "date_of_birth": "a full date of birth",
        "financial_account": "a bank account number, card number or other payment details",
        "account_id": "an employee, customer, patient, student, order or membership number, a username "
                      "or a password",
        NONE: "contains nothing that identifies a person: opinions, descriptions, or general details such "
              "as a city, a job role or an age band",
    },
}


def found_kind(answer: dict[str, Any]) -> str:
    """The kind of personal data an answer reports, or '' when it reports none or is unsure."""
    if answer.get("_forced") or answer.get("band") not in ("act", "suggest"):
        return ""
    return answer["label"] if answer.get("label") in KINDS else ""


def _on_response_submitted(response: dict[str, Any], answers: dict[str, engine.Answer], **_: Any) -> None:
    if response["offline"]:
        return      # Offline Mode: no gateway call at all.
    for q in engine.questions(response["survey_no"]):
        text = (answers.get(q["qid"]) or {}).get("text") or ""
        if q["type"] != "text" or not text.strip():
            continue
        body = {"text": text, "survey_question": q["wording"][:1000], "questions": [ANSWER_SPEC]}
        result = gateway.call("POST", "/v1/classify", body, module=ANSWER_MODULE, trigger="response.submitted",
                              kind="submit", survey_no=response["survey_no"], response_id=response["id"])
        if result.fallback:
            engine.add_note(response["id"], {"kind": "check skipped", "what": f"personal data in {q['qid']}",
                                             "reason": result.reason})
            gateway.set_outcome(result.log_id, "not checked (fallback)")
            continue
        answer = result.answers.get(ANSWER_SPEC["id"]) or {}
        kind = found_kind(answer)
        if not kind:
            gateway.set_outcome(result.log_id, "no personal data flagged")
            continue
        db.run("INSERT INTO pii_flag(response_id, qid, kind, confidence, band, log_id, created_at)"
               " VALUES(?,?,?,?,?,?,?) ON CONFLICT(response_id, qid) DO UPDATE SET kind=excluded.kind,"
               " confidence=excluded.confidence, band=excluded.band, log_id=excluded.log_id,"
               " created_at=excluded.created_at", response["id"], q["qid"], kind, answer.get("confidence"),
               answer["band"], result.log_id, db.now())
        gateway.set_outcome(result.log_id, f"personal data flagged: {KINDS[kind].lower()}")


def flags(response_id: int) -> dict[str, dict[str, Any]]:
    """Flags on one response, by question id."""
    return {row["qid"]: row for row in db.rows("SELECT * FROM pii_flag WHERE response_id=?", response_id)}


events.subscribe("response.submitted", ANSWER_MODULE, _on_response_submitted)
