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

# ROADMAP.md §4 Stage 5: the planner proposes backlog items.  ``probe`` and
# ``proof`` are required because invariant 2 admits nothing without them, and
# the count is capped in the schema itself so an over-long answer is malformed -
# it takes the existing one-repair path instead of needing a rule of its own.
MAX_PROPOSALS = 5

PLANNER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "maxItems": MAX_PROPOSALS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id", "statement", "cost_class", "sites", "probe", "proof", "rationale",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "cost_class": {"type": "string"},
                    "sites": {"type": "array", "items": {"type": "string"}},
                    "probe": {"type": "string"},
                    "proof": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        }
    },
}

REVIEWER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "location", "claim", "citation"],
                "properties": {
                    "kind": {"type": "string", "enum": ["contract", "defect", "suggestion"]},
                    "location": {"type": "string"},
                    "claim": {"type": "string"},
                    "citation": {"type": "string"},
                },
            },
        }
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
    if expected == "array":
        if not isinstance(value, list):
            return "%s is not an array" % path
        limit = schema.get("maxItems")
        if limit is not None and len(value) > limit:
            return "%s has %d items, above the maximum of %d" % (path, len(value), limit)
        for index, element in enumerate(value):
            reason = validate(schema.get("items", {}), element, "%s[%d]" % (path, index))
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
