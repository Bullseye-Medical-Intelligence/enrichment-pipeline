"""CLI interface for SynthCoPilot."""

import argparse
import json
import sys

from synthcopilot import __version__
from synthcopilot.parser import cleanup, get_audio_path, inspect, load, save
from synthcopilot.geometry import generate_rail
from synthcopilot.rhythm import detect_onsets, snap_notes_to_rail
from synthcopilot.models import HAND_LEFT, HAND_RIGHT, Rail


def parse_timestamp(ts: str) -> float:
    """Convert mm:ss or mm:ss.ms string to seconds."""
    parts = ts.split(":")
    if len(parts) == 2:
        minutes = float(parts[0])
        seconds = float(parts[1])
        return minutes * 60.0 + seconds
    if len(parts) == 1:
        return float(parts[0])
    raise ValueError(f"Invalid timestamp format: {ts} (use mm:ss or seconds)")


def cmd_inspect(args):
    """Print a summary of a .synth file."""
    summary = inspect(args.file)
    print(json.dumps(summary, indent=2))


def cmd_generate(args):
    """Generate a rail between two time anchors, optionally snapping notes to audio."""
    track_data, work_dir = load(args.file)
    try:
        start_sec = parse_timestamp(args.start)
        end_sec = parse_timestamp(args.end)
        difficulty = args.difficulty
        hand = HAND_LEFT if args.hand == "left" else HAND_RIGHT

        start_beat = track_data.seconds_to_beats(start_sec)
        end_beat = track_data.seconds_to_beats(end_sec)

        start_anchor = (args.start_x, args.start_y, start_beat)
        end_anchor = (args.end_x, args.end_y, end_beat)

        rail_nodes = generate_rail(
            start=start_anchor,
            end=end_anchor,
            num_nodes=args.nodes,
            rail_type=args.rail_type,
            complexity=args.complexity,
            fade_zone=args.fade_zone,
            max_velocity=args.max_velocity,
        )

        diff = track_data.difficulties.get(difficulty)
        if diff is None:
            print(f"Difficulty '{difficulty}' not found in track", file=sys.stderr)
            return 1

        new_rail = Rail(hand_type=hand, nodes=rail_nodes)
        diff.rails.append(new_rail)
        print(f"Added rail: {len(rail_nodes)} nodes, {args.rail_type} (complexity {args.complexity})")

        if args.snap_to_audio:
            audio_path = get_audio_path(work_dir)
            if audio_path is None:
                print("No audio file found in .synth archive", file=sys.stderr)
                return 1

            onsets = detect_onsets(audio_path, start_sec, end_sec, args.sensitivity)
            notes = snap_notes_to_rail(
                onsets, rail_nodes, start_sec, end_sec,
                track_data.bpm, track_data.offset, hand,
                min_gap=args.cooldown,
                max_hand_speed=args.max_hand_speed,
            )
            diff.notes.extend(notes)
            print(f"Snapped {len(notes)} notes to audio onsets")

        output = args.output or args.file.replace(".synth", "_modified.synth")
        save(track_data, work_dir, output)
        print(f"Saved: {output}")
    finally:
        cleanup(work_dir)


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        prog="synthcopilot",
        description="SynthCoPilot — semi-automated beatmap generation for Synth Riders",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # -- inspect --
    p_inspect = subparsers.add_parser("inspect", help="Show .synth file summary")
    p_inspect.add_argument("file", help="Path to .synth file")
    p_inspect.set_defaults(func=cmd_inspect)

    # -- generate --
    p_gen = subparsers.add_parser("generate", help="Generate rails and snap notes")
    p_gen.add_argument("--file", required=True, help="Path to .synth file")
    p_gen.add_argument("--start", required=True, help="Start timestamp (mm:ss)")
    p_gen.add_argument("--end", required=True, help="End timestamp (mm:ss)")
    p_gen.add_argument("--start-x", type=float, default=0.0, help="Start X position (default: 0.0)")
    p_gen.add_argument("--start-y", type=float, default=1.5, help="Start Y position (default: 1.5)")
    p_gen.add_argument("--end-x", type=float, default=0.0, help="End X position (default: 0.0)")
    p_gen.add_argument("--end-y", type=float, default=1.5, help="End Y position (default: 1.5)")
    p_gen.add_argument("--rail-type", choices=["smooth", "wave", "spiral", "zigzag"], default="smooth",
                        help="Rail curve modifier type")
    p_gen.add_argument("--complexity", type=int, default=0, help="Modifier intensity (0 = smooth)")
    p_gen.add_argument("--nodes", type=int, default=16, help="Number of rail nodes (default: 16)")
    p_gen.add_argument("--fade-zone", type=float, default=0.15,
                        help="Envelope fade fraction near anchors (0.0–0.5, default: 0.15)")
    p_gen.add_argument("--max-velocity", type=float, default=4.0,
                        help="Max rail node-to-node velocity in grid-units/beat (0 = unclamped)")
    p_gen.add_argument("--snap-to-audio", action="store_true", help="Snap notes to audio transients")
    p_gen.add_argument("--sensitivity", type=float, default=1.0,
                        help="Onset detection sensitivity (higher = more notes)")
    p_gen.add_argument("--cooldown", type=float, default=0.050,
                        help="Minimum seconds between notes (default: 0.050)")
    p_gen.add_argument("--max-hand-speed", type=float, default=6.0,
                        help="Max hand speed in grid-units/sec for note filtering (0 = off)")
    p_gen.add_argument("--difficulty", default="Expert", help="Target difficulty (default: Expert)")
    p_gen.add_argument("--hand", choices=["left", "right"], default="right", help="Hand assignment")
    p_gen.add_argument("--output", help="Output .synth path (default: <input>_modified.synth)")
    p_gen.set_defaults(func=cmd_generate)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    return args.func(args)
