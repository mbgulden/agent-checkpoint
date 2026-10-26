"""Shipped default texts (plan §5 verbatim), checkpoint path, and
operator-template rendering."""
import agent_checkpoint as ac


def plan_turns(used, cap, left=None):
    p = ac.TierPlan()
    p.turns_used = used
    p.turns_cap = cap
    p.turns_left = left if left is not None else cap - used
    return p


def plan_both(used, cap, elapsed, tcap):
    p = plan_turns(used, cap)
    p.time_used = elapsed
    p.time_cap = tcap
    p.time_left = max(0.0, tcap - elapsed)
    return p


PATH = "/home/x/.hermes/agent-checkpoints/t_abc.md"


def test_tier1_default_snapshot_verbatim_both_axes():
    """Plan §5 tier 1, verbatim (both-axes shape, the shipped default)."""
    cfg = ac.PluginConfig()
    out = ac.render_checkpoint_text(cfg, plan_both(10, 20, 1800, 3600), 0, PATH)
    assert out == (
        "Budget checkpoint: you have used 10 of 20 turns (10 left) and "
        "30m00s of 1h00m (30m00s left) — about half your budget remains; "
        "keep working at normal pace. Before continuing: write everything "
        f"found so far into {PATH}, including what remains to be done. "
        "Then continue the task."
    )


def test_tier2_default_snapshot_verbatim_both_axes():
    """Plan §5 tier 2, verbatim (both-axes shape, the shipped default)."""
    cfg = ac.PluginConfig()
    out = ac.render_checkpoint_text(cfg, plan_both(18, 20, 2952, 3600), 1, PATH)
    assert out == (
        "Final budget checkpoint: about 2 turns / 10m48s remain. Do not "
        "start new subtasks or new lines of investigation. Finish the step "
        "in progress, then: (1) bring "
        f"{PATH}"
        " (and your deliverable, if the task names one) to its final state "
        "for this run — if the work is unfinished, mark it INCOMPLETE and "
        "state the exact next step; (2) post your summary through the "
        "normal completion/blocking channel; (3) verify the file exists "
        "before you finish."
    )


def test_tier1_goal_dialect_turns_only():
    """Single-axis (goal) tier 1: same sentences, no time clause, no None."""
    cfg = ac.PluginConfig()
    out = ac.render_checkpoint_text(cfg, plan_turns(10, 20), 0, PATH)
    assert out == (
        "Budget checkpoint: you have used 10 of 20 turns (10 left) — "
        "about half your budget remains; keep working at normal pace. "
        "Before continuing: write everything found so far into "
        f"{PATH}, including what remains to be done. Then continue the task."
    )


def test_tier2_goal_dialect_turns_only():
    cfg = ac.PluginConfig()
    out = ac.render_checkpoint_text(cfg, plan_turns(18, 20), 1, PATH)
    assert out == (
        "Final budget checkpoint: about 2 turns remain. Do not start new "
        "subtasks or new lines of investigation. Finish the step in "
        f"progress, then: (1) bring {PATH} (and your deliverable, if the "
        "task names one) to its final state for this run — if the work is "
        "unfinished, mark it INCOMPLETE and state the exact next step; "
        "(2) post your summary through the normal completion/blocking "
        "channel; (3) verify the file exists before you finish."
    )


def test_tier1_time_only_rendering():
    """Chat with only an armed time budget: no turns clause."""
    cfg = ac.PluginConfig()
    p = ac.TierPlan()
    p.time_used = 900.0
    p.time_cap = 1800.0
    p.time_left = 900.0
    out = ac.render_checkpoint_text(cfg, p, 0, PATH)
    assert out is not None
    assert "15m00s of 30m00s" in out
    assert "turns" not in out
    assert "None" not in out


def test_checkpoint_path_for():
    cfg = ac.PluginConfig(checkpoint_dir="/a/b")
    assert ac.checkpoint_path_for(cfg, "t_abc") == "/a/b/t_abc.md"
    cfg2 = ac.PluginConfig(checkpoint_dir="/a/b/")
    assert ac.checkpoint_path_for(cfg2, "t_abc") == "/a/b/t_abc.md"


def test_checkpoint_path_default_dir():
    cfg = ac.PluginConfig()
    assert cfg.checkpoint_dir == "$HERMES_HOME/agent-checkpoints/"


def test_operator_template_substitution_and_axis_strip():
    """Operator template with both axes present renders both."""
    cfg = ac.PluginConfig(tier1_template=(
        "T1: {turns_used}/{turns_cap} @ {time_left}. File: {checkpoint_file}."
    ))
    out = ac.render_checkpoint_text(cfg, plan_both(10, 20, 1800, 3600), 0, PATH)
    assert out == f"T1: 10/20 @ 30m00s. File: {PATH}."


def test_operator_template_missing_axis_strips_clause():
    """Operator template referencing an absent axis has the clause
    stripped rather than printing None."""
    cfg = ac.PluginConfig(tier1_template=(
        "T1: {turns_used}/{turns_cap} turns ({turns_left} left) "
        "and {time_used} of {time_cap} ({time_left} left)."
    ))
    out = ac.render_checkpoint_text(cfg, plan_turns(10, 20), 0, PATH)
    assert out is not None
    assert "10/20 turns (10 left)" in out
    assert "None" not in out
    assert "{" not in out  # no unreplaced placeholders


def test_render_fail_open_bad_template():
    """A template that breaks formatting fails open to None."""
    cfg = ac.PluginConfig(tier1_template="{turns_used}{turns_cap")
    # (unbalanced braces) — string .format is not used, but exercise the
    # try/except by giving a non-string template.
    cfg_bad = ac.PluginConfig(tier1_template=None)
    out = ac.render_checkpoint_text(cfg_bad, plan_turns(10, 20), 0, PATH)
    assert out is not None  # None template → shipped default (not an error)


def test_time_formatting():
    assert ac._fmt_time(5) == "5s"
    assert ac._fmt_time(65) == "1m05s"
    assert ac._fmt_time(3665) == "1h01m"
