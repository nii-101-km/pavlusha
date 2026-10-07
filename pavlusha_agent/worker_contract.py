"""Generation schema for the existing Worker JSON actions; Core remains authoritative."""
from __future__ import annotations

from .gui import WIDTH, HEIGHT, MAX_DELAY, MAX_TEXT, PHYSICAL_KEYS, MODIFIER_KEYS, MAX_HOLD_SECONDS
from .project_state import WORK_STATUSES


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def _variant(field, name, properties=None, required=()):
    return _object({field: {"const": name}, **(properties or {})}, (field, *required))


def worker_response_format(*, initialized: bool, checkpoint_required: bool = False,
                           periodic_review: bool = False, gui_enabled: bool = False,
                           expert_enabled: bool = False, interactive: bool = False,
                           functions: list | None = None) -> dict:
    """Select canonical response shapes using the same phase gates as run_agent.

    Whitespace normalization, state IDs/transitions, byte limits, permissions and
    execution bounds are still checked by Core, not inferred by this schema.
    """
    text = {"type": "string", "minLength": 1}
    string = {"type": "string"}
    boolean = {"type": "boolean"}
    integer = {"type": "integer"}

    def strings(*, nonempty=False):
        return {"type": "array" if nonempty else ["array", "null"],
                "items": text, "uniqueItems": True, **({"minItems": 1} if nonempty else {})}

    initial_work = _object({"objective": text, "status": {"enum": ["PLANNED", "ACTIVE"]},
                            "deliverables": strings()}, ("objective",))
    design = _object({"decision": text, "rationale": text}, ("decision", "rationale"))
    initial = _variant("action", "project_init", {
        "design": {"type": "array", "items": design},
        "work": {"type": "array", "items": initial_work, "minItems": 2},
    }, ("work",))
    work_update = _variant("op", "update_work", {
        "id": text, "status": {"enum": sorted(WORK_STATUSES)}, "evidence": strings(),
        "reason": text, "deviation": text,
    }, ("id",))
    # op + id alone is not a mutation. Keep at least one mutable field present.
    work_update["minProperties"] = 3
    changes = [
        _variant("op", "add_design", {"decision": text, "rationale": text}, ("decision", "rationale")),
        _variant("op", "supersede_design", {"id": text, "deviation": text}, ("id", "deviation")),
        _variant("op", "add_work", initial_work["properties"], ("objective",)),
        work_update,
        _variant("op", "record_deviation", {
            "affects": strings(nonempty=True), "original": text, "actual": text, "reason": text,
        }, ("affects", "original", "actual", "reason")),
    ]
    update = _variant("action", "project_update", {
        "changes": {"type": "array", "items": {"anyOf": changes}, "minItems": 1, "maxItems": 32},
    }, ("changes",))
    if not initialized:
        actions = [initial]
    elif checkpoint_required:
        actions = [update, _variant("action", "project_review_complete", {
            "note": {"type": ["string", "null"]}, "handoff": string,
        })]
    elif periodic_review:
        actions = [update, _variant("action", "project_review_skip")]
    else:
        actions = [
            _variant("action", "shell", {"command": text, "network": boolean, "gpu": boolean,
                                         "release_worker": boolean, "timeout": integer}, ("command",)),
            _variant("action", "finish", {"summary": text}, ("summary",)),
            update,
        ]
        if expert_enabled:
            actions.append(_variant("action", "ask_expert", {"question": text, "context": string},
                                    ("question", "context")))
        for function in functions or []:
            actions.append(_variant("action", "call_function", {
                "name": {"const": function["name"]}, "arguments": function["arguments"],
            }, ("name", "arguments")))
        if gui_enabled:
            delay = {"type": ["number", "null"], "minimum": 0, "maximum": MAX_DELAY}
            x = {"type": "integer", "minimum": 0, "maximum": WIDTH - 1}
            y = {"type": "integer", "minimum": 0, "maximum": HEIGHT - 1}
            actions.extend([
                _variant("action", "gui_start", {"command": text, "network": boolean,
                                                  "timeout": integer, "delay": delay}, ("command",)),
                _variant("action", "view_gui", {"delay": delay}),
                _variant("action", "click", {"x": x, "y": y, "delay": delay}, ("x", "y")),
                _variant("action", "right_click", {"x": x, "y": y, "delay": delay}, ("x", "y")),
                _variant("action", "drag", {"x1": x, "y1": y, "x2": x, "y2": y, "delay": delay},
                         ("x1", "y1", "x2", "y2")),
                _variant("action", "type_text", {"text": {"type": "string", "maxLength": MAX_TEXT},
                                                  "delay": delay}, ("text",)),
                _variant("action", "gui_close", {"delay": delay}),
                _variant('action', 'press_key', {
                    'key': {'enum': sorted(PHYSICAL_KEYS)},
                    'modifiers': {'type': 'array', 'items': {'enum': list(MODIFIER_KEYS)},
                                  'uniqueItems': True, 'maxItems': len(MODIFIER_KEYS)},
                    'delay': delay}, ('key',)),
                _variant('action', 'hold_key', {
                    'key': {'enum': sorted(PHYSICAL_KEYS)},
                    'duration': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': MAX_HOLD_SECONDS},
                    'delay': delay}, ('key', 'duration')),
            ])
    if interactive:
        actions.extend([_variant("action", name, {"text": text}, ("text",))
                        for name in ("message", "wait_for_user")])
    schema = actions[0] if len(actions) == 1 else {"anyOf": actions}
    return {"type": "json_schema", "json_schema": {
        "name": "worker_action", "strict": True, "schema": schema,
    }}
