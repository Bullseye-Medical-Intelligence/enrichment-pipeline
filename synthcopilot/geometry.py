"""Geometry engine: Bezier curves and rail generation with complexity modifiers.

Generates intermediate rail nodes that smoothly connect a start and end
anchor point. Complexity modifiers inject algorithmic geometry (sine waves,
spirals, zigzag) into the interpolated path for Dubstep-style chaotic rails
while preserving the start and end positions exactly.

Envelope windowing uses a cubic smoothstep (Hermite) instead of a raw sine
taper. The smoothstep has zero first-derivative at t=0 and t=1, which
eliminates the inflection-clipping problem: modifier amplitude ramps
organically near anchors instead of producing velocity spikes where a sine
taper's slope is steepest.
"""

import math

import numpy as np

from synthcopilot.models import RailNode

FADE_ZONE = 0.15
MAX_VELOCITY = 4.0


def generate_rail(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    num_nodes: int = 16,
    rail_type: str = "smooth",
    complexity: int = 0,
    fade_zone: float = FADE_ZONE,
    max_velocity: float = MAX_VELOCITY,
) -> list[RailNode]:
    """Generate rail nodes between two anchor points.

    Args:
        start: (x, y, time) of the starting anchor.
        end: (x, y, time) of the ending anchor.
        num_nodes: total number of nodes including start and end.
        rail_type: "smooth", "wave", "spiral", or "zigzag".
        complexity: intensity of the modifier (0 = pure curve).
        fade_zone: fraction of the rail (0–0.5) where the envelope
                   ramps from zero to full. Larger = gentler entry/exit.
        max_velocity: if > 0, clamp node-to-node displacement to prevent
                      physically impossible hand movements.

    Returns:
        List of RailNode objects forming the rail.
    """
    if num_nodes < 2:
        num_nodes = 2

    t_values = np.linspace(0.0, 1.0, num_nodes)

    p0 = np.array([start[0], start[1]])
    p3 = np.array([end[0], end[1]])
    p1, p2 = _auto_control_points(p0, p3)

    points = np.array([_cubic_bezier(t, p0, p1, p2, p3) for t in t_values])
    times = np.linspace(start[2], end[2], num_nodes)

    if complexity > 0 and rail_type != "smooth":
        points = _apply_modifier(
            points, t_values, rail_type, complexity, p0, p3, fade_zone
        )

    points[0] = p0
    points[-1] = p3

    if max_velocity > 0 and num_nodes > 2:
        points = _clamp_velocity(points, times, max_velocity)
        points[0] = p0
        points[-1] = p3

    return [
        RailNode(time=float(times[i]), x=float(points[i][0]), y=float(points[i][1]))
        for i in range(num_nodes)
    ]


def _cubic_bezier(
    t: float,
    p0: np.ndarray,
    p1: np.ndarray,
    p2: np.ndarray,
    p3: np.ndarray,
) -> np.ndarray:
    """Evaluate a cubic Bezier curve at parameter t in [0, 1]."""
    u = 1.0 - t
    return u**3 * p0 + 3 * u**2 * t * p1 + 3 * u * t**2 * p2 + t**3 * p3


def _auto_control_points(
    p0: np.ndarray, p3: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Generate smooth control points for a cubic Bezier between two endpoints."""
    dx = p3[0] - p0[0]
    dy = p3[1] - p0[1]
    dist = math.hypot(dx, dy)
    tension = max(dist * 0.35, 0.3)

    mid = (p0 + p3) / 2.0
    perp = np.array([-dy, dx])
    if np.linalg.norm(perp) > 0:
        perp = perp / np.linalg.norm(perp)

    p1 = p0 + np.array([dx * 0.33, dy * 0.33]) + perp * tension * 0.2
    p2 = p3 - np.array([dx * 0.33, dy * 0.33]) - perp * tension * 0.2

    return p1, p2


def _smoothstep_envelope(t: float, fade: float) -> float:
    """Compute a cubic smoothstep envelope with configurable fade zones.

    Returns 0 at t=0 and t=1, ramping to 1 over the fade zone at each end.
    The Hermite basis (3x^2 - 2x^3) has zero first-derivative at the
    boundaries, so modifier amplitude enters and exits with zero velocity —
    no inflection clipping near anchor points.
    """
    if t <= 0.0 or t >= 1.0:
        return 0.0
    fade = max(fade, 0.01)
    if t < fade:
        x = t / fade
        return x * x * (3.0 - 2.0 * x)
    if t > 1.0 - fade:
        x = (1.0 - t) / fade
        return x * x * (3.0 - 2.0 * x)
    return 1.0


def _apply_modifier(
    points: np.ndarray,
    t_values: np.ndarray,
    rail_type: str,
    complexity: int,
    p0: np.ndarray,
    p3: np.ndarray,
    fade_zone: float = FADE_ZONE,
) -> np.ndarray:
    """Apply wave/spiral/zigzag modifier to interpolated curve points."""
    n = len(points)
    direction = p3 - p0
    length = np.linalg.norm(direction)
    if length < 1e-6:
        tangent = np.array([1.0, 0.0])
    else:
        tangent = direction / length
    normal = np.array([-tangent[1], tangent[0]])

    amplitude = 0.15 + complexity * 0.12
    amplitude = min(amplitude, 1.5)
    freq = 1.0 + complexity * 0.8

    for i in range(1, n - 1):
        t = t_values[i]
        envelope = _smoothstep_envelope(t, fade_zone)

        if rail_type == "wave":
            offset = math.sin(2 * math.pi * freq * t) * amplitude * envelope
            points[i] += normal * offset

        elif rail_type == "spiral":
            angle = 2 * math.pi * freq * t
            r = amplitude * envelope
            points[i][0] += math.cos(angle) * r
            points[i][1] += math.sin(angle) * r

        elif rail_type == "zigzag":
            sawtooth = (2.0 * (freq * t % 1.0) - 1.0)
            offset = sawtooth * amplitude * envelope
            points[i] += normal * offset

    return points


def _clamp_velocity(
    points: np.ndarray, times: np.ndarray, max_vel: float
) -> np.ndarray:
    """Limit node-to-node displacement so hand velocity stays playable.

    Uses bidirectional clamping: a forward pass anchored at the start
    and a backward pass anchored at the end, blended by proximity to
    each anchor. This prevents the last-segment spike that a single
    forward pass would create when the end anchor is re-pinned.
    """
    n = len(points)
    if n < 3:
        return points

    fwd = points.copy()
    for i in range(1, n):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            continue
        delta = fwd[i] - fwd[i - 1]
        dist = np.linalg.norm(delta)
        if dist > 0 and dist / dt > max_vel:
            fwd[i] = fwd[i - 1] + delta * (max_vel * dt / dist)

    bwd = points.copy()
    for i in range(n - 2, -1, -1):
        dt = times[i + 1] - times[i]
        if dt <= 0:
            continue
        delta = bwd[i] - bwd[i + 1]
        dist = np.linalg.norm(delta)
        if dist > 0 and dist / dt > max_vel:
            bwd[i] = bwd[i + 1] + delta * (max_vel * dt / dist)

    for i in range(1, n - 1):
        blend = i / (n - 1)
        points[i] = fwd[i] * (1.0 - blend) + bwd[i] * blend

    return points


def catmull_rom_chain(
    anchors: list[tuple[float, float, float]],
    nodes_per_segment: int = 12,
    rail_type: str = "smooth",
    complexity: int = 0,
) -> list[RailNode]:
    """Generate a smooth rail through multiple anchor points.

    Uses Catmull-Rom interpolation for smooth transitions between segments,
    then applies the selected modifier.
    """
    if len(anchors) < 2:
        raise ValueError("Need at least 2 anchor points")

    if len(anchors) == 2:
        return generate_rail(anchors[0], anchors[1], nodes_per_segment, rail_type, complexity)

    all_nodes = []
    for i in range(len(anchors) - 1):
        segment = generate_rail(
            anchors[i], anchors[i + 1], nodes_per_segment, rail_type, complexity
        )
        if i > 0:
            segment = segment[1:]
        all_nodes.extend(segment)

    return all_nodes
