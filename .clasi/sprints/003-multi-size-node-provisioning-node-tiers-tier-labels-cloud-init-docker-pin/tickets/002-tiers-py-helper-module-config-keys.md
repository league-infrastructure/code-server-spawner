---
id: '002'
title: tiers.py helper module + config keys
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# tiers.py helper module + config keys

## Description

Create `cspawn/cs_docker/tiers.py` — the single module responsible for reading,
parsing, and exposing `NODE_TIERS` config. Add the three new config keys to all
three env config files. This is the foundation ticket: every other sprint ticket
that needs tier information imports from here.

`Config` is raw dotenv strings. JSON keys like `NODE_TIERS` must be parsed by a
helper — no raw `cfg.get("NODE_TIERS")` anywhere else in the codebase. This module
is the enforcement point.

## Acceptance Criteria

- [x] `cspawn/cs_docker/tiers.py` exists and is importable.
- [x] `Tier` is a frozen `dataclass(frozen=True)` with fields `name: str`, `slug: str`, `capacity: int`.
- [x] `load_tiers(cfg) -> list[Tier]` parses `NODE_TIERS` JSON from config; raises `ValueError` on malformed JSON or entries missing required fields.
- [x] `load_tiers(cfg)` falls back to `[Tier(name="default", slug=cfg["DO_SIZE"], capacity=default_capacity(cfg))]` when `NODE_TIERS` is absent or empty.
- [x] `default_tier(cfg) -> Tier` returns the tier named by `DEFAULT_TIER`; falls back to `load_tiers(cfg)[0]` if `DEFAULT_TIER` is absent or doesn't match.
- [x] `tier_by_name(cfg, name: str) -> Tier | None` returns the matching tier or `None`.
- [x] `tier_for_slug(cfg, slug: str) -> Tier | None` returns the tier whose `slug` matches, or `None`.
- [x] `default_capacity(cfg) -> int` returns `int(cfg["DEFAULT_CAPACITY"])` if present and valid, else `6`.
- [x] `node_capacity(node_attrs: dict, cfg) -> int` reads `Spec.Labels["cs.capacity"]` as int; falls back to `default_capacity(cfg)`.
- [x] `config/prod/public.env` has `NODE_TIERS`, `DEFAULT_TIER=small`, `DEFAULT_CAPACITY=6`.
- [x] `config/local-prod/public.env` has the same three keys with the same values.
- [x] `config/devel/public.env` has the same three keys (may use same or test values).
- [x] `DO_SIZE` is retained in all three config files (unchanged).
- [x] Unit tests pass (see Testing Plan).

## Implementation Plan

### Approach

Create a new file in `cspawn/cs_docker/` (alongside `csmanager.py`, `manager.py`).
Then add config keys to the three `public.env` files. No changes to existing modules.

### Files to Create

- `cspawn/cs_docker/tiers.py`

Full implementation:

```python
"""
cspawn/cs_docker/tiers.py — Node size tier config helpers.

All code that reads NODE_TIERS / DEFAULT_TIER / DEFAULT_CAPACITY goes through
this module. Never read these config keys raw elsewhere.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cspawn.util.config import Config


@dataclass(frozen=True)
class Tier:
    """A named node size tier with a DigitalOcean slug and host capacity."""
    name: str
    slug: str
    capacity: int


def load_tiers(cfg) -> list[Tier]:
    """Parse NODE_TIERS JSON from config. Fall back to a synthetic default tier.

    Fallback: if NODE_TIERS is absent or empty, synthesize a single tier from
    DO_SIZE and DEFAULT_CAPACITY. Existing deployments without NODE_TIERS continue
    to work.

    Raises ValueError if NODE_TIERS is present but malformed.
    """
    raw = (cfg.get("NODE_TIERS") or "").strip()
    if not raw:
        slug = cfg.get("DO_SIZE") or "s-1vcpu-1gb"
        return [Tier(name="default", slug=slug, capacity=default_capacity(cfg))]

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"NODE_TIERS is not valid JSON: {exc}") from exc

    if not isinstance(entries, list) or not entries:
        raise ValueError("NODE_TIERS must be a non-empty JSON array")

    tiers = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"NODE_TIERS[{i}] must be a JSON object")
        for field in ("name", "slug", "capacity"):
            if field not in entry:
                raise ValueError(f"NODE_TIERS[{i}] missing required field '{field}'")
        try:
            cap = int(entry["capacity"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"NODE_TIERS[{i}].capacity must be an integer: {exc}") from exc
        tiers.append(Tier(name=str(entry["name"]), slug=str(entry["slug"]), capacity=cap))

    return tiers


def default_tier(cfg) -> Tier:
    """Return the default tier (DEFAULT_TIER name, or tiers()[0])."""
    tiers = load_tiers(cfg)
    name = (cfg.get("DEFAULT_TIER") or "").strip()
    if name:
        for t in tiers:
            if t.name == name:
                return t
    return tiers[0]


def tier_by_name(cfg, name: str) -> Tier | None:
    """Return the tier with the given name, or None."""
    for t in load_tiers(cfg):
        if t.name == name:
            return t
    return None


def tier_for_slug(cfg, slug: str) -> Tier | None:
    """Return the tier whose DO slug matches, or None. Used by label-backfill."""
    for t in load_tiers(cfg):
        if t.slug == slug:
            return t
    return None


def default_capacity(cfg) -> int:
    """Return DEFAULT_CAPACITY from config, or 6 if absent/invalid."""
    raw = cfg.get("DEFAULT_CAPACITY")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return 6


def node_capacity(node_attrs: dict, cfg) -> int:
    """Read cs.capacity from a swarm node's Spec.Labels; fall back to default_capacity."""
    labels = (node_attrs.get("Spec") or {}).get("Labels") or {}
    if "cs.capacity" in labels:
        try:
            return int(labels["cs.capacity"])
        except (TypeError, ValueError):
            pass
    return default_capacity(cfg)
```

- `tests/test_tiers.py` (new unit test file)

### Files to Modify

- `config/prod/public.env` — add after the `DO_SIZE` line:
  ```
  NODE_TIERS=[{"name":"small","slug":"s-4vcpu-8gb-amd","capacity":6},{"name":"large","slug":"s-8vcpu-16gb-amd","capacity":14}]
  DEFAULT_TIER=small
  DEFAULT_CAPACITY=6
  ```

- `config/local-prod/public.env` — same three lines in the same location.

- `config/devel/public.env` — same three lines; devel may use the same slug values.
  Note: devel config is used for local testing without real DO provisioning, so the
  slug values are documentation only.

### Testing Plan

**New tests** in `tests/test_tiers.py`:

```python
# Happy path: NODE_TIERS present
def test_load_tiers_parses_json(): ...
def test_load_tiers_returns_correct_capacity(): ...
def test_default_tier_by_name(): ...
def test_default_tier_fallback_to_first(): ...
def test_tier_by_name_found(): ...
def test_tier_by_name_not_found_returns_none(): ...
def test_tier_for_slug_found(): ...
def test_tier_for_slug_not_found_returns_none(): ...

# Fallback: NODE_TIERS absent
def test_load_tiers_fallback_uses_do_size(): ...
def test_load_tiers_fallback_capacity_from_default_capacity(): ...
def test_load_tiers_fallback_default_capacity_6_when_absent(): ...

# Error cases
def test_load_tiers_raises_on_invalid_json(): ...
def test_load_tiers_raises_on_missing_name_field(): ...
def test_load_tiers_raises_on_missing_capacity_field(): ...
def test_load_tiers_raises_on_non_integer_capacity(): ...
def test_load_tiers_raises_on_non_list(): ...

# node_capacity helper
def test_node_capacity_reads_label(): ...
def test_node_capacity_fallback_when_label_absent(): ...
def test_node_capacity_fallback_on_invalid_label_value(): ...
```

**Existing tests**: Run `uv run pytest` to confirm no regressions.

### Documentation Updates

Docstrings in `tiers.py` are the documentation. No other doc changes needed for
this ticket.
