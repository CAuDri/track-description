"""Analytical width transitions between opposite intersection arms."""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class WidthTransition:
    """@brief A quintic boundary in intersection-local coordinates.

    The first arm points outward along ``axis``. The boundary travels inward
    from its port to the opposite port over ``2 * port_distance``. Its lateral
    position and optional marking-width change both use the same monotone
    quintic, with zero slope and curvature at both endpoints.
    """

    port_distance: float
    axis: Point
    start_boundary: float
    end_boundary: float
    start_width: float
    end_width: float

    def __post_init__(self):
        for name in ("port_distance", "start_boundary", "end_boundary", "start_width", "end_width"):
            if _finite(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive")
        if len(self.axis) != 2:
            raise ValueError("Transition axis needs two components")
        for value in self.axis:
            _finite(value, "axis component")
        if not math.isclose(math.hypot(*self.axis), 1, rel_tol=0, abs_tol=1e-10):
            raise ValueError("Transition axis must be a unit vector")

    @staticmethod
    def _parameter(t: float) -> float:
        t = _finite(t, "transition parameter")
        if not 0 <= t <= 1:
            raise ValueError("Transition parameter must lie between zero and one")
        return t

    @staticmethod
    def _blend(t: float) -> float:
        return t * t * t * (10 + t * (-15 + 6 * t))

    @staticmethod
    def _interpolate(first: float, second: float, t: float) -> float:
        # Preserve the source cross-sections exactly at the two ports.
        if t == 0:
            return first
        if t == 1:
            return second
        return first + (second - first) * WidthTransition._blend(t)

    def width(self, t: float) -> float:
        """@return The physical marking width, interpolated without overshoot."""
        return self._interpolate(self.start_width, self.end_width, self._parameter(t))

    def point(self, t: float, *, side: int = 0) -> Point:
        """@return The centerline, inner (-1) or outer (+1) paint-edge point."""
        t = self._parameter(t)
        if side not in (-1, 0, 1):
            raise ValueError("Transition side must be -1, zero or +1")
        d = self.port_distance
        longitudinal = d * (1 - 2 * t)
        lateral = self._interpolate(self.start_boundary, self.end_boundary, t)
        ux, uy = self.axis
        x, y = d + longitudinal * ux - lateral * uy, longitudinal * uy + lateral * ux
        if side:
            nx, ny = self.normal(t)
            half = side * self.width(t) / 2
            x, y = x + half * nx, y + half * ny
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Transition point exceeds finite coordinates")
        return x, y

    def normal(self, t: float) -> Point:
        """@return The outward unit normal, on the right of the directed boundary."""
        t = self._parameter(t)
        lateral = (self.end_boundary - self.start_boundary) * 30 * t * t * (1 - t) * (1 - t)
        longitudinal = -2 * self.port_distance
        ux, uy = self.axis
        dx, dy = longitudinal * ux - lateral * uy, longitudinal * uy + lateral * ux
        speed = math.hypot(dx, dy)
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("Transition tangent exceeds finite coordinates")
        return dy / speed, -dx / speed

    def _second_derivative_bound(self) -> float:
        """Bound all three curves, including variable-width normal offsets.

        For the quintic, max|S'|=15/8, max|S''|=10/sqrt(3), and
        max|S'''|=60. The centerline speed is at least the full port span.
        Differentiating its unit normal gives |n'| <= A2/L and the conservative
        |n''| <= A3/L + 3(A2/L)^2. Thus the offset curve p+h*n has the bound
        |p''| + |h''| + 2|h'||n'| + |h||n''| used below.
        """
        axis_length = math.hypot(*self.axis)
        length = 2 * self.port_distance * axis_length
        difference = abs(self.end_boundary - self.start_boundary) * axis_length
        a2, a3 = difference * 10 / math.sqrt(3), difference * 60
        normal_first = a2 / length
        normal_second = a3 / length + 3 * normal_first * normal_first
        half_difference = abs(self.end_width - self.start_width) / 2
        half_maximum = max(self.start_width, self.end_width) / 2
        return (
            a2
            + half_difference * 10 / math.sqrt(3)
            + 2 * half_difference * 15 / 8 * normal_first
            + half_maximum * normal_second
        )

    def sample(
        self, tolerance: float, *, side: int = 0, start: float = 0, end: float = 1
    ) -> list[Point]:
        """@return A shared-grid approximation with a proven chord-error bound.

        A twice differentiable curve has chord error at most M*dt^2/8 when
        M bounds its second derivative. This also detects the quintic's S bend
        whose midpoint lies on the endpoint chord. All three paint curves and
        partial requests use the same global grid; t=0.5 is always present.
        """
        tolerance = _finite(tolerance, "transition tolerance")
        if tolerance <= 0:
            raise ValueError("Transition tolerance must be positive")
        start, end = self._parameter(start), self._parameter(end)
        if side not in (-1, 0, 1):
            raise ValueError("Transition side must be -1, zero or +1")
        bound = self._second_derivative_bound()
        required = bound / tolerance / 8
        if not math.isfinite(required):
            raise ValueError("Width transition exceeds the sampling workload")
        count = max(1, math.ceil(math.sqrt(required)))
        if count + 3 > 50000:
            raise ValueError("Width transition exceeds 50000 samples")
        low, high = sorted((start, end))
        stations = {start, end}
        stations.update(i / count for i in range(count + 1) if low <= i / count <= high)
        if low <= 0.5 <= high:
            stations.add(0.5)
        return [self.point(t, side=side) for t in sorted(stations, reverse=start > end)]
