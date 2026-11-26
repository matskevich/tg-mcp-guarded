"""Validate advertised named arguments before an SDK can silently discard them.

Declared object fields are closed by default. Explicit extensible maps (including
opaque rich-message data) retain additionalProperties. No argument values enter
errors, and schema references cannot retrieve network resources.
"""
import re
from copy import deepcopy

from jsonschema import Draft202012Validator
from referencing import Registry


class ArgumentContractError(ValueError):
    pass


def closed_schema(schema):
    value = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        if isinstance(node.get("properties"), dict) and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
            for child in node.get(key, {}).values():
                visit(child)
        for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
            visit(node.get(key))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for child in node.get(key, []):
                visit(child)

    visit(value)
    return value


def check_arguments(schema, arguments):
    errors = Draft202012Validator(closed_schema(schema), registry=Registry()).iter_errors(arguments)
    error = next(errors, None)
    if error is None:
        return
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        fields = set(error.instance) - set(error.schema.get("properties", {}))
        patterns = error.schema.get("patternProperties", {})
        fields = [str(key) for key in fields if not any(re.search(p, str(key)) for p in patterns)]
        names = [key[:80] if re.fullmatch(r"[A-Za-z0-9_.-]+", key) else "<invalid-field-name>"
                 for key in sorted(fields)[:20]]
        raise ArgumentContractError("unknown_tool_arguments:" + ",".join(names))
    raise ArgumentContractError("invalid_tool_arguments:" + str(error.validator))
