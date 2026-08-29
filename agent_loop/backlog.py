"""Reader for a consumer repository's ``.agent-loop/backlog.yaml``.

File order is priority order, so the item list keeps the order it was written in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import yaml

from .errors import ConfigError


@dataclass(frozen=True)
class Item:
    id: str
    group: str
    statement: str
    cost_class: str
    selectable: bool
    sites: Tuple[str, ...]
    design_doc: str
    probe: Optional[str] = None
    proof: Optional[str] = None
    cost_class_reason: str = ""
    notes: str = ""


def _item(raw: Mapping[str, Any], index: int) -> Item:
    if not isinstance(raw, dict):
        raise ConfigError("backlog item %d is not a mapping" % index)
    for key in ("id", "statement", "cost_class"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ConfigError("backlog item %d is missing a string %r" % (index, key))
    if not isinstance(raw.get("selectable"), bool):
        raise ConfigError("backlog item %r needs a boolean 'selectable'" % raw["id"])
    sites = raw.get("sites") or []
    if not isinstance(sites, list) or not all(isinstance(site, str) for site in sites):
        raise ConfigError("backlog item %r has a malformed 'sites'" % raw["id"])
    return Item(
        id=raw["id"],
        group=str(raw.get("group", "")),
        statement=raw["statement"],
        cost_class=raw["cost_class"],
        selectable=raw["selectable"],
        sites=tuple(sites),
        design_doc=str(raw.get("design_doc", "")),
        probe=raw.get("probe"),
        proof=raw.get("proof"),
        cost_class_reason=str(raw.get("cost_class_reason", "")),
        notes=str(raw.get("notes", "")),
    )


def load(path: os.PathLike) -> Tuple[Item, ...]:
    backlog_path = Path(path)
    try:
        document = yaml.safe_load(backlog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError("cannot read backlog %s: %s" % (backlog_path, exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError("backlog %s is not valid YAML: %s" % (backlog_path, exc)) from exc
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ConfigError("backlog %s must be a mapping with an 'items' list" % backlog_path)
    items = tuple(_item(raw, index) for index, raw in enumerate(document["items"]))
    seen = set()
    for item in items:
        if item.id in seen:
            raise ConfigError("backlog has a duplicate item id %r" % item.id)
        seen.add(item.id)
    return items
