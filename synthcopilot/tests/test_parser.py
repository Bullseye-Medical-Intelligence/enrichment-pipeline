"""Tests for the parser module — round-trip fidelity and edge cases."""

import json
import os
import tempfile
import zipfile

import pytest

from synthcopilot.parser import load, save, cleanup, inspect
from synthcopilot.models import DIFFICULTIES


def _make_synth(track_json: dict, audio_bytes: bytes = b"fake_ogg_data", audio_name: str = "song.ogg") -> str:
    """Create a temporary .synth file for testing."""
    path = tempfile.mktemp(suffix=".synth")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("track.json", json.dumps(track_json))
        zf.writestr(audio_name, audio_bytes)
    return path


MINIMAL_TRACK = {
    "Name": "Test Song",
    "Author": "TestMapper",
    "BPM": 140.0,
    "Offset": 0.5,
    "BeatPerBar": 4,
    "BeatDivisions": 2,
    "Notes_Expert": [
        {"time": 4.0, "Position": [0.5, 1.2], "Type": 0},
        {"time": 5.0, "Position": [-0.3, 1.8], "Type": 1},
    ],
    "Slides_Expert": [
        {
            "Type": 0,
            "notes": [
                {"time": 8.0, "Position": [0.0, 1.0]},
                {"time": 9.0, "Position": [0.5, 1.5]},
                {"time": 10.0, "Position": [1.0, 1.0]},
            ],
        }
    ],
    "Crouches_Expert": [
        {"time": 12.0, "Type": 0},
    ],
    "Notes_Easy": [],
    "Slides_Easy": [],
    "Crouches_Easy": [],
}


class TestLoad:
    def test_load_parses_metadata(self):
        path = _make_synth(MINIMAL_TRACK)
        try:
            track, work_dir = load(path)
            assert track.name == "Test Song"
            assert track.author == "TestMapper"
            assert track.bpm == 140.0
            assert track.offset == 0.5
            assert track.audio_filename == "song.ogg"
            cleanup(work_dir)
        finally:
            os.unlink(path)

    def test_load_parses_notes(self):
        path = _make_synth(MINIMAL_TRACK)
        try:
            track, work_dir = load(path)
            expert = track.difficulties["Expert"]
            assert len(expert.notes) == 2
            assert expert.notes[0].time == 4.0
            assert expert.notes[0].x == 0.5
            assert expert.notes[0].y == 1.2
            assert expert.notes[0].hand_type == 0
            assert expert.notes[1].hand_type == 1
            cleanup(work_dir)
        finally:
            os.unlink(path)

    def test_load_parses_rails(self):
        path = _make_synth(MINIMAL_TRACK)
        try:
            track, work_dir = load(path)
            expert = track.difficulties["Expert"]
            assert len(expert.rails) == 1
            rail = expert.rails[0]
            assert rail.hand_type == 0
            assert len(rail.nodes) == 3
            assert rail.nodes[1].x == 0.5
            cleanup(work_dir)
        finally:
            os.unlink(path)

    def test_load_parses_walls(self):
        path = _make_synth(MINIMAL_TRACK)
        try:
            track, work_dir = load(path)
            expert = track.difficulties["Expert"]
            assert len(expert.walls) == 1
            assert expert.walls[0].time == 12.0
            cleanup(work_dir)
        finally:
            os.unlink(path)

    def test_load_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load("/nonexistent/path.synth")

    def test_load_invalid_zip_raises(self):
        path = tempfile.mktemp(suffix=".synth")
        with open(path, "w") as f:
            f.write("not a zip")
        try:
            with pytest.raises(ValueError, match="Not a valid ZIP"):
                load(path)
        finally:
            os.unlink(path)

    def test_load_missing_track_json_raises(self):
        path = tempfile.mktemp(suffix=".synth")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("other.txt", "hello")
        try:
            with pytest.raises(FileNotFoundError, match="track.json not found"):
                load(path)
        finally:
            os.unlink(path)


class TestRoundTrip:
    def test_save_and_reload_preserves_data(self):
        path = _make_synth(MINIMAL_TRACK)
        try:
            track, work_dir = load(path)
            output = tempfile.mktemp(suffix=".synth")
            save(track, work_dir, output)
            cleanup(work_dir)

            track2, work_dir2 = load(output)
            assert track2.name == "Test Song"
            assert track2.bpm == 140.0
            expert = track2.difficulties["Expert"]
            assert len(expert.notes) == 2
            assert len(expert.rails) == 1
            assert len(expert.rails[0].nodes) == 3
            cleanup(work_dir2)
            os.unlink(output)
        finally:
            os.unlink(path)

    def test_preserves_unknown_fields(self):
        track_json = dict(MINIMAL_TRACK)
        track_json["CustomField"] = "preserved"
        track_json["EditorVersion"] = "2.0"
        path = _make_synth(track_json)
        try:
            track, work_dir = load(path)
            output = tempfile.mktemp(suffix=".synth")
            save(track, work_dir, output)
            cleanup(work_dir)

            with zipfile.ZipFile(output, "r") as zf:
                raw = json.loads(zf.read("track.json"))
            assert raw["CustomField"] == "preserved"
            assert raw["EditorVersion"] == "2.0"
            os.unlink(output)
        finally:
            os.unlink(path)


class TestTimeConversion:
    def test_seconds_to_beats(self):
        path = _make_synth(MINIMAL_TRACK)
        track, work_dir = load(path)
        # BPM=140, offset=0.5
        # At t=0.5s (offset), beat = 0
        assert track.seconds_to_beats(0.5) == pytest.approx(0.0)
        # At t=1.5s, elapsed = 1.0s, beats = 1.0 * 140/60 = 2.333...
        assert track.seconds_to_beats(1.5) == pytest.approx(140.0 / 60.0)
        cleanup(work_dir)
        os.unlink(path)

    def test_beats_to_seconds_inverse(self):
        path = _make_synth(MINIMAL_TRACK)
        track, work_dir = load(path)
        for sec in [0.5, 1.0, 5.0, 72.0]:
            beats = track.seconds_to_beats(sec)
            assert track.beats_to_seconds(beats) == pytest.approx(sec)
        cleanup(work_dir)
        os.unlink(path)


class TestInspect:
    def test_inspect_returns_summary(self):
        path = _make_synth(MINIMAL_TRACK)
        try:
            summary = inspect(path)
            assert summary["name"] == "Test Song"
            assert summary["bpm"] == 140.0
            assert "Expert" in summary["difficulties"]
            assert summary["difficulties"]["Expert"]["notes"] == 2
            assert summary["difficulties"]["Expert"]["rails"] == 1
        finally:
            os.unlink(path)
