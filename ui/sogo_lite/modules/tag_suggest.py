"""Scope 6: tag suggestions (plan section 8.6).

"Suggest tags" runs exact-name auto-map first, then asks the gateway about whatever
is still untagged, one tag category at a time. Answer options get a `choice` over
the category's tags; questions get a `labels` question so they can take several.
Only question and answer wording is sent. Nothing is applied until Accept.

The option limit: a category can hold 500 tags, the model about 15 options. The
category is dry-run through /v1/tasks/compile first. If it does not fit, each item
is sent with a shortlist of up to 14 tags chosen by word overlap. Which path an
item took is recorded on the suggestion so the two can be compared.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sogo_lite import db, engine, gateway
from sogo_lite.modules import shared_classes

MODULE = "tag_suggest"
SHORTLIST_SIZE = 14
BATCH_MAX_ITEMS = 256
SPEC_ID = "tag"
_STOPWORDS = {"the", "and", "for", "was", "were", "your", "you", "how", "what", "which", "did", "with",
              "our", "are", "rate", "this", "that", "have", "has", "from", "any", "please"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words
            if len(w) > 2 and w not in _STOPWORDS}


def shortlist(text: str, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Up to 14 tags sharing a word with the item, best overlap first."""
    words = _tokens(text)
    scored = [(len(words & _tokens(tag["name"])), tag) for tag in tags]
    scored = [(score, tag) for score, tag in scored if score > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [tag for _, tag in scored[:SHORTLIST_SIZE]]


def _spec(target: str, tags: list[dict[str, Any]]) -> dict[str, Any]:
    classes = {tag["name"]: "" for tag in tags}
    if target == "option" and len(tags) >= 2:
        # The gateway adds the escape option, which covers "none of these tags".
        return {"id": SPEC_ID, "type": "choice", "classes": classes,
                "instructions": "Which of these tags best describes this survey answer option?"}
    return {"id": SPEC_ID, "type": "labels", "classes": classes}


def _fits(survey_no: int, spec: dict[str, Any], category: str) -> Optional[bool]:
    """True: send the full list. False: shortlist. None: the gateway is unavailable."""
    # Part of the batch action, so it runs under the batch timeout like the calls it guards.
    result = gateway.call("POST", "/v1/tasks/compile", {"questions": [spec]}, module=MODULE,
                          trigger=f"Suggest tags: budget check ({category})", kind="batch", survey_no=survey_no)
    if result.fallback:
        # A 422 is the gateway refusing the question set (too many options), not an outage.
        fits = False if result.status == 422 else None
        gateway.set_outcome(result.log_id, "does not compile: shortlist" if fits is False
                            else "budget check unavailable")
        return fits
    lines = (result.data.get("budget") or {}).get("lines") or []
    over = any(line["status"] == "over" for line in lines)
    gateway.set_outcome(result.log_id, "over budget: shortlist" if over else "fits: full tag list")
    return not over


def _decode(spec: dict[str, Any], tags: list[dict[str, Any]],
            answers: dict[str, dict[str, Any]]) -> list[tuple[dict[str, Any], float, str]]:
    """(tag, confidence, band) for every tag worth showing: band act or suggest."""
    picks: list[tuple[dict[str, Any], float, str]] = []
    if spec["type"] == "choice":
        answer = answers.get(SPEC_ID) or {}
        tag = next((t for t in tags if t["name"] == answer.get("label")), None)
        if tag and answer.get("band") in ("act", "suggest"):
            picks.append((tag, float(answer.get("confidence") or 0.0), answer["band"]))
        return picks
    for tag in tags:
        answer = answers.get(f"{SPEC_ID}.{shared_classes.label_slug(tag['name'])}") or {}
        if answer.get("band") in ("act", "suggest"):
            picks.append((tag, float(answer.get("value") or 0.0), answer["band"]))
    return picks


def _untagged(survey_no: int, category_id: int) -> dict[str, list[dict[str, Any]]]:
    """Items exact auto-map and the author left untagged in this category."""
    option_tags, question_tags = engine.option_tags(survey_no), engine.question_tags(survey_no)
    items: dict[str, list[dict[str, Any]]] = {"option": [], "question": []}
    for q in engine.questions(survey_no):
        if not any(t["category_id"] == category_id for t in question_tags.get(q["id"], [])):
            items["question"].append({"id": f"question:{q['id']}", "text": q["wording"]})
        for option in q["options"]:
            if not any(t["category_id"] == category_id for t in option_tags.get(option["id"], [])):
                items["option"].append({"id": f"option:{option['id']}", "text": option["text"],
                                        "survey_question": q["wording"]})
    return items


def suggest(survey_no: int) -> dict[str, Any]:
    """Run "Suggest tags" for a project. Returns a summary for the review list."""
    summary: dict[str, Any] = {"automapped": engine.automap(survey_no), "created": 0, "batch_calls": 0,
                               "full": 0, "shortlist": 0, "no_candidates": 0, "unavailable": False}
    db.run("DELETE FROM suggestion WHERE kind='tag' AND survey_no=? AND status='pending'", survey_no)
    for category in engine.tag_categories():
        tags = category["tags"]
        if not tags:
            continue
        for target, items in _untagged(survey_no, category["id"]).items():
            if not items:
                continue
            fits = _fits(survey_no, _spec(target, tags), category["name"])
            if fits is None:
                summary["unavailable"] = True
                return summary
            # plan: (candidate tags, items, path)
            plans: list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]] = []
            if fits:
                plans.append((tags, items, "full"))
            else:
                groups: dict[tuple[int, ...], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
                for item in items:
                    candidates = shortlist(item["text"], tags)
                    if not candidates:
                        summary["no_candidates"] += 1
                        continue
                    key = tuple(t["id"] for t in candidates)
                    groups.setdefault(key, (candidates, []))[1].append(item)
                plans += [(candidates, grouped, "shortlist") for candidates, grouped in groups.values()]
            for candidates, planned, path in plans:
                summary[path] += len(planned)
                _run(survey_no, category, target, candidates, planned, path, summary)
    return summary


def _run(survey_no: int, category: dict[str, Any], target: str, candidates: list[dict[str, Any]],
         items: list[dict[str, Any]], path: str, summary: dict[str, Any]) -> None:
    spec = _spec(target, candidates)
    for start in range(0, len(items), BATCH_MAX_ITEMS):
        chunk = items[start:start + BATCH_MAX_ITEMS]
        result = gateway.call("POST", "/v1/classify/batch", {"questions": [spec], "items": chunk},
                              module=MODULE, trigger=f"Suggest tags ({category['name']}, {target}s, {path})",
                              kind="batch", survey_no=survey_no)
        summary["batch_calls"] += 1
        if result.fallback:
            summary["unavailable"] = True
            gateway.set_outcome(result.log_id, "no suggestions available")
            continue
        created = 0
        for item_result in result.data.get("results") or []:
            target_type, _, target_id = str(item_result.get("id") or "").partition(":")
            for tag, confidence, band in _decode(spec, candidates, item_result.get("answers") or {}):
                db.run("INSERT INTO suggestion(kind, survey_no, target_type, target_id, proposed, confidence,"
                       " band, detail, created_at) VALUES('tag',?,?,?,?,?,?,?,?)", survey_no, target_type,
                       int(target_id), db.dumps({"tag_id": tag["id"], "tag": tag["name"],
                                                 "category": category["name"]}),
                       confidence, band, db.dumps({"path": path, "candidates": len(candidates)}), db.now())
                created += 1
        summary["created"] += created
        gateway.set_outcome(result.log_id, f"{created} suggestions from {len(chunk)} items ({path} tag list, "
                                           f"{len(candidates)} tags)")


def review_list(survey_no: int) -> list[dict[str, Any]]:
    """Pending suggestions with the text of what they would tag."""
    found = db.rows("SELECT * FROM suggestion WHERE kind='tag' AND survey_no=? AND status='pending'"
                    " ORDER BY target_type DESC, target_id, confidence DESC", survey_no)
    for s in found:
        s["proposed"] = db.loads(s["proposed"], {})
        s["detail"] = db.loads(s["detail"], {})
        if s["target_type"] == "option":
            row = db.one("SELECT o.text, q.wording FROM answer_option o JOIN question q ON q.id=o.question_id"
                         " WHERE o.id=?", s["target_id"])
            s["target_text"] = f"{row['text']}" if row else "(deleted)"
            s["target_context"] = row["wording"] if row else ""
        else:
            s["target_text"] = db.val("SELECT wording FROM question WHERE id=?", s["target_id"]) or "(deleted)"
            s["target_context"] = ""
    return found


def apply(suggestion: dict[str, Any]) -> None:
    proposed = db.loads(suggestion["proposed"], {}) if isinstance(suggestion["proposed"], str) \
        else suggestion["proposed"]
    if suggestion["target_type"] == "option":
        engine.tag_option(suggestion["target_id"], proposed["tag_id"], "suggestion")
    else:
        engine.tag_question(suggestion["target_id"], proposed["tag_id"], "suggestion")
