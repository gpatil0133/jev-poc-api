"""Feature concepts: Invitation hold and Invitation replies.

The mock has no Distribute module, so this file is a small stand-in for it:
contacts, free-text Activity notes, a batch of email invitations and an inbox
for replies. Sent emails go to the local outbox; nothing leaves the mock.

  hold     before an invitation goes out, the contact's latest Activity note is
           read: send, hold or don't send. Unsubscribed and flagged contacts are
           left out by a plain rule first, as Touch Rules would.
  replies  a reply to an invitation is sorted into unsubscribe, wrong person,
           out of office, feedback or question, and acted on.

Whether real replies reach Sogolytics at all is an open question; here a reply
is pasted in by hand.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db, engine, gateway, modules

HOLD_MODULE = "invite_hold"
REPLY_MODULE = "invite_replies"
TIMING_TASK = "invite.timing"
REPLY_TASK = "invite.reply_type"
ACCOUNT_INBOX = "survey-owner@example.test"
STATUSES = {"sent": "Sent", "held": "Held", "not_sent": "Not sent"}
REPLY_LABELS = {"unsubscribe": "Unsubscribe", "wrong_person": "Wrong person", "out_of_office": "Out of office",
                "feedback": "Feedback", "question": "Question"}


def contacts() -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM contact ORDER BY id")
    for contact in found:
        contact["note"] = latest_note(contact["id"])
    return found


def add_contact(name: str, email: str, note: str = "") -> Optional[int]:
    if not name.strip() or "@" not in email:
        return None
    contact_id = db.run("INSERT INTO contact(name, email) VALUES(?,?)", name.strip(), email.strip())
    add_activity(contact_id, note)
    return contact_id


def add_activity(contact_id: int, note: str) -> None:
    if note.strip():
        db.run("INSERT INTO activity(contact_id, note, created_at) VALUES(?,?,?)", contact_id, note.strip(), db.now())


def latest_note(contact_id: int) -> Optional[dict[str, Any]]:
    return db.one("SELECT * FROM activity WHERE contact_id=? ORDER BY id DESC LIMIT 1", contact_id)


# ── invitation hold ─────────────────────────────────────────────────────────

def _decide(contact: dict[str, Any], survey_no: int) -> dict[str, Any]:
    """One invitation: the plain rules first, then the note, read only when there is one."""
    if contact["unsubscribed"]:
        return {"status": "not_sent", "reason": "unsubscribed"}
    if contact["flagged"]:
        return {"status": "not_sent", "reason": f"flagged: {contact['flagged']}"}
    note = contact.get("note") or latest_note(contact["id"])
    if not note or not modules.is_on(HOLD_MODULE):
        return {"status": "sent", "reason": "no Activity note" if not note else "notes are not read (module off)"}
    result = gateway.call("POST", "/v1/classify",
                          {"text": note["note"], "tasks": [TIMING_TASK], "context": {"activity_note": note["note"]}},
                          module=HOLD_MODULE, trigger="invitation queued", kind="submit", survey_no=survey_no)
    if result.fallback:
        # Today's behaviour: the note is ignored and the invitation goes out.
        gateway.set_outcome(result.log_id, "sent (fallback: the note was not read)")
        return {"status": "sent", "reason": f"note not read ({result.reason})", "log_id": result.log_id}
    answer = result.answers.get(TIMING_TASK) or {}
    decision = {"label": answer.get("label"), "confidence": answer.get("confidence"), "band": answer.get("band"),
                "log_id": result.log_id}
    acted = answer.get("band") == "act" and not answer.get("_forced")
    if acted and answer.get("label") == "hold":
        decision.update(status="held", reason="the latest note says this is a bad time")
    elif acted and answer.get("label") == "dont_send":
        decision.update(status="not_sent", reason="the latest note says this contact should not be surveyed")
    else:
        decision.update(status="sent", reason="nothing in the note" if acted else "unsure about the note: sent as today")
    gateway.set_outcome(result.log_id, f"invitation {decision['status']}")
    return decision


def queue(survey_no: int) -> dict[str, int]:
    """Invite every contact to a project. Each decision is kept, and a sent invitation
    is written to the email outbox."""
    proj = engine.project(survey_no)
    counts = {status: 0 for status in STATUSES}
    for contact in contacts():
        decision = _decide(contact, survey_no)
        counts[decision["status"]] += 1
        db.run("INSERT INTO invitation(contact_id, survey_no, status, reason, label, confidence, band, log_id,"
               " created_at) VALUES(?,?,?,?,?,?,?,?,?)", contact["id"], survey_no, decision["status"],
               decision["reason"], decision.get("label"), decision.get("confidence"), decision.get("band"),
               decision.get("log_id"), db.now())
        if decision["status"] == "sent":
            _email(survey_no, contact["email"], f"[{proj['name']}] We would value your feedback",
                   f"Dear {contact['name']},\nPlease take our short survey: /take/{survey_no}")
    return counts


def invitations(limit: int = 200) -> list[dict[str, Any]]:
    return db.rows("SELECT i.*, c.name, c.email, p.name AS project FROM invitation i JOIN contact c"
                   " ON c.id=i.contact_id LEFT JOIN project p ON p.survey_no=i.survey_no"
                   " ORDER BY i.id DESC LIMIT ?", limit)


def _email(survey_no: Optional[int], to: str, subject: str, body: str) -> None:
    db.run("INSERT INTO sink_log(sink, survey_no, rule_id, response_id, payload, created_at)"
           " VALUES('email', ?, NULL, NULL, ?, ?)", survey_no, db.dumps({"to": to, "subject": subject, "body": body}),
           db.now())


# ── invitation replies ──────────────────────────────────────────────────────

def receive_reply(contact_id: int, text: str) -> str:
    """Sort one reply and act on it. Returns what was done."""
    contact = db.one("SELECT * FROM contact WHERE id=?", contact_id)
    text = (text or "").strip()
    if not contact or not text:
        return "Pick a contact and paste the reply."
    answer: dict[str, Any] = {}
    log_id: Optional[int] = None
    action = "left in the inbox for a person"
    if modules.is_on(REPLY_MODULE):
        result = gateway.call("POST", "/v1/classify",
                              {"text": text, "tasks": [REPLY_TASK], "context": {"email_reply": text}},
                              module=REPLY_MODULE, trigger="reply received", kind="submit")
        log_id = result.log_id
        answer = {} if result.fallback else (result.answers.get(REPLY_TASK) or {})
        label, band = answer.get("label"), "none" if answer.get("_forced") else answer.get("band")
        # Wrongly unsubscribing someone is the safer mistake, so a likely request is enough.
        if label == "unsubscribe" and band in ("act", "suggest"):
            db.run("UPDATE contact SET unsubscribed=1 WHERE id=?", contact_id)
            action = "contact unsubscribed"
        elif label == "wrong_person" and band == "act":
            db.run("UPDATE contact SET flagged='wrong person' WHERE id=?", contact_id)
            action = "address flagged as the wrong person; no more invitations"
        elif label == "out_of_office" and band == "act":
            action = "ignored (automatic reply)"
        elif label in ("feedback", "question") and band == "act":
            _email(None, ACCOUNT_INBOX, f"Reply from {contact['name']}: {REPLY_LABELS[label].lower()}", text)
            action = f"forwarded to the account as {REPLY_LABELS[label].lower()}"
        gateway.set_outcome(log_id, action)
    db.run("INSERT INTO email_reply(contact_id, text, label, confidence, band, action, log_id, created_at)"
           " VALUES(?,?,?,?,?,?,?,?)", contact_id, text, answer.get("label"), answer.get("confidence"),
           answer.get("band"), action, log_id, db.now())
    return f"Reply from {contact['name']}: {action}."


def replies(limit: int = 200) -> list[dict[str, Any]]:
    return db.rows("SELECT e.*, c.name, c.email FROM email_reply e JOIN contact c ON c.id=e.contact_id"
                   " ORDER BY e.id DESC LIMIT ?", limit)
