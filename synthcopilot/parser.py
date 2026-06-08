"""Parser module: handles .synth file I/O and track.json round-tripping.

A .synth file is a ZIP archive containing:
  - track.json (note, rail, wall placement data)
  - An audio file (typically .ogg or .wav)
  - Optional cover art or metadata files

The parser extracts to a temp directory, deserializes track.json into
TrackData, and can repackage a modified TrackData back into a valid .synth.
Unknown JSON fields are preserved for lossless round-tripping.
"""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from synthcopilot.models import (
    DIFFICULTIES,
    Difficulty,
    Note,
    Rail,
    TrackData,
    Wall,
)

AUDIO_EXTENSIONS = {".ogg", ".wav", ".mp3", ".flac", ".egg"}


def load(synth_path: str) -> tuple[TrackData, str]:
    """Unzip a .synth file and parse track.json.

    Returns (TrackData, work_dir) where work_dir is a temporary directory
    containing the extracted archive contents. The caller is responsible
    for cleanup (or passing work_dir to save() then calling cleanup()).
    """
    synth_path = Path(synth_path)
    if not synth_path.exists():
        raise FileNotFoundError(f"File not found: {synth_path}")
    if not zipfile.is_zipfile(synth_path):
        raise ValueError(f"Not a valid ZIP/.synth archive: {synth_path}")

    work_dir = tempfile.mkdtemp(prefix="synthcopilot_")
    with zipfile.ZipFile(synth_path, "r") as zf:
        zf.extractall(work_dir)

    track_json_path = _find_track_json(work_dir)
    if track_json_path is None:
        shutil.rmtree(work_dir)
        raise FileNotFoundError("track.json not found inside .synth archive")

    with open(track_json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    track_data = _deserialize(raw)
    track_data.audio_filename = _find_audio_file(work_dir)

    return track_data, work_dir


def save(track_data: TrackData, work_dir: str, output_path: str) -> str:
    """Serialize TrackData back to track.json and repackage as .synth.

    Writes the updated track.json into work_dir, then zips the entire
    work_dir into output_path. Returns the absolute output path.
    """
    output_path = Path(output_path).resolve()
    raw = _serialize(track_data)

    track_json_path = _find_track_json(work_dir)
    if track_json_path is None:
        track_json_path = os.path.join(work_dir, "track.json")

    with open(track_json_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(work_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                arc_name = os.path.relpath(abs_path, work_dir)
                zf.write(abs_path, arc_name)

    return str(output_path)


def cleanup(work_dir: str) -> None:
    """Remove the temporary working directory."""
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)


def get_audio_path(work_dir: str) -> str | None:
    """Return the absolute path to the audio file inside the work directory."""
    filename = _find_audio_file(work_dir)
    if filename is None:
        return None
    return os.path.join(work_dir, filename)


# -- Coordinate system reference --
#
# Synth Riders uses a 2D grid projected in 3D space:
#   X: horizontal, roughly -3.0 (far left) to +3.0 (far right), center = 0
#   Y: vertical,   roughly  0.0 (floor)    to +3.0 (ceiling),   center ~ 1.5
#   Time: beat position (float), advances along the Z axis toward the player
#
# Notes and rail nodes store [X, Y] in the "Position" array and beat time
# in the "time" field. The parser preserves these as-is.


def _find_track_json(work_dir: str) -> str | None:
    """Locate track.json inside the extracted directory (may be nested)."""
    for root, _dirs, files in os.walk(work_dir):
        for f in files:
            if f.lower() == "track.json":
                return os.path.join(root, f)
    return None


def _find_audio_file(work_dir: str) -> str | None:
    """Return the filename of the first audio file found."""
    for f in os.listdir(work_dir):
        if Path(f).suffix.lower() in AUDIO_EXTENSIONS:
            return f
    return None


def _deserialize(raw: dict) -> TrackData:
    """Convert raw track.json dict into a TrackData model."""
    track = TrackData(
        bpm=raw.get("BPM", 120.0),
        offset=raw.get("Offset", 0.0),
        name=raw.get("Name", ""),
        author=raw.get("Author", ""),
        raw=raw,
    )

    for diff in DIFFICULTIES:
        notes_key = f"Notes_{diff}"
        rails_key = f"Slides_{diff}"
        walls_key = f"Crouches_{diff}"

        difficulty = Difficulty(
            name=diff,
            notes=[Note.from_dict(n) for n in raw.get(notes_key, [])],
            rails=[Rail.from_dict(r) for r in raw.get(rails_key, [])],
            walls=[Wall.from_dict(w) for w in raw.get(walls_key, [])],
        )
        track.difficulties[diff] = difficulty

    return track


def _serialize(track_data: TrackData) -> dict:
    """Convert TrackData back to a track.json dict, preserving unknown fields."""
    raw = dict(track_data.raw)

    raw["BPM"] = track_data.bpm
    raw["Offset"] = track_data.offset
    raw["Name"] = track_data.name
    raw["Author"] = track_data.author

    for diff_name, diff in track_data.difficulties.items():
        raw[f"Notes_{diff_name}"] = [n.to_dict() for n in diff.notes]
        raw[f"Slides_{diff_name}"] = [r.to_dict() for r in diff.rails]
        raw[f"Crouches_{diff_name}"] = [w.to_dict() for w in diff.walls]

    return raw


def inspect(synth_path: str) -> dict:
    """Quick summary of a .synth file without full deserialization."""
    track_data, work_dir = load(synth_path)
    try:
        summary = {
            "name": track_data.name,
            "author": track_data.author,
            "bpm": track_data.bpm,
            "offset": track_data.offset,
            "audio_file": track_data.audio_filename,
            "difficulties": {},
        }
        for diff_name, diff in track_data.difficulties.items():
            if diff.notes or diff.rails or diff.walls:
                summary["difficulties"][diff_name] = {
                    "notes": len(diff.notes),
                    "rails": len(diff.rails),
                    "walls": len(diff.walls),
                }
        return summary
    finally:
        cleanup(work_dir)
