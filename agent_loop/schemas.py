"""The output schema each role must answer with."""

from __future__ import annotations

WORKER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["diff_applied", "test_path", "mutation_evidence", "status", "reason"],
    "properties": {
        "diff_applied": {"type": "boolean"},
        "test_path": {"type": "string"},
        "mutation_evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reverted_command", "observed_failure_line"],
            "properties": {
                "reverted_command": {"type": "string"},
                "observed_failure_line": {"type": "string"},
            },
        },
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "reason": {"type": "string"},
    },
}


def validate(schema, value, path="output"):
    """Check ``value`` against the subset of JSON Schema these schemas use.

    Returns a reason string, or None when the value fits.
    """
    if value is None:
        return "%s is absent" % path
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return "%s is not an object" % path
        for key in schema.get("required", []):
            if key not in value:
                return "%s is missing required key %r" % (path, key)
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(schema.get("properties", {}))
            if unknown:
                return "%s has unknown keys: %s" % (path, sorted(unknown))
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                reason = validate(sub, value[key], "%s.%s" % (path, key))
                if reason is not None:
                    return reason
        return None
    if expected == "boolean" and not isinstance(value, bool):
        return "%s is not a boolean" % path
    if expected == "string":
        if not isinstance(value, str):
            return "%s is not a string" % path
        if "enum" in schema and value not in schema["enum"]:
            return "%s must be one of %s" % (path, schema["enum"])
    return None
