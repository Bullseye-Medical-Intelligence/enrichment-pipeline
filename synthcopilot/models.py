"""Data models for Synth Riders track data."""

from dataclasses import dataclass, field


DIFFICULTIES = ("Easy", "Normal", "Hard", "Expert", "Master")

HAND_RIGHT = 0
HAND_LEFT = 1


@dataclass
class Note:
    time: float
    x: float
    y: float
    hand_type: int = HAND_RIGHT
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.raw)
        d["time"] = self.time
        d["Position"] = [self.x, self.y]
        d["Type"] = self.hand_type
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        pos = d.get("Position", [0.0, 0.0])
        return cls(
            time=d.get("time", 0.0),
            x=pos[0],
            y=pos[1],
            hand_type=d.get("Type", HAND_RIGHT),
            raw=d,
        )


@dataclass
class RailNode:
    time: float
    x: float
    y: float
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.raw)
        d["time"] = self.time
        d["Position"] = [self.x, self.y]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RailNode":
        pos = d.get("Position", [0.0, 0.0])
        return cls(
            time=d.get("time", 0.0),
            x=pos[0],
            y=pos[1],
            raw=d,
        )


@dataclass
class Rail:
    hand_type: int = HAND_RIGHT
    nodes: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.raw)
        d["Type"] = self.hand_type
        d["notes"] = [n.to_dict() for n in self.nodes]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Rail":
        return cls(
            hand_type=d.get("Type", HAND_RIGHT),
            nodes=[RailNode.from_dict(n) for n in d.get("notes", [])],
            raw=d,
        )


@dataclass
class Wall:
    time: float
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.raw)
        d["time"] = self.time
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Wall":
        return cls(time=d.get("time", 0.0), raw=d)


@dataclass
class Difficulty:
    name: str
    notes: list = field(default_factory=list)
    rails: list = field(default_factory=list)
    walls: list = field(default_factory=list)


@dataclass
class TrackData:
    bpm: float = 120.0
    offset: float = 0.0
    name: str = ""
    author: str = ""
    difficulties: dict = field(default_factory=dict)
    audio_filename: str | None = None
    raw: dict = field(default_factory=dict)

    def seconds_to_beats(self, seconds: float) -> float:
        """Convert a timestamp in seconds to beat time."""
        return (seconds - self.offset) * (self.bpm / 60.0)

    def beats_to_seconds(self, beats: float) -> float:
        """Convert beat time back to seconds."""
        return (beats * 60.0 / self.bpm) + self.offset
