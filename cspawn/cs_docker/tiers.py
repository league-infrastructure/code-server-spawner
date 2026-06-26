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
