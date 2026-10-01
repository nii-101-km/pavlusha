"""Controller-owned durable project state for fast recovery after bounded-context loss.

Project State is not a transcript or a reasoning summary.  It records durable project
intent (design/work/deviations) plus Worker-selected evidence pointers for recovery.
The Worker proposes typed mutations; Core validates only structural state invariants and
publishes the canonical representation atomically through StateStore.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from .core import AgentError

WORK_STATUSES = frozenset({"PLANNED", "ACTIVE", "DONE", "BLOCKED", "SUPERSEDED"})
DESIGN_STATUSES = frozenset({"ACTIVE", "SUPERSEDED"})

# One operational paragraph, bounded in UTF-8 bytes (including non-ASCII text).
HANDOFF_MAX_BYTES = 2048


def validate_handoff(value: Any) -> str:
    try:
        valid = isinstance(value, str) and len(value.encode("utf-8")) <= HANDOFF_MAX_BYTES
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise AgentError(f"project_review_complete.handoff must be text of at most {HANDOFF_MAX_BYTES} UTF-8 bytes")
    return value.strip()


def handoff_message(value: str) -> dict[str, str] | None:
    if not value:
        return None
    return {"role": "user", "content": (
        "CHECKPOINT HANDOFF (latest operational continuation; may be stale):\n"
        "Reconcile with /work, Project State and Project Map, which take precedence.\n" + value
    )}


def empty_project_state() -> dict[str, Any]:
    return {
        "revision": 0,
        "initialized": False,
        "last_review_operation": 0,
        "design": [],
        "work": [],
        "deviations": [],
        "recovery": {"active": None},
        "counters": {"design": 0, "work": 0, "deviation": 0},
    }


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if value is None and allow_empty:
        return []
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise AgentError(f"{field} must be {qualifier} of strings")
    out: list[str] = []
    for item in value:
        out.append(_text(item, field))
    if len(set(out)) != len(out):
        raise AgentError(f"{field} must not contain duplicates")
    return out


def normalize_evidence(value: Any, field: str = "evidence") -> list[str]:
    """Normalize Worker-selected recovery evidence without judging its sufficiency.

    Evidence is intentionally opaque to Core.  The Worker contract says entries must point
    to FILES and/or SHELL OPERATIONS / RESULTS; Core only keeps the field well-formed.
    """
    return _string_list(value, field)

def validate_project_action(action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = action.get("action")
    if kind == "project_init":
        raw_design = action.get("design", [])
        raw_work = action.get("work")
        if not isinstance(raw_design, list):
            raise AgentError("project_init.design must be a list")
        if not isinstance(raw_work, list) or len(raw_work) < 2:
            raise AgentError("project_init.work must contain at least two concrete plan items")
        design: list[dict[str, str]] = []
        for index, item in enumerate(raw_design):
            if not isinstance(item, dict):
                raise AgentError(f"project_init.design[{index}] must be an object")
            design.append({
                "decision": _text(item.get("decision"), f"project_init.design[{index}].decision"),
                "rationale": _text(item.get("rationale"), f"project_init.design[{index}].rationale"),
            })
        work: list[dict[str, Any]] = []
        active = 0
        for index, item in enumerate(raw_work):
            if not isinstance(item, dict):
                raise AgentError(f"project_init.work[{index}] must be an object")
            status = str(item.get("status", "PLANNED")).strip().upper()
            if status not in {"PLANNED", "ACTIVE"}:
                raise AgentError("initial work status must be PLANNED or ACTIVE")
            active += int(status == "ACTIVE")
            work.append({
                "objective": _text(item.get("objective"), f"project_init.work[{index}].objective"),
                "status": status,
                "deliverables": _string_list(item.get("deliverables", []), f"project_init.work[{index}].deliverables"),
            })
        if active != 1:
            raise AgentError("project_init requires exactly one ACTIVE work item")
        if not any(item["status"] == "PLANNED" for item in work):
            raise AgentError("project_init requires at least one PLANNED work item beyond the active item")
        return kind, {"design": design, "work": work}

    if kind == "project_update":
        raw_changes = action.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes or len(raw_changes) > 32:
            raise AgentError("project_update.changes must be a non-empty list of at most 32 operations")
        changes: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_changes):
            if not isinstance(raw, dict):
                raise AgentError(f"project_update.changes[{index}] must be an object")
            op = _text(raw.get("op"), f"project_update.changes[{index}].op")
            if op == "add_design":
                changes.append({"op": op, "decision": _text(raw.get("decision"), "add_design.decision"),
                                "rationale": _text(raw.get("rationale"), "add_design.rationale")})
            elif op == "supersede_design":
                changes.append({"op": op, "id": _text(raw.get("id"), "supersede_design.id"),
                                "deviation": _text(raw.get("deviation"), "supersede_design.deviation")})
            elif op == "add_work":
                status = str(raw.get("status", "PLANNED")).strip().upper()
                if status not in {"PLANNED", "ACTIVE"}:
                    raise AgentError("add_work.status must be PLANNED or ACTIVE")
                changes.append({"op": op, "objective": _text(raw.get("objective"), "add_work.objective"),
                                "status": status,
                                "deliverables": _string_list(raw.get("deliverables", []), "add_work.deliverables")})
            elif op == "update_work":
                item: dict[str, Any] = {"op": op, "id": _text(raw.get("id"), "update_work.id")}
                if "status" in raw:
                    status = _text(raw.get("status"), "update_work.status").upper()
                    if status not in WORK_STATUSES:
                        raise AgentError(f"update_work.status must be one of {sorted(WORK_STATUSES)}")
                    item["status"] = status
                if "evidence" in raw:
                    item["evidence"] = normalize_evidence(raw.get("evidence"), "update_work.evidence")
                if "reason" in raw:
                    item["reason"] = _text(raw.get("reason"), "update_work.reason")
                if "deviation" in raw:
                    item["deviation"] = _text(raw.get("deviation"), "update_work.deviation")
                if len(item) == 2:
                    raise AgentError("update_work must change status, evidence, reason, or deviation")
                changes.append(item)
            elif op == "record_deviation":
                changes.append({
                    "op": op,
                    "affects": _string_list(raw.get("affects"), "record_deviation.affects", allow_empty=False),
                    "original": _text(raw.get("original"), "record_deviation.original"),
                    "actual": _text(raw.get("actual"), "record_deviation.actual"),
                    "reason": _text(raw.get("reason"), "record_deviation.reason"),
                })
            else:
                raise AgentError(f"unsupported project_update op: {op}")
        return kind, {"changes": changes}

    if kind == "project_review_skip":
        return kind, {}

    if kind == "project_review_complete":
        note = action.get("note", "")
        if note is not None and not isinstance(note, str):
            raise AgentError("project_review_complete.note must be a string")
        return kind, {"note": str(note or "").strip(),
                      "handoff": validate_handoff(action.get("handoff", ""))}

    raise AgentError("not a project-state action")


def _find(items: list[dict[str, Any]], item_id: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise AgentError(f"unknown {kind} id: {item_id}")


def _validate_final(state: dict[str, Any]) -> None:
    active = [item for item in state["work"] if item.get("status") == "ACTIVE"]
    unfinished = [item for item in state["work"] if item.get("status") in {"PLANNED", "ACTIVE"}]
    if unfinished and len(active) != 1:
        raise AgentError("project state with unfinished work must contain exactly one ACTIVE work item")
    if len(active) > 1:
        raise AgentError("project state may not contain more than one ACTIVE work item")
    state["recovery"]["active"] = active[0]["id"] if active else None
    deviation_ids = {item["id"] for item in state["deviations"]}
    for item in state["work"]:
        status = item["status"]
        if status == "BLOCKED" and not item.get("reason"):
            raise AgentError(f"{item['id']} BLOCKED requires reason")
        if status == "SUPERSEDED":
            deviation = item.get("deviation")
            if not deviation or deviation not in deviation_ids:
                raise AgentError(f"{item['id']} SUPERSEDED requires a valid deviation id")
    for item in state["design"]:
        if item["status"] == "SUPERSEDED":
            deviation = item.get("deviation")
            if not deviation or deviation not in deviation_ids:
                raise AgentError(f"{item['id']} SUPERSEDED requires a valid deviation id")


def validate_persisted_project_state(state: Any) -> None:
    """Validate a committed document without repairing it or judging evidence."""
    def integer(value: Any, field: str, minimum: int = 0) -> None:
        if type(value) is not int or value < minimum:
            raise AgentError(f"Project State {field} must be an integer >= {minimum}")

    if not isinstance(state, dict) or state.get("initialized") is not True:
        raise AgentError("committed Project State must be initialized")
    integer(state.get("revision"), "revision", 1)
    integer(state.get("last_review_operation"), "last_review_operation")
    counters = state.get("counters")
    recovery = state.get("recovery")
    if not isinstance(counters, dict) or not isinstance(recovery, dict) or "active" not in recovery:
        raise AgentError("Project State requires counters and recovery.active")
    all_ids: set[str] = set()
    for field, prefix in (("design", "D"), ("work", "W"), ("deviations", "V")):
        items = state.get(field)
        if not isinstance(items, list) or (field == "work" and len(items) < 2):
            raise AgentError(f"Project State {field} must be a list (work requires at least two items)")
        maximum = 0
        for item in items:
            if not isinstance(item, dict):
                raise AgentError(f"Project State {field} entries must be objects")
            item_id = item.get("id")
            if (not isinstance(item_id, str) or not item_id.startswith(prefix)
                    or len(item_id) < 4 or not item_id[1:].isascii() or not item_id[1:].isdigit()
                    or int(item_id[1:]) < 1 or item_id != f"{prefix}{int(item_id[1:]):03d}"
                    or item_id in all_ids):
                raise AgentError(f"invalid or duplicate Project State {field} id")
            all_ids.add(item_id)
            maximum = max(maximum, int(item_id[1:]))
            if field == "work":
                if not isinstance(item.get("status"), str) or item["status"] not in WORK_STATUSES:
                    raise AgentError(f"invalid work status for {item_id}")
                _text(item.get("objective"), f"{item_id}.objective")
                for key in ("deliverables", "evidence"):
                    if not isinstance(item.get(key), list):
                        raise AgentError(f"{item_id} requires a {key} list")
                    _string_list(item[key], f"{item_id}.{key}")
                if "reason" in item:
                    _text(item["reason"], f"{item_id}.reason")
            elif field == "design":
                if not isinstance(item.get("status"), str) or item["status"] not in DESIGN_STATUSES:
                    raise AgentError(f"invalid design status for {item_id}")
                _text(item.get("decision"), f"{item_id}.decision")
                _text(item.get("rationale"), f"{item_id}.rationale")
            else:
                if item.get("status") != "ACTIVE":
                    raise AgentError(f"invalid deviation status for {item_id}")
                _string_list(item.get("affects"), f"{item_id}.affects", allow_empty=False)
                for key in ("original", "actual", "reason"):
                    _text(item.get(key), f"{item_id}.{key}")
        counter = {"design": "design", "work": "work", "deviations": "deviation"}[field]
        integer(counters.get(counter), f"counters.{counter}")
        if counters[counter] != maximum or maximum != len(items):
            raise AgentError(f"Project State counters.{counter} does not match IDs")
    world_ids = {item["id"] for item in state["work"] + state["design"]}
    for item in state["deviations"]:
        if any(target not in world_ids for target in item["affects"]):
            raise AgentError("Project State deviation references an unknown design/work ID")
    checked = copy.deepcopy(state)
    _validate_final(checked)
    if checked["recovery"] != recovery:
        raise AgentError("Project State recovery.active does not match ACTIVE work")


def initialize_project_state(current: dict[str, Any], data: dict[str, Any], *, operation_count: int) -> dict[str, Any]:
    if current.get("initialized"):
        raise AgentError("Project State is already initialized")
    state = empty_project_state()
    for raw in data["design"]:
        state["counters"]["design"] += 1
        state["design"].append({"id": f"D{state['counters']['design']:03d}", "status": "ACTIVE", **copy.deepcopy(raw)})
    for raw in data["work"]:
        state["counters"]["work"] += 1
        state["work"].append({
            "id": f"W{state['counters']['work']:03d}",
            "objective": raw["objective"], "status": raw["status"],
            "deliverables": copy.deepcopy(raw.get("deliverables", [])), "evidence": [],
        })
    state["initialized"] = True
    state["revision"] = 1
    state["last_review_operation"] = operation_count
    _validate_final(state)
    return state


def apply_project_changes(current: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    if not current.get("initialized"):
        raise AgentError("Project State is not initialized")
    state = copy.deepcopy(current)
    for change in changes:
        op = change["op"]
        if op == "add_design":
            state["counters"]["design"] += 1
            state["design"].append({"id": f"D{state['counters']['design']:03d}", "status": "ACTIVE",
                                    "decision": change["decision"], "rationale": change["rationale"]})
        elif op == "supersede_design":
            item = _find(state["design"], change["id"], "design")
            item["status"] = "SUPERSEDED"
            item["deviation"] = change["deviation"]
        elif op == "add_work":
            state["counters"]["work"] += 1
            state["work"].append({"id": f"W{state['counters']['work']:03d}",
                                  "objective": change["objective"], "status": change["status"],
                                  "deliverables": copy.deepcopy(change.get("deliverables", [])), "evidence": []})
        elif op == "update_work":
            item = _find(state["work"], change["id"], "work")
            if "status" in change:
                item["status"] = change["status"]
            if "evidence" in change:
                item["evidence"] = copy.deepcopy(change["evidence"])
            if "reason" in change:
                item["reason"] = change["reason"]
            if "deviation" in change:
                item["deviation"] = change["deviation"]
        elif op == "record_deviation":
            valid_ids = {item["id"] for item in state["design"]} | {item["id"] for item in state["work"]}
            unknown = [item for item in change["affects"] if item not in valid_ids]
            if unknown:
                raise AgentError("deviation affects unknown id(s): " + ", ".join(unknown))
            state["counters"]["deviation"] += 1
            state["deviations"].append({
                "id": f"V{state['counters']['deviation']:03d}", "status": "ACTIVE",
                "affects": copy.deepcopy(change["affects"]), "original": change["original"],
                "actual": change["actual"], "reason": change["reason"],
            })
    state["revision"] = int(state.get("revision", 0)) + 1
    _validate_final(state)
    return state


def complete_review(current: dict[str, Any], *, operation_count: int) -> dict[str, Any]:
    if not current.get("initialized"):
        raise AgentError("Project State is not initialized")
    state = copy.deepcopy(current)
    _validate_final(state)
    state["last_review_operation"] = operation_count
    state["revision"] = int(state.get("revision", 0)) + 1
    return state


def review_due(project_state: dict[str, Any], *, operation_count: int, every: int) -> bool:
    if not project_state.get("initialized") or every <= 0:
        return False
    return operation_count - int(project_state.get("last_review_operation", 0) or 0) >= every


def project_state_message(project_state: dict[str, Any], *, review_required: str | None = None) -> dict[str, str]:
    if not project_state.get("initialized"):
        body = (
            "PROJECT STATE: UNINITIALIZED\n"
            "INITIAL PROJECT PLAN REQUIRED\n"
            "Normal shell/finish actions are blocked until project_init publishes the initial execution plan. "
            "Use TASK and PROJECT MAP to materialize the concrete objectives you currently intend to execute; "
            "do not use project_init as a permission slip and do not collapse the whole task into one generic item. "
            "Create at least two WORK items: exactly one ACTIVE and at least one PLANNED. "
            "Include final verification as its own planned item when completion is verifiable. "
            "Record DESIGN only when an architectural decision is actually known."
        )
    else:
        public = {key: copy.deepcopy(project_state[key]) for key in
                  ("revision", "last_review_operation", "design", "work", "deviations", "recovery")}
        body = (
            "PROJECT STATE (Core-owned durable recovery checkpoint; recovery pointers, not memories)\n"
            "WORK evidence is Worker-selected FILES and/or SHELL OPERATIONS / RESULTS.\n" +
            json.dumps(public, ensure_ascii=False, indent=2)
        )
    if review_required:
        body += (
            "\n\nPROJECT CHECKPOINT REQUIRED\n"
            f"reason: {review_required}\n"
            "Assume recent history/reasoning may disappear. Materialize only durable changes to DESIGN, WORK and "
            "DEVIATIONS. For WORK evidence, point to useful FILES and/or SHELL OPERATIONS / RESULTS. "
            "Do not preserve transient hypotheses or cheap-to-reobserve facts. "
            "Use project_update as needed, then call project_review_complete. Normal shell/finish actions are blocked until then."
        )
    return {"role": "user", "content": body}


def context_notice_message(reason: str) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "CONTEXT NOTICE\n"
            f"{reason}\n"
            "Older working memory was intentionally discarded to save context. This is normal and does not mean project state was lost. "
            "Absence from recent memory is not evidence that work was not performed. Recover from TASK, PROJECT STATE, PROJECT MAP, "
            "and current files/tests/environment. Do not reconstruct discarded reasoning unless current evidence requires it."
        ),
    }
