"""Tests for the geometry engine — curve generation and modifiers."""

import math

import pytest

from synthcopilot.geometry import generate_rail, catmull_rom_chain


class TestGenerateRail:
    def test_start_and_end_preserved(self):
        nodes = generate_rail((0.0, 0.0, 0.0), (2.0, 3.0, 10.0), num_nodes=20)
        assert nodes[0].x == pytest.approx(0.0)
        assert nodes[0].y == pytest.approx(0.0)
        assert nodes[0].time == pytest.approx(0.0)
        assert nodes[-1].x == pytest.approx(2.0)
        assert nodes[-1].y == pytest.approx(3.0)
        assert nodes[-1].time == pytest.approx(10.0)

    def test_correct_node_count(self):
        for n in [2, 5, 16, 50]:
            nodes = generate_rail((0, 0, 0), (1, 1, 4), num_nodes=n)
            assert len(nodes) == n

    def test_minimum_two_nodes(self):
        nodes = generate_rail((0, 0, 0), (1, 1, 4), num_nodes=1)
        assert len(nodes) == 2

    def test_times_are_monotonic(self):
        nodes = generate_rail((0, 0, 0), (1, 1, 8), num_nodes=16)
        for i in range(1, len(nodes)):
            assert nodes[i].time > nodes[i - 1].time

    def test_smooth_generates_within_bounds(self):
        nodes = generate_rail((-1, 0, 0), (1, 2, 8), num_nodes=20, rail_type="smooth")
        for n in nodes:
            assert -3.0 <= n.x <= 3.0
            assert -1.0 <= n.y <= 4.0


class TestModifiers:
    def test_wave_deviates_from_smooth(self):
        smooth = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="smooth")
        wave = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="wave", complexity=3)
        diffs = sum(abs(w.y - s.y) for w, s in zip(wave, smooth))
        assert diffs > 0.1

    def test_spiral_deviates_from_smooth(self):
        smooth = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="smooth")
        spiral = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="spiral", complexity=3)
        diffs = sum(abs(sp.x - sm.x) + abs(sp.y - sm.y) for sp, sm in zip(spiral, smooth))
        assert diffs > 0.1

    def test_zigzag_deviates_from_smooth(self):
        smooth = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="smooth")
        zigzag = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="zigzag", complexity=3)
        diffs = sum(abs(z.y - s.y) for z, s in zip(zigzag, smooth))
        assert diffs > 0.1

    def test_complexity_zero_no_modification(self):
        smooth = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="smooth")
        wave0 = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=16, rail_type="wave", complexity=0)
        for a, b in zip(smooth, wave0):
            assert a.x == pytest.approx(b.x, abs=1e-10)
            assert a.y == pytest.approx(b.y, abs=1e-10)

    def test_higher_complexity_bigger_deviation(self):
        low = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=20, rail_type="wave", complexity=1)
        high = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=20, rail_type="wave", complexity=5)
        smooth = generate_rail((0, 1, 0), (2, 1, 8), num_nodes=20, rail_type="smooth")
        dev_low = sum(abs(l.y - s.y) for l, s in zip(low, smooth))
        dev_high = sum(abs(h.y - s.y) for h, s in zip(high, smooth))
        assert dev_high > dev_low

    def test_anchors_preserved_with_modifiers(self):
        for rtype in ["wave", "spiral", "zigzag"]:
            nodes = generate_rail((-1, 0.5, 0), (1, 2.5, 8), num_nodes=20,
                                   rail_type=rtype, complexity=5)
            assert nodes[0].x == pytest.approx(-1.0)
            assert nodes[0].y == pytest.approx(0.5)
            assert nodes[-1].x == pytest.approx(1.0)
            assert nodes[-1].y == pytest.approx(2.5)


class TestCatmullRomChain:
    def test_chain_through_three_anchors(self):
        anchors = [(0, 0, 0), (1, 2, 4), (2, 0, 8)]
        nodes = catmull_rom_chain(anchors, nodes_per_segment=8)
        assert nodes[0].x == pytest.approx(0.0)
        assert nodes[-1].x == pytest.approx(2.0)
        assert len(nodes) == 8 + 7  # first segment 8, second 7 (skip duplicate)

    def test_chain_requires_two_anchors(self):
        with pytest.raises(ValueError):
            catmull_rom_chain([(0, 0, 0)])
