"""Coordinate-math tests: the Retina/window-offset bugs live here.

Everything in this file is pure arithmetic - no game, no capture backend.
"""

from __future__ import annotations

import pytest

from seerauto import geometry as G


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def test_rect_basics():
    r = G.Rect(10, 20, 100, 50)
    assert (r.x2, r.y2) == (110, 70)
    assert r.center == G.Point(60, 45)
    assert r.size == G.Size(100, 50)
    assert r.as_int_tuple() == (10, 20, 100, 50)
    assert not r.is_empty


def test_rect_inset_clamp_contains():
    r = G.Rect(0, 0, 100, 100)
    inner = r.inset(10)
    assert inner.as_tuple() == (10.0, 10.0, 80.0, 80.0)
    assert r.clamp_to(G.Rect(90, 90, 100, 100)).as_tuple() == (90.0, 90.0, 10.0, 10.0)
    assert r.clamp_to(G.Rect(200, 200, 10, 10)).is_empty
    assert r.contains(G.Point(0, 0))
    assert not r.contains(G.Point(100, 50))  # half-open
    assert r.contains_rect(G.Rect(10, 10, 10, 10))
    assert not r.contains_rect(G.Rect(-1, 10, 10, 10))


def test_rect_iou():
    a = G.Rect(0, 0, 10, 10)
    assert a.iou(a) == pytest.approx(1.0)
    assert a.iou(G.Rect(5, 0, 10, 10)) == pytest.approx(50 / 150)
    assert a.iou(G.Rect(100, 100, 10, 10)) == 0.0


def test_rect_from_norm():
    outer = G.Rect(100, 200, 400, 300)
    r = G.Rect.from_norm((0.5, 0.5, 1.0, 1.0), into=outer)
    assert r.as_tuple() == (300.0, 350.0, 200.0, 150.0)


def test_merge_rects():
    assert G.merge_rects([]) is None
    assert G.merge_rects([G.Rect(0, 0, 0, 0)]) is None
    m = G.merge_rects([G.Rect(10, 10, 10, 10), G.Rect(50, 5, 10, 10)])
    assert m is not None and m.as_tuple() == (10.0, 5.0, 50.0, 15.0)


# --------------------------------------------------------------------------- #
# point <-> pixel (Retina)
# --------------------------------------------------------------------------- #
def test_retina_conversions():
    assert G.logical_to_pixel(1149) == 2298
    assert G.pixel_to_logical(2298) == 1149
    assert G.rect_logical_to_pixel(G.Rect(1, 2, 3, 4)).as_tuple() == (2, 4, 6, 8)
    assert G.rect_pixel_to_logical(G.Rect(2, 4, 6, 8)).as_tuple() == (1, 2, 3, 4)
    # round trip
    for value in (0, 1, 37.5, 1149):
        assert G.pixel_to_logical(G.logical_to_pixel(value)) == pytest.approx(value)


# --------------------------------------------------------------------------- #
# window geometry
# --------------------------------------------------------------------------- #
def test_window_pixel_size_and_rect():
    geo = G.WindowGeometry(origin_pt=G.Point(300, 120))
    assert geo.pixel_size == G.Size(2298, 1428)
    assert geo.window_rect_pt.as_tuple() == (300.0, 120.0, 1149.0, 714.0)


def test_game_px_to_global_pt():
    geo = G.WindowGeometry(origin_pt=G.Point(300, 120))
    # documented skill button, game.png pixel space
    p = geo.global_pt_from_game_px(1151, 1281)
    assert p.x == pytest.approx(300 + 1151 / 2)
    assert p.y == pytest.approx(120 + 1281 / 2)
    # and back
    back = geo.game_px_from_global_pt(p)
    assert back.x == pytest.approx(1151)
    assert back.y == pytest.approx(1281)


def test_global_rect_for_screencapture():
    geo = G.WindowGeometry(origin_pt=G.Point(300.4, 120.6))
    r = geo.global_rect_from_game_px(G.Rect(0, 0, 2298, 1428))
    assert r.as_tuple() == pytest.approx((300.4, 120.6, 1149.0, 714.0))


def test_geometry_close_tolerance():
    a = G.WindowGeometry(origin_pt=G.Point(300, 120))
    b = G.WindowGeometry(origin_pt=G.Point(301, 121))
    c = G.WindowGeometry(origin_pt=G.Point(320, 120))
    assert a.is_close(b, tol_pt=2)
    assert not a.is_close(c, tol_pt=2)


# --------------------------------------------------------------------------- #
# reference space + stage mapping
# --------------------------------------------------------------------------- #
def test_reference_layout_matches_measured_screenshot():
    """The reference constants must reproduce the measured game.png layout."""
    ref = G.DEFAULT_REFERENCE
    assert ref.capture_px == G.Size(2298, 1428)
    # stage measured on artifacts/game.png: x 102..2193, y 128..1347
    assert ref.stage_px.as_tuple() == (102.0, 128.0, 2092.0, 1220.0)
    assert ref.stage_px.x2 == 2194
    assert ref.stage_px.y2 == 1348
    # window pixel size == capture size (all fixtures are window-sized)
    assert ref.geometry().pixel_size == ref.capture_px


def test_stage_map_identity_for_reference_frame():
    smap = G.DEFAULT_REFERENCE.geometry().stage_map()
    assert smap.stage_px.as_tuple() == pytest.approx((102, 128, 2092, 1220))
    assert smap.ref_offset == G.Point(102, 128)
    assert smap.ref_size == G.Size(2092, 1220)
    assert smap.scale_x == pytest.approx(1.0)
    assert smap.scale_y == pytest.approx(1.0)
    # canonical stage (0,0) is the top-left of the playfield, which is at
    # (102,128) in a reference window capture
    assert smap.frame_from_canon_rect(G.Rect(0, 0, 10, 10)).as_tuple() == (102.0, 128.0, 10.0, 10.0)
    # game_px -> frame is the identity for a reference-sized frame
    game = G.Rect(200, 300, 100, 50)
    assert smap.frame_rect_from_game_px(game).as_tuple() == pytest.approx(game.as_tuple())


def test_stage_map_roundtrip():
    smap = G.DEFAULT_REFERENCE.geometry().stage_map(frame_size_px=G.Size(1150, 715))
    ref_rect = G.Rect(500, 400, 120, 80)
    frame_rect = smap.frame_from_canon_rect(ref_rect)
    back = smap.canon_from_frame_rect(frame_rect)
    assert back.as_tuple() == pytest.approx(ref_rect.as_tuple())
    p = G.Point(700, 600)
    assert smap.canon_from_frame_point(smap.frame_from_canon_point(p)).as_tuple() == pytest.approx(p.as_tuple())


def test_stage_map_scales_with_frame_size():
    ref = G.DEFAULT_REFERENCE
    for size in (G.Size(2298, 1428), G.Size(1150, 715), G.Size(600, 373), G.Size(998, 620)):
        smap = ref.geometry().stage_map(frame_size_px=size)
        # stage occupies the same *fraction* of every frame except for the tiny
        # rounding differences between fixture aspect ratios.
        frac_x = smap.stage_px.x / size.w
        frac_w = smap.stage_px.w / size.w
        assert frac_x == pytest.approx(ref.stage_px.x / ref.capture_px.w, abs=1e-9)
        assert frac_w == pytest.approx(ref.stage_px.w / ref.capture_px.w, abs=1e-9)
        # canonical stage center maps inside the frame
        center = smap.frame_from_canon_point(G.Point(*ref.stage_px.size.as_tuple()).scaled(0.5))
        assert 0 <= center.x <= size.w and 0 <= center.y <= size.h


def test_stage_map_normalised_helpers():
    smap = G.DEFAULT_REFERENCE.geometry().stage_map()
    r = smap.canon_rect_from_norm((0.0, 0.0, 0.5, 0.5))
    assert r.as_tuple() == pytest.approx((0.0, 0.0, 1046.0, 610.0))
    fr = smap.frame_rect_from_norm((0.0, 0.0, 0.5, 0.5))
    assert fr.as_tuple() == pytest.approx((102.0, 128.0, 1046.0, 610.0))


def test_game_px_stage_roundtrip():
    ref = G.DEFAULT_REFERENCE
    skill = G.Rect(1151, 1281, 10, 10)  # docs/state.md, game_px space
    stage = G.game_px_rect_to_stage_rect(skill)
    assert stage.as_tuple() == (1049.0, 1153.0, 10.0, 10.0)
    assert G.stage_rect_to_game_px_rect(stage).as_tuple() == skill.as_tuple()
    p = G.Point(1151, 1281)
    assert G.stage_point_to_game_px_point(G.game_px_point_to_stage_point(p)).as_tuple() == p.as_tuple()


def test_stage_map_translate_tracks_frame_crop():
    """Cropping a frame must shift the stage map by exactly the crop offset."""
    smap = G.DEFAULT_REFERENCE.geometry().stage_map()
    shifted = smap.translate(-10, -20)
    r = G.Rect(300, 300, 40, 40)
    a = smap.frame_from_canon_rect(r)
    b = shifted.frame_from_canon_rect(r)
    assert (a.x - b.x, a.y - b.y) == pytest.approx((10, 20))


def test_stage_map_handles_retina_scale_other_than_two():
    ref = G.DEFAULT_REFERENCE
    geo = G.WindowGeometry(size_pt=G.Size(1149, 714), scale=1.0)
    smap = geo.stage_map()
    assert smap.stage_px.w == pytest.approx(1046.0)
    assert smap.stage_px.h == pytest.approx(610.0)


def test_clamp():
    assert G.clamp(-5, 0, 10) == 0
    assert G.clamp(5, 0, 10) == 5
    assert G.clamp(50, 0, 10) == 10
