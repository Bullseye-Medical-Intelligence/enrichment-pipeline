"""Tests for the rhythm engine — cooldown, velocity gate, note snapping."""

import math

import pytest

from synthcopilot.rhythm import apply_cooldown, snap_notes_to_rail
from synthcopilot.models import RailNode, Note


class TestCooldown:
    def test_empty_input(self):
        assert apply_cooldown([]) == []

    def test_single_onset(self):
        assert apply_cooldown([1.0]) == [1.0]

    def test_removes_clustered_onsets(self):
        onsets = [1.000, 1.020, 1.040, 1.060, 1.100, 1.200]
        filtered = apply_cooldown(onsets, min_gap=0.050)
        assert filtered == [1.000, 1.060, 1.200]

    def test_all_spaced_out_keeps_all(self):
        onsets = [0.0, 0.1, 0.2, 0.3]
        filtered = apply_cooldown(onsets, min_gap=0.05)
        assert len(filtered) == 4

    def test_exact_gap_boundary_included(self):
        onsets = [1.0, 1.05, 1.10]
        filtered = apply_cooldown(onsets, min_gap=0.05)
        assert filtered == [1.0, 1.05, 1.10]

    def test_dense_dubstep_cluster(self):
        onsets = [t * 0.01 for t in range(20)]
        filtered = apply_cooldown(onsets, min_gap=0.050)
        for i in range(1, len(filtered)):
            assert filtered[i] - filtered[i-1] >= 0.050


class TestVelocityGate:
    def _straight_rail(self, x0, y0, x1, y1, n=10):
        return [
            RailNode(time=float(i), x=x0 + (x1-x0)*i/(n-1), y=y0 + (y1-y0)*i/(n-1))
            for i in range(n)
        ]

    def test_velocity_gate_drops_fast_notes(self):
        rail = self._straight_rail(0, 0, 10, 10, n=20)
        onsets = [0.0, 0.01, 5.0, 10.0]
        notes = snap_notes_to_rail(
            onsets, rail, 0.0, 10.0, bpm=120, offset=0.0,
            hand_type=0, min_gap=0.0, max_hand_speed=0.5,
        )
        assert len(notes) < len(onsets)

    def test_velocity_gate_disabled(self):
        rail = self._straight_rail(0, 0, 10, 10, n=20)
        onsets = [0.0, 0.01, 0.02]
        notes = snap_notes_to_rail(
            onsets, rail, 0.0, 10.0, bpm=120, offset=0.0,
            hand_type=0, min_gap=0.0, max_hand_speed=0,
        )
        assert len(notes) == 3

    def test_cooldown_and_velocity_combined(self):
        rail = self._straight_rail(0, 0, 5, 5, n=50)
        onsets = [0.0, 0.02, 0.04, 0.06, 1.0, 1.02, 2.0, 3.0]
        notes = snap_notes_to_rail(
            onsets, rail, 0.0, 5.0, bpm=120, offset=0.0,
            hand_type=0, min_gap=0.050, max_hand_speed=6.0,
        )
        assert len(notes) <= len(onsets)
        assert len(notes) >= 1


class TestSnapToRail:
    def test_empty_inputs(self):
        assert snap_notes_to_rail([], [], 0, 1, 120) == []
        rail = [RailNode(time=0, x=0, y=0)]
        assert snap_notes_to_rail([], rail, 0, 1, 120) == []

    def test_notes_follow_rail_path(self):
        rail = [
            RailNode(time=0.0, x=0.0, y=0.0),
            RailNode(time=4.0, x=2.0, y=2.0),
        ]
        onsets = [0.0, 2.5, 5.0]
        notes = snap_notes_to_rail(
            onsets, rail, 0.0, 5.0, bpm=120, offset=0.0,
            min_gap=0.0, max_hand_speed=0,
        )
        assert len(notes) == 3
        assert notes[0].x == pytest.approx(0.0)
        assert notes[1].x == pytest.approx(1.0, abs=0.01)
        assert notes[2].x == pytest.approx(2.0)
