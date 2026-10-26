"""Pure tier math (plan sections 2-3): fraction math, tiny-cap collapse,
penultimate-turn rule, no-cap no-op, per-axis controls, floors."""
import pytest

import agent_checkpoint as ac


def caps(turns=None, tsec=None, dialect="kanban"):
    return ac.RunCaps(turns_cap=turns, time_cap=tsec, dialect=dialect)


# ---------------------------------------------------------------------------
# Fraction math (both axes, asymmetric tier 2)
# ---------------------------------------------------------------------------

def test_tier1_at_50pct_turn_axis():
    """Turns used = 50% of turn cap, time well below → tier 1 fires."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=10, elapsed=60.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == [0]
    assert plan.effective_fraction == pytest.approx(0.5)


def test_tier1_driven_by_time_axis():
    """Time used = 50% of time cap, turns barely used → tier 1 fires.
    Effective fraction = max(axis fractions) — the time axis drives it."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=1, elapsed=1800.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == [0]
    assert plan.effective_fraction == pytest.approx(0.5)


def test_tier2_asymmetric_time_axis_first():
    """Time 82% (>= 0.8) with turns 85% (< 0.9): tier 2 fires on the TIME
    axis alone — the asymmetric 0.8/0.9 thresholds (plan §2). Tier 1
    already fired (latched in)."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=17, elapsed=2952.0, fired_tiers=[0])  # 0.82 of 3600
    assert plan is not None
    assert plan.fire_tiers == [1]


def test_tier2_not_due_below_both_thresholds():
    """Time 79% (< 0.8) and turns 88% (< 0.9): neither axis crossed →
    no tier 2 (tier 1 already latched)."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=17.6 and 17, elapsed=2844.0, fired_tiers=[0])
    assert plan is not None
    assert 1 not in plan.fire_tiers


def test_tier2_on_turns_axis():
    """Turns 90% with time far below → tier 2 on the turns axis."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=18, elapsed=100.0, fired_tiers=[0])
    assert plan is not None
    assert plan.fire_tiers == [1]


def test_two_fraction_list_sets_tier2_both_axes():
    """tier_fractions=[0.5, 0.75]: the second element is the tier-2 fraction
    for BOTH axes (replaces the asymmetric 0.8/0.9)."""
    cfg = ac.PluginConfig(tier_fractions=[0.5, 0.75])
    plan = ac.compute_tier_plan(cfg, caps(turns=20, tsec=3600),
                                turns_used=15, elapsed=100.0,
                                fired_tiers=[0])
    assert plan is not None
    assert plan.fire_tiers == [1]  # turns 0.75 >= 0.75


def test_single_fraction_list_keeps_asymmetric_tier2():
    """One configured fraction (the default [0.5]): tier 2 stays the
    asymmetric finalize checkpoint (0.8 time / 0.9 turns)."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(tier_fractions=[0.5]), caps(turns=20, tsec=3600),
        turns_used=17, elapsed=2952.0, fired_tiers=[0])
    assert plan is not None
    assert plan.fire_tiers == [1]


# ---------------------------------------------------------------------------
# Latching (unit level)
# ---------------------------------------------------------------------------

def test_latch_once_per_tier():
    """A tier that already fired does not re-fire at higher usage."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=19, elapsed=3500.0, fired_tiers=[0, 1])
    assert plan is not None
    assert plan.fire_tiers == []


def test_tier1_suppressed_when_already_fired_tier2_first():
    """Pathological ordering: tier 2 latched but tier 1 not — tier 1 can
    still fire (independent latches)."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=20, tsec=3600),
        turns_used=10, elapsed=60.0, fired_tiers=[1])
    assert plan is not None
    assert plan.fire_tiers == [0]


# ---------------------------------------------------------------------------
# Tiny-cap collapse (plan §3)
# ---------------------------------------------------------------------------

def test_tiny_turn_cap_suppresses_tier1():
    """Turn cap 8 (< 10): tier 1 suppressed, tier 2 only."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=8),
        turns_used=4, elapsed=0.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == []  # tier1 suppressed, tier2 not yet due

    plan2 = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=8),
        turns_used=8, elapsed=0.0, fired_tiers=[])
    assert plan2 is not None
    assert plan2.fire_tiers == [1]  # only tier 2 (penultimate/last turn)


def test_tiny_turn_cap_tier1_with_opt_in():
    """allow_tiny_tier1=True: tier 1 fires even on a tiny cap."""
    cfg = ac.PluginConfig(allow_tiny_tier1=True)
    plan = ac.compute_tier_plan(cfg, caps(turns=8),
                                turns_used=4, elapsed=0.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == [0]


def test_tiny_time_cap_suppresses_tier1():
    """Time cap 3 min (< 5 min) with no turns cap: tier 1 suppressed."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(tsec=180.0),
        turns_used=0, elapsed=90.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == []


def test_normal_caps_do_not_collapse():
    """Turn cap 10 (NOT < 10) and time 1h: tier 1 fires normally."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=10, tsec=3600),
        turns_used=5, elapsed=100.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == [0]


# ---------------------------------------------------------------------------
# Penultimate-turn rule (plan §3)
# ---------------------------------------------------------------------------

def test_tier2_fires_at_penultimate_turn_even_if_below_threshold():
    """Turn cap 10, at turn 8 (remaining = 2): tier 2 fires by the
    penultimate-turn rule even though 8/10 = 0.8 < 0.9."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=10, tsec=7200),
        turns_used=8, elapsed=100.0, fired_tiers=[0])
    assert plan is not None
    assert 1 in plan.fire_tiers


def test_tier2_not_forced_before_penultimate():
    """Turn cap 10, at turn 7 (remaining = 3): no forced tier 2, and the
    fraction is below 0.9 → no tier 2 at all."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=10, tsec=7200),
        turns_used=7, elapsed=100.0, fired_tiers=[0])
    assert plan is not None
    assert 1 not in plan.fire_tiers


# ---------------------------------------------------------------------------
# No-cap no-op, floors, axis enable/disable
# ---------------------------------------------------------------------------

def test_no_caps_noop():
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(), turns_used=5, elapsed=100.0,
        fired_tiers=[])
    assert plan is None


def test_one_turn_run_no_checkpoint():
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps(turns=1), turns_used=0, elapsed=0.0,
        fired_tiers=[])
    assert plan is None


def test_min_budget_floor_ignores_axis():
    cfg = ac.PluginConfig(min_budget_floor_seconds=3600)
    plan = ac.compute_tier_plan(
        cfg, caps(turns=None, tsec=60.0),  # 60s < 1h floor
        turns_used=0, elapsed=30.0, fired_tiers=[])
    assert plan is None  # time axis ignored, no other axis → no-op

    cfg2 = ac.PluginConfig(min_budget_floor_turns=5)
    plan2 = ac.compute_tier_plan(
        cfg2, caps(turns=3), turns_used=2, elapsed=0.0, fired_tiers=[])
    assert plan2 is None


def test_per_axis_disable():
    cfg = ac.PluginConfig(enabled_axes=("turns",))
    plan = ac.compute_tier_plan(
        cfg, caps(turns=20, tsec=3600),
        turns_used=1, elapsed=1800.0, fired_tiers=[])
    # time axis disabled: 50% time usage alone would NOT trigger tier 1.
    assert plan is not None
    assert plan.fire_tiers == []
    assert plan.time_used is None  # not rendered either


def test_single_axis_plan_uses_that_axis_for_effective_fraction():
    cfg = ac.PluginConfig(enabled_axes=("time",))
    plan = ac.compute_tier_plan(
        cfg, caps(turns=2, tsec=3600),
        turns_used=1, elapsed=1800.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == [0]
    assert plan.turns_used is None
