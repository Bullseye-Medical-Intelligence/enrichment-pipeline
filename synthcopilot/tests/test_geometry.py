"""Tests for the geometry engine — curve generation and modifiers."""

import math

import pytest

from synthcopilot.geometry import generate_rail, catmull_rom_chain, _smoothstep_envelope


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


class TestSmoothstepEnvelope:
    def test_zero_at_boundaries(self):
        assert _smoothstep_envelope(0.0, 0.15) == 0.0
        assert _smoothstep_envelope(1.0, 0.15) == 0.0

    def test_one_in_middle(self):
        assert _smoothstep_envelope(0.5, 0.15) == 1.0
        assert _smoothstep_envelope(0.3, 0.15) == 1.0

    def test_smooth_ramp_in_fade_zone(self):
        fade = 0.2
        v1 = _smoothstep_envelope(0.05, fade)
        v2 = _smoothstep_envelope(0.10, fade)
        v3 = _smoothstep_envelope(0.15, fade)
        assert 0.0 < v1 < v2 < v3 < 1.0

    def test_symmetric(self):
        fade = 0.15
        assert _smoothstep_envelope(0.1, fade) == pytest.approx(
            _smoothstep_envelope(0.9, fade)
        )

    def test_wider_fade_zone_slower_ramp(self):
        narrow = _smoothstep_envelope(0.1, 0.10)
        wide = _smoothstep_envelope(0.1, 0.25)
        assert wide < narrow


class TestVelocityClamping:
    def test_velocity_clamp_reduces_mean_speed(self):
        nodes_unclamped = generate_rail(
            (0, 0, 0), (3, 3, 4), num_nodes=30,
            rail_type="zigzag", complexity=8, max_velocity=0,
        )
        nodes_clamped = generate_rail(
            (0, 0, 0), (3, 3, 4), num_nodes=30,
            rail_type="zigzag", complexity=8, max_velocity=2.0,
        )
        def mean_speed(nodes):
            speeds = []
            for i in range(1, len(nodes)):
                dt = nodes[i].time - nodes[i-1].time
                if dt > 0:
                    dx = nodes[i].x - nodes[i-1].x
                    dy = nodes[i].y - nodes[i-1].y
                    speeds.append(math.hypot(dx, dy) / dt)
            return sum(speeds) / len(speeds) if speeds else 0
        assert mean_speed(nodes_clamped) < mean_speed(nodes_unclamped)

    def test_velocity_clamp_preserves_anchors(self):
        nodes = generate_rail(
            (-1, 0, 0), (1, 2, 4), num_nodes=20,
            rail_type="wave", complexity=5, max_velocity=1.0,
        )
        assert nodes[0].x == pytest.approx(-1.0)
        assert nodes[0].y == pytest.approx(0.0)
        assert nodes[-1].x == pytest.approx(1.0)
        assert nodes[-1].y == pytest.approx(2.0)


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
