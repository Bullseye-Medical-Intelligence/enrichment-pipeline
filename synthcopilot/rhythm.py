"""Rhythm engine: audio onset detection and note snapping via librosa.

Analyzes a time-slice of the audio file to detect transient spikes
(drum hits, synth plucks, percussive attacks) and maps them onto
positions along a generated rail curve.

Physical constraints:
- Cooldown threshold prevents humanly impossible note clusters (default
  50ms minimum gap between successive notes on the same hand).
- Velocity gate rejects notes that would require arm travel faster than
  a configurable max speed, measured in Synth Riders grid-units/second.
"""

import math

import numpy as np

from synthcopilot.models import Note, RailNode

try:
    import librosa
except ImportError:
    librosa = None

MIN_NOTE_GAP_SEC = 0.050
MAX_HAND_SPEED = 6.0


def detect_onsets(
    audio_path: str,
    start_sec: float,
    end_sec: float,
    sensitivity: float = 1.0,
) -> list[float]:
    """Detect transient onset timestamps within a time window.

    Args:
        audio_path: path to the audio file (.ogg, .wav, .mp3).
        start_sec: start of the analysis window in seconds.
        end_sec: end of the analysis window in seconds.
        sensitivity: multiplier on onset detection threshold
                     (lower = more onsets detected).

    Returns:
        List of onset timestamps in seconds (absolute, not relative).
    """
    if librosa is None:
        raise ImportError("librosa is required: pip install librosa soundfile")

    y, sr = librosa.load(audio_path, sr=None, offset=start_sec, duration=end_sec - start_sec)

    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onset_frames = librosa.onset.onset_detect(
        y=y,
        sr=sr,
        onset_envelope=onset_env,
        delta=0.07 / max(sensitivity, 0.1),
        wait=int(sr * 0.03 / 512),
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)
    absolute_times = onset_times + start_sec

    return [float(t) for t in absolute_times if start_sec <= t <= end_sec]


def apply_cooldown(
    onset_times: list[float], min_gap: float = MIN_NOTE_GAP_SEC
) -> list[float]:
    """Remove onsets that fall within the cooldown window of a prior onset."""
    if not onset_times:
        return []
    filtered = [onset_times[0]]
    for t in onset_times[1:]:
        if t - filtered[-1] >= min_gap:
            filtered.append(t)
    return filtered


def snap_notes_to_rail(
    onset_times_sec: list[float],
    rail_nodes: list[RailNode],
    start_sec: float,
    end_sec: float,
    bpm: float,
    offset: float = 0.0,
    hand_type: int = 0,
    min_gap: float = MIN_NOTE_GAP_SEC,
    max_hand_speed: float = MAX_HAND_SPEED,
) -> list[Note]:
    """Place notes along a rail curve at detected onset timestamps.

    For each onset, finds the corresponding position on the rail by
    interpolating between rail nodes based on time, then creates a
    Note at that (x, y, beat_time).

    Physical constraints are applied in two passes:
    1. Cooldown: drop onsets closer than min_gap seconds apart.
    2. Velocity gate: drop notes that would require hand travel
       faster than max_hand_speed grid-units/second from the
       previous accepted note.

    Args:
        onset_times_sec: onset timestamps in seconds.
        rail_nodes: the generated rail to snap notes onto.
        start_sec: start time in seconds (matches rail start).
        end_sec: end time in seconds (matches rail end).
        bpm: beats per minute for time conversion.
        offset: audio offset in seconds.
        hand_type: 0=right, 1=left.
        min_gap: minimum seconds between notes (cooldown).
        max_hand_speed: max grid-units/second between consecutive notes.

    Returns:
        List of Note objects placed along the rail at onset positions.
    """
    if not rail_nodes or not onset_times_sec:
        return []

    duration = end_sec - start_sec
    if duration <= 0:
        return []

    onset_times_sec = apply_cooldown(sorted(onset_times_sec), min_gap)

    notes = []
    prev_pos = None
    prev_sec = None

    for t_sec in onset_times_sec:
        progress = (t_sec - start_sec) / duration
        progress = max(0.0, min(1.0, progress))

        x, y = _interpolate_position(rail_nodes, progress)

        if max_hand_speed > 0 and prev_pos is not None:
            dt = t_sec - prev_sec
            if dt > 0:
                dist = math.hypot(x - prev_pos[0], y - prev_pos[1])
                if dist / dt > max_hand_speed:
                    continue

        beat_time = (t_sec - offset) * (bpm / 60.0)
        notes.append(Note(time=beat_time, x=x, y=y, hand_type=hand_type))
        prev_pos = (x, y)
        prev_sec = t_sec

    return notes


def _interpolate_position(
    nodes: list[RailNode], progress: float
) -> tuple[float, float]:
    """Find the (x, y) position at a fractional progress along the rail."""
    if len(nodes) == 1:
        return nodes[0].x, nodes[0].y

    idx_float = progress * (len(nodes) - 1)
    idx = int(idx_float)
    frac = idx_float - idx

    if idx >= len(nodes) - 1:
        return nodes[-1].x, nodes[-1].y

    a = nodes[idx]
    b = nodes[idx + 1]
    x = a.x + (b.x - a.x) * frac
    y = a.y + (b.y - a.y) * frac
    return x, y


def get_audio_duration(audio_path: str) -> float:
    """Return the total duration of an audio file in seconds."""
    if librosa is None:
        raise ImportError("librosa is required: pip install librosa soundfile")
    return float(librosa.get_duration(path=audio_path))
