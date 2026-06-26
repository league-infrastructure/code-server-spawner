"""Unit tests for cspawn.cs_docker.tiers — NODE_TIERS config helpers."""
import json
import pytest

from cspawn.cs_docker.tiers import (
    Tier,
    default_capacity,
    default_tier,
    load_tiers,
    node_capacity,
    tier_by_name,
    tier_for_slug,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NODE_TIERS_JSON = json.dumps([
    {"name": "small", "slug": "s-4vcpu-8gb-amd", "capacity": 6},
    {"name": "large", "slug": "s-8vcpu-16gb-amd", "capacity": 14},
])


def cfg_with_tiers(**overrides):
    """Return a plain dict (Config-compatible) with NODE_TIERS set."""
    base = {
        "NODE_TIERS": NODE_TIERS_JSON,
        "DEFAULT_TIER": "small",
        "DEFAULT_CAPACITY": "6",
        "DO_SIZE": "s-4vcpu-8gb-amd",
    }
    base.update(overrides)
    return base


def cfg_without_tiers(**overrides):
    """Return a plain dict without NODE_TIERS (backward-compat path)."""
    base = {
        "DO_SIZE": "s-4vcpu-8gb-amd",
        "DEFAULT_CAPACITY": "6",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path: NODE_TIERS present
# ---------------------------------------------------------------------------


def test_load_tiers_parses_json():
    cfg = cfg_with_tiers()
    tiers = load_tiers(cfg)
    assert len(tiers) == 2
    assert tiers[0].name == "small"
    assert tiers[1].name == "large"


def test_load_tiers_returns_correct_capacity():
    cfg = cfg_with_tiers()
    tiers = load_tiers(cfg)
    assert tiers[0].capacity == 6
    assert tiers[1].capacity == 14


def test_default_tier_by_name():
    cfg = cfg_with_tiers(DEFAULT_TIER="large")
    t = default_tier(cfg)
    assert t.name == "large"
    assert t.capacity == 14


def test_default_tier_fallback_to_first():
    # DEFAULT_TIER names a tier that doesn't exist → falls back to first
    cfg = cfg_with_tiers(DEFAULT_TIER="nonexistent")
    t = default_tier(cfg)
    assert t.name == "small"


def test_tier_by_name_found():
    cfg = cfg_with_tiers()
    t = tier_by_name(cfg, "large")
    assert t is not None
    assert t.slug == "s-8vcpu-16gb-amd"


def test_tier_by_name_not_found_returns_none():
    cfg = cfg_with_tiers()
    assert tier_by_name(cfg, "xlarge") is None


def test_tier_for_slug_found():
    cfg = cfg_with_tiers()
    t = tier_for_slug(cfg, "s-8vcpu-16gb-amd")
    assert t is not None
    assert t.name == "large"


def test_tier_for_slug_not_found_returns_none():
    cfg = cfg_with_tiers()
    assert tier_for_slug(cfg, "s-unknown-slug") is None


# ---------------------------------------------------------------------------
# Fallback: NODE_TIERS absent
# ---------------------------------------------------------------------------


def test_load_tiers_fallback_uses_do_size():
    cfg = cfg_without_tiers(DO_SIZE="s-4vcpu-8gb-amd")
    tiers = load_tiers(cfg)
    assert len(tiers) == 1
    assert tiers[0].name == "default"
    assert tiers[0].slug == "s-4vcpu-8gb-amd"


def test_load_tiers_fallback_capacity_from_default_capacity():
    cfg = cfg_without_tiers(DEFAULT_CAPACITY="10")
    tiers = load_tiers(cfg)
    assert tiers[0].capacity == 10


def test_load_tiers_fallback_default_capacity_6_when_absent():
    cfg = {"DO_SIZE": "s-4vcpu-8gb-amd"}  # No DEFAULT_CAPACITY key
    tiers = load_tiers(cfg)
    assert tiers[0].capacity == 6


def test_load_tiers_fallback_empty_node_tiers():
    """NODE_TIERS present but empty string → fallback path."""
    cfg = {"NODE_TIERS": "", "DO_SIZE": "s-2vcpu-4gb", "DEFAULT_CAPACITY": "4"}
    tiers = load_tiers(cfg)
    assert len(tiers) == 1
    assert tiers[0].slug == "s-2vcpu-4gb"
    assert tiers[0].capacity == 4


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_load_tiers_raises_on_invalid_json():
    cfg = {"NODE_TIERS": "not-json", "DO_SIZE": "s-4vcpu-8gb-amd"}
    with pytest.raises(ValueError, match="not valid JSON"):
        load_tiers(cfg)


def test_load_tiers_raises_on_missing_name_field():
    bad = json.dumps([{"slug": "s-4vcpu-8gb-amd", "capacity": 6}])
    cfg = {"NODE_TIERS": bad}
    with pytest.raises(ValueError, match="missing required field 'name'"):
        load_tiers(cfg)


def test_load_tiers_raises_on_missing_capacity_field():
    bad = json.dumps([{"name": "small", "slug": "s-4vcpu-8gb-amd"}])
    cfg = {"NODE_TIERS": bad}
    with pytest.raises(ValueError, match="missing required field 'capacity'"):
        load_tiers(cfg)


def test_load_tiers_raises_on_non_integer_capacity():
    bad = json.dumps([{"name": "small", "slug": "s-4vcpu-8gb-amd", "capacity": "big"}])
    cfg = {"NODE_TIERS": bad}
    with pytest.raises(ValueError, match="capacity must be an integer"):
        load_tiers(cfg)


def test_load_tiers_raises_on_non_list():
    bad = json.dumps({"name": "small", "slug": "s-4vcpu-8gb-amd", "capacity": 6})
    cfg = {"NODE_TIERS": bad}
    with pytest.raises(ValueError, match="non-empty JSON array"):
        load_tiers(cfg)


def test_load_tiers_raises_on_empty_list():
    cfg = {"NODE_TIERS": "[]"}
    with pytest.raises(ValueError, match="non-empty JSON array"):
        load_tiers(cfg)


# ---------------------------------------------------------------------------
# node_capacity helper
# ---------------------------------------------------------------------------


def test_node_capacity_reads_label():
    node_attrs = {"Spec": {"Labels": {"cs.capacity": "12"}}}
    cfg = cfg_with_tiers()
    assert node_capacity(node_attrs, cfg) == 12


def test_node_capacity_fallback_when_label_absent():
    node_attrs = {"Spec": {"Labels": {}}}
    cfg = cfg_with_tiers(DEFAULT_CAPACITY="8")
    assert node_capacity(node_attrs, cfg) == 8


def test_node_capacity_fallback_on_invalid_label_value():
    node_attrs = {"Spec": {"Labels": {"cs.capacity": "not-a-number"}}}
    cfg = cfg_with_tiers(DEFAULT_CAPACITY="6")
    assert node_capacity(node_attrs, cfg) == 6


def test_node_capacity_fallback_no_spec():
    """Node attrs with no Spec key at all → default capacity."""
    node_attrs = {}
    cfg = cfg_without_tiers(DEFAULT_CAPACITY="5")
    assert node_capacity(node_attrs, cfg) == 5


def test_node_capacity_fallback_no_labels():
    """Node attrs with Spec but no Labels key → default capacity."""
    node_attrs = {"Spec": {}}
    cfg = cfg_without_tiers(DEFAULT_CAPACITY="7")
    assert node_capacity(node_attrs, cfg) == 7


# ---------------------------------------------------------------------------
# default_capacity
# ---------------------------------------------------------------------------


def test_default_capacity_from_config():
    assert default_capacity({"DEFAULT_CAPACITY": "10"}) == 10


def test_default_capacity_fallback_when_absent():
    assert default_capacity({}) == 6


def test_default_capacity_fallback_on_invalid():
    assert default_capacity({"DEFAULT_CAPACITY": "not-a-number"}) == 6


# ---------------------------------------------------------------------------
# Tier dataclass
# ---------------------------------------------------------------------------


def test_tier_is_frozen():
    t = Tier(name="small", slug="s-4vcpu-8gb-amd", capacity=6)
    with pytest.raises((AttributeError, TypeError)):
        t.name = "large"  # type: ignore[misc]


def test_tier_equality():
    t1 = Tier(name="small", slug="s-4vcpu-8gb-amd", capacity=6)
    t2 = Tier(name="small", slug="s-4vcpu-8gb-amd", capacity=6)
    assert t1 == t2


def test_default_tier_absent_default_tier_key():
    """DEFAULT_TIER absent → returns tiers[0]."""
    cfg = {"NODE_TIERS": NODE_TIERS_JSON}
    t = default_tier(cfg)
    assert t.name == "small"
