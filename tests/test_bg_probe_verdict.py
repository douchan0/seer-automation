"""The background-input verdict must be earned, not asserted.

An independent review found the first version reporting `BACKGROUND_INPUT_BLOCKED`
whenever *anything* went wrong — including "the client was on the server list".  That
word claims something specific about this client's PepperFlash canvas, and it may only
be used when a negative result is backed by a positive control at the same coordinates.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("bg_probe_tool", _ROOT / "tools" / "bg_probe.py")
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["bg_probe_tool"] = probe
_SPEC.loader.exec_module(probe)

GOOD = {"world_before": True, "spt_absent_before": True}


def _arm(**kw):
    base = dict(GOOD, spt_opened=False, left_dirty=False)
    base.update(kw)
    return base


def test_negative_without_a_control_is_inconclusive():
    verdict, _, _ = probe.verdict_for({"background": _arm()})
    assert verdict == "PROBE_INCONCLUSIVE"


def test_negative_with_a_working_control_is_blocked():
    verdict, _, basis = probe.verdict_for({
        "background": _arm(),
        "foreground": _arm(spt_opened=True, panel_closed=True),
    })
    assert verdict == "BACKGROUND_INPUT_BLOCKED"
    assert "PID" in basis


def test_a_working_background_lane_is_a_pass():
    verdict, _, _ = probe.verdict_for({"background": _arm(spt_opened=True)})
    assert verdict == "BACKGROUND_INPUT_LIVE_PASS"


def test_not_in_the_world_is_not_a_blocked_verdict():
    """Being on the server list says nothing about the input lane."""

    verdict, failed, _ = probe.verdict_for({
        "background": _arm(world_before=False),
        "foreground": _arm(spt_opened=True, panel_closed=True),
    })
    assert verdict == "PROBE_INCONCLUSIVE"
    assert failed


def test_a_failed_control_does_not_convict_the_lane():
    """If foreground cannot open the panel either, the coordinates are wrong."""

    verdict, _, basis = probe.verdict_for({
        "background": _arm(),
        "foreground": _arm(spt_opened=False),
    })
    assert verdict == "PROBE_INCONCLUSIVE"
    assert "coordinates" in basis


def test_a_dirty_screen_blocks_attribution():
    verdict, _, _ = probe.verdict_for({
        "background": _arm(left_dirty=True),
        "foreground": _arm(spt_opened=True, panel_closed=True),
    })
    assert verdict == "PROBE_INCONCLUSIVE"


def test_a_check_only_run_says_so():
    verdict, failed, _ = probe.verdict_for({"background": {"world_before": True}})
    assert verdict == "PROBE_INCONCLUSIVE"
    assert "check" in failed[0]


def test_frontmost_must_be_measured_before_it_can_be_unchanged():
    """Two failed `lsappinfo` reads are not 'the focus did not change'."""

    import inspect

    src = inspect.getsource(probe.run_arm)
    assert "front_measured" in src
    assert "cursor_measured" in src
