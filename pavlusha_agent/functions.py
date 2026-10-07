"""Opt-in trusted local capabilities. No Core objects are passed to user code."""
from __future__ import annotations

import importlib.util
import inspect
import json
import math
import sys
import types
import typing
import uuid
from pathlib import Path

from .core import AgentError


def type_schema(annotation):
    primitives = {str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}
    if annotation in primitives:
        return {"type": primitives[annotation]}
    if annotation is typing.Any:
        return {}
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [type_schema(arg) for arg in args]}
    if annotation is list or origin is list:
        return {"type": "array", "items": type_schema(args[0]) if args else {}}
    if annotation is dict or origin is dict:
        if args and args[0] is not str:
            raise ValueError("dict keys must be str")
        return {"type": "object", "additionalProperties": type_schema(args[1]) if args else {}}
    raise ValueError(f"unsupported annotation: {annotation!r}")


def validate_json(value, schema, path="value"):
    """Validate actual JSON types, including arbitrary JSON inside Any/bare containers."""
    if value is None or type(value) in (str, bool, int):
        pass
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite number")
    elif type(value) is list:
        for i, item in enumerate(value):
            validate_json(item, {}, f"{path}[{i}]")
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path}: object keys must be strings")
            validate_json(item, {}, f"{path}.{key}")
    else:
        raise ValueError(f"{path}: unsupported JSON value {type(value).__name__}")
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            try:
                validate_json(value, variant, path)
                return
            except ValueError:
                pass
        raise ValueError(f"{path}: does not match declared union")
    expected = schema.get("type")
    matches = {
        "null": value is None, "string": type(value) is str,
        "boolean": type(value) is bool, "integer": type(value) is int,
        "number": type(value) in (int, float), "array": type(value) is list,
        "object": type(value) is dict,
    }
    if expected and not matches[expected]:
        raise ValueError(f"{path}: expected {expected}")
    if expected == "array":
        for i, item in enumerate(value):
            validate_json(item, schema["items"], f"{path}[{i}]")
    if expected == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}: missing required argument {key}")
        for key, item in value.items():
            if key in properties:
                validate_json(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path}: unexpected argument {key}")
            else:
                validate_json(item, schema.get("additionalProperties", {}), f"{path}.{key}")


class FunctionRegistry:
    def __init__(self, paths):
        # Preserve the single-path API as well as the ordered repeatable CLI input.
        if isinstance(paths, (str, Path)):
            paths = [paths]
        functions, descriptions, sources, loaded = {}, [], {}, []
        try:
            for path in paths:
                source = Path(path).expanduser().resolve()
                module_name, module_functions, module_descriptions = self._load_module(source)
                loaded.append(module_name)
                for name in module_functions:
                    if name in functions:
                        raise AgentError(
                            f"Duplicate function name {name!r} exported by {sources[name]} and {source}"
                        )
                    sources[name] = source
                functions.update(module_functions)
                descriptions.extend(module_descriptions)
        except (Exception, SystemExit):
            for module_name in loaded:
                sys.modules.pop(module_name, None)
            raise
        # Publish only after every module succeeds. Imports are trusted code; their
        # external side effects cannot be rolled back by this registry transaction.
        self.functions = functions
        self.descriptions = descriptions

    @staticmethod
    def _load_module(path):
        functions, descriptions = {}, []
        source = Path(path).expanduser().resolve()
        module_name = "_pavlusha_user_functions_" + uuid.uuid4().hex
        try:
            spec = importlib.util.spec_from_file_location(module_name, source)
            if spec is None or spec.loader is None or source.suffix != ".py":
                raise ValueError("--functions requires a Python .py file")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            # Export only public functions defined here; imported helpers are never registered.
            for name, function in vars(module).items():
                if name.startswith("_") or not inspect.isfunction(function) or function.__module__ != module_name:
                    continue
                if name != function.__name__:
                    raise ValueError(f"{name}: aliases are not supported")
                if not name.isidentifier() or len(name) > 128:
                    raise ValueError(f"{name}: expected a Python identifier of at most 128 characters")
                if (inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function)
                        or inspect.isasyncgenfunction(function)):
                    raise ValueError(f"{name}: only synchronous non-generator functions are supported")
                signature = inspect.signature(function)
                hints = typing.get_type_hints(function)
                properties, required = {}, []
                for parameter in signature.parameters.values():
                    if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
                        raise ValueError(f"{name}.{parameter.name}: positional-only and variadic arguments are unsupported")
                    if parameter.name not in hints:
                        raise ValueError(f"{name}.{parameter.name}: type annotation required")
                    schema = type_schema(hints[parameter.name])
                    if parameter.default is parameter.empty:
                        required.append(parameter.name)
                    else:
                        validate_json(parameter.default, schema, f"{name}.{parameter.name} default")
                        schema = {**schema, "default": json.loads(json.dumps(parameter.default, allow_nan=False))}
                    properties[parameter.name] = schema
                arguments = {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}
                result = type_schema(hints["return"]) if "return" in hints else {}
                functions[name] = (function, arguments, result)
                descriptions.append({"name": name, "description": inspect.getdoc(function) or "",
                                          "arguments": arguments, "result": result})
            if not functions:
                raise ValueError("module exports no public functions")
            return module_name, functions, descriptions
        except (Exception, SystemExit) as exc:
            sys.modules.pop(module_name, None)
            raise AgentError(f"Cannot load functions from {source}: {type(exc).__name__}: {exc}") from exc

    def prompt(self):
        return ('\n\nUser-enabled external capabilities: request {"action":"call_function",'
                '"name":"NAME","arguments":{...}}. Calls are synchronous; results are observations, '
                'not Project State mutations. Shell remains available. Core checkpoint and interactive '
                'gates apply. There is no release_worker or per-call timeout for these calls.\n' +
                json.dumps(self.descriptions, ensure_ascii=False))

    def call(self, action, output_limit):
        name = action.get("name")
        # Avoid echoing arbitrary Worker input into result/history.
        outcome = {"name": name if type(name) is str and len(name) <= 128 else None}
        def error(kind, detail):
            return {**outcome, "error": kind, "detail": detail[:min(output_limit, 1200)]}
        if type(name) is not str or not name.isidentifier():
            return error("invalid_arguments", "name must be a registered Python identifier")
        if name not in self.functions:
            return error("unknown_function", "function is not registered for this run")
        function, arguments_schema, result_schema = self.functions[name]
        try:
            if set(action) != {"action", "name", "arguments"}:
                raise ValueError("expected only action, name and arguments")
            arguments = action["arguments"]
            validate_json(arguments, arguments_schema, "arguments")
        except (ValueError, RecursionError) as exc:
            return error("invalid_arguments", str(exc))
        try:
            result = function(**arguments)
        except (Exception, SystemExit) as exc:
            return error("function_exception", f"{type(exc).__name__}: {exc}")
        try:
            validate_json(result, result_schema, "result")
            # Bound admission without constructing a second megabyte-sized JSON string.
            chunks, size = [], 0
            for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(result):
                size += len(chunk)
                if size > output_limit:
                    return {**outcome, "error": "output_limit_exceeded", "output_withheld": True,
                            "output_limit_chars": output_limit}
                chunks.append(chunk)
            return {**outcome, "result": json.loads("".join(chunks))}
        except (ValueError, TypeError, RecursionError) as exc:
            return error("invalid_result", f"{type(exc).__name__}: {exc}")
