#!/usr/bin/env python3
"""Assemble CAuDri roads, resolve paths and check their planar occupied areas."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from functools import lru_cache
import json
import math
from pathlib import Path
import sys

from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.strtree import STRtree

if __package__:
    from .expand_track import (Diagnostic, ExpansionResult, InputError, SchemaBundle,
                               child, expand_track, load_track, prepare_track,
                               source_location, write_file)
    from .validate_schema import CheckResult, validate_schema
else:
    from expand_track import (Diagnostic, ExpansionResult, InputError, SchemaBundle,
                              child, expand_track, load_track, prepare_track,
                              source_location, write_file)
    from validate_schema import CheckResult, validate_schema


__all__ = ["GeometryResult", "GeometryModel", "validate_geometry"]
CHECKS = ("primitives", "connections", "surfaces", "overlaps", "placements", "spans")
OPPOSITE = {"entry": "straight", "straight": "entry", "left": "right", "right": "left"}


def angle(value):
    return (value + math.pi) % math.tau - math.pi


def add(a, b):
    return a[0] + b[0], a[1] + b[1]


def scale(a, factor):
    return a[0] * factor, a[1] * factor


def unit(yaw):
    return math.cos(yaw), math.sin(yaw)


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def transform(point, pose):
    c, s = math.cos(pose[2]), math.sin(pose[2])
    return pose[0] + c*point[0] - s*point[1], pose[1] + s*point[0] + c*point[1]


def rectangle(center, length, width, yaw):
    return Polygon([transform(p, (*center, yaw)) for p in
                    [(-length/2, -width/2), (length/2, -width/2),
                     (length/2, width/2), (-length/2, width/2)]])


def objects(container):
    return ((key, value) for key, value in container.items() if isinstance(value, dict)) \
        if isinstance(container, dict) else ()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"Expected a finite number, got {value!r}")
    return float(value)


class PathError(ValueError):
    """A requested directed path has no unique geometric interpretation."""


@dataclass
class StageResult:
    """@brief Check outcomes and source-linked findings for one validation stage."""

    stage: str
    diagnostics: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    prerequisites: list = field(default_factory=list)

    @property
    def valid(self):
        if any(d.severity == "error" for d in self.diagnostics):
            return False
        if not self.checks or any(c.status in {"blocked", "unsupported"} or
                                 c.details.get("blocked_paths") for c in self.checks):
            return None
        if any(c.status == "skipped" and c.details.get("applicable", True) for c in self.checks):
            return None
        if any(p.get("valid") is None for p in self.prerequisites):
            return None
        return True

    def report(self):
        """@return JSON-compatible report; computed geometry stays in memory."""
        return {"stage": self.stage, "valid": self.valid,
                "checks": [asdict(c) for c in self.checks],
                "diagnostics": [asdict(d) for d in self.diagnostics],
                "prerequisites": self.prerequisites}


@dataclass
class GeometryResult(StageResult):
    stage: str = "geometry"
    model: object = None
    expansion: object = None
    schema_result: object = None


class Reporter:
    """Collect findings by check group, retaining partially blocked work."""

    def __init__(self, result, expansion, groups, skip=()):
        self.result, self.expansion = result, expansion
        self.groups, self.skip = tuple(groups), set(skip)
        unknown = self.skip - set(self.groups)
        if unknown:
            raise InputError(f"Unknown {result.stage} checks: {', '.join(sorted(unknown))}")
        self.findings, self.blocked = defaultdict(int), defaultdict(dict)

    def issue(self, group, code, path, message, severity="error", schema_ref=None, **details):
        if group in self.skip:
            return
        self.findings[group] += 1
        self.result.diagnostics.append(Diagnostic(
            f"{self.result.stage}.{code}", severity, path, message,
            source_location(self.expansion.source, path, self.expansion.provenance),
            schema_ref, {"check": group, **details}))

    def block(self, group, path, message):
        if group not in self.skip:
            self.blocked[group][path] = message

    def finish(self):
        for group in self.groups:
            count, blocked = self.findings[group], self.blocked[group]
            state = "skipped" if group in self.skip else "failed" if count else "blocked" if blocked else "passed"
            details = {"blocked_paths": blocked} if blocked else {}
            self.result.checks.append(CheckResult(group, state,
                "Disabled by caller." if state == "skipped" else
                f"{count} finding(s), {len(blocked)} blocked operation(s).", details))
        return self.result


def prepare(track, bundle=None, source_uri=None, check_schema=False):
    if isinstance(track, ExpansionResult):
        expanded = track
        bundle = bundle or SchemaBundle(track.track["$schema"])
    else:
        doc = prepare_track(track, bundle=bundle, source_uri=source_uri)
        bundle = bundle or SchemaBundle.for_track(doc)
        expanded = expand_track(doc, bundle=bundle)
    return expanded, bundle, validate_schema(expanded, bundle=bundle, check_schema=check_schema)


def integrate(function, end, max_curvature=0, tolerance=1e-10):
    """Adaptive Simpson integration for the two clothoid coordinates."""
    def refine(a, b, fa, fm, fb, estimate, eps, depth):
        mid = (a+b)/2
        fl, fr = function((a+mid)/2), function((mid+b)/2)
        left, right = (mid-a)*(fa+4*fl+fm)/6, (b-mid)*(fm+4*fr+fb)/6
        delta = left + right - estimate
        if abs(delta) <= 15*eps:
            return left + right + delta/15
        if depth == 0:
            raise ValueError("Clothoid integration did not converge")
        return (refine(a, mid, fa, fl, fm, left, eps/2, depth-1) +
                refine(mid, b, fm, fr, fb, right, eps/2, depth-1))

    # Split by accumulated angular change to avoid aliasing a highly curved integral.
    count = max(1, math.ceil(abs(end)*max_curvature/(math.pi/4)))
    if count > 10000:
        raise ValueError("Clothoid exceeds the supported integration workload")
    result = 0.0
    for i in range(count):
        a, b = end*i/count, end*(i+1)/count
        fa, fm, fb = function(a), function((a+b)/2), function(b)
        result += refine(a, b, fa, fm, fb, (b-a)*(fa+4*fm+fb)/6, tolerance/count, 22)
    return result


@dataclass(frozen=True)
class Curve:
    kind: str
    length: float
    k0: float = 0
    k1: float = 0

    def yaw(self, distance):
        return self.k0*distance + (self.k1-self.k0)*distance*distance/(2*self.length)

    @lru_cache(maxsize=40000)
    def point(self, distance):
        if self.kind == "straight":
            return distance, 0.0
        if self.k0 == self.k1:
            if self.k0 == 0:
                return distance, 0.0
            return math.sin(self.k0*distance)/self.k0, (1-math.cos(self.k0*distance))/self.k0
        curvature = max(abs(self.k0), abs(self.k1))
        return (integrate(lambda s: math.cos(self.yaw(s)), distance, curvature),
                integrate(lambda s: math.sin(self.yaw(s)), distance, curvature))

    def stations(self, tolerance, offset=0, start=0, end=None):
        end = self.length if end is None else end
        k = max(abs(self.k0), abs(self.k1))
        # Linear interpolation error is bounded by max(|p''|) * step² / 8.
        acceleration = k*(1+k*abs(offset)) + abs(offset*(self.k1-self.k0)/self.length)
        step = math.sqrt(8*tolerance/acceleration) if acceleration else self.length
        count = max(1, math.ceil(abs(end-start)/max(step, 1e-8)))
        if count > 50000:
            raise ValueError("Curve needs more than 50000 samples at the selected tolerance")
        return [start+(end-start)*i/count for i in range(count+1)]


@dataclass
class Section:
    identifier: str
    value: dict
    curve: Curve
    ports: dict
    pose: tuple = (0., 0., 0.)
    component: int = -1
    surface: object = None
    reference: object = None
    arms: dict = field(default_factory=dict)
    stations: list = field(default_factory=list)
    chord_lengths: list = field(default_factory=list)

    @property
    def path(self):
        return child("/sections", self.identifier)

    @property
    def intersection(self):
        return self.value["type"] == "intersection"

    @property
    def width(self):
        return number(self.value["road"]["lane_width"])

    @property
    def marking(self):
        return number(self.value["road"]["marking_width"])

    @property
    def boundary(self):
        """@return Reference-line offset of the outer marking's centerline."""
        return self.width + self.marking

    @property
    def half(self):
        return self.width + 1.5*self.marking

    @property
    def inner(self):
        return self.width + self.marking/2

    def point(self, s, offset=0):
        local = add(self.curve.point(s), scale(unit(self.curve.yaw(s)+math.pi/2), offset))
        return transform(local, self.pose)

    def yaw(self, s):
        return self.pose[2] + self.curve.yaw(s)

    def port(self, name):
        x, y, yaw = self.ports[name]
        return (*transform((x, y), self.pose), angle(yaw+self.pose[2]))

    def project(self, point):
        """Return reference arc length, refining the closest sampled segment."""
        if self.intersection or self.reference is None:
            raise PathError("Projection needs a resolved linear reference curve")
        distance = self.reference.project(Point(point))
        i = min(len(self.stations)-2, max(0, bisect_right(self.chord_lengths, distance)-1))
        low, high = self.stations[max(0, i-1)], self.stations[min(len(self.stations)-1, i+2)]
        def squared(s):
            x, y = self.point(s)
            return (x-point[0])**2+(y-point[1])**2

        # Solve the perpendicular-foot condition rather than minimizing squared
        # distance: a nonzero lateral offset makes that minimum numerically flat.
        for _ in range(70):
            if high-low < 1e-12:
                break
            mid = (low+high)/2
            x, y = self.point(mid)
            tangent = unit(self.yaw(mid))
            derivative = (x-point[0])*tangent[0]+(y-point[1])*tangent[1]
            if derivative > 0:
                high = mid
            else:
                low = mid
        return min((0., self.curve.length, (low+high)/2), key=squared)


@dataclass
class Instance:
    path: str
    value: dict
    section: str | None
    owner: object = None
    slot: str = ""
    kind: str = ""
    start: float | None = None
    end: float | None = None
    shape: object = None
    position: tuple | None = None
    yaw: float = 0
    placed_section: str | None = None
    station: float | None = None
    route: list | None = None
    parked: bool = False

    def __post_init__(self):
        self.kind = self.kind or self.value.get("type", "")


@dataclass
class Leg:
    section: str
    start: float
    end: float
    length: float
    line: object
    incoming: str | None = None
    outgoing: str | None = None


class GeometryModel:
    """@brief Reusable planar sections, object placements and directed path queries."""

    def __init__(self, expansion, bundle, reporter):
        self.expansion, self.bundle, self.reporter = expansion, bundle, reporter
        self.track = expansion.track
        settings = bundle.catalog("global/$defs/validation").value["properties"]
        self.tolerance = number(settings["position_tolerance"]["default"])
        self.angle_tolerance = math.radians(number(settings["angle_tolerance"]["default"]))
        self.approximation = number(settings["approximation_tolerance"]["default"])
        if min(self.tolerance, self.angle_tolerance, self.approximation) <= 0:
            raise InputError("Geometry tolerances must be positive")
        self.sections, self.links, self.edge_paths = {}, {}, {}
        self.instances, self.by_path, self.surfaces = [], {}, []
        self.root_component = 0
        self.inconsistent_components = set()

    def build_sections(self):
        for identifier, value in objects(self.track.get("sections")):
            path = child("/sections", identifier)
            try:
                params, kind = value.get("parameters", {}), value["type"]
                if kind == "intersection":
                    d, a = number(params["port_distance"]), math.radians(number(params["angle"]))
                    if d <= 0 or not 0 < a < math.pi:
                        raise ValueError("Intersection needs positive port distance and angle between 0 and 180 degrees")
                    curve = Curve("straight", 2*d)
                    ports = {"entry": (0, 0, math.pi)}
                    all_ports = {"straight": (2*d, 0, 0), "right": (d+d*math.cos(a), -d*math.sin(a), -a),
                                 "left": (d-d*math.cos(a), d*math.sin(a), math.pi-a)}
                    ports.update({p: all_ports[p] for p in params["ports"]})
                else:
                    if kind == "arc":
                        radius = number(params["radius"])
                        if radius <= 0:
                            raise ValueError("Arc radius must be positive")
                        k = (1 if params["direction"] == "left" else -1)/radius
                        curve = Curve("arc", radius*math.radians(number(params["angle"])), k, k)
                    elif kind == "clothoid":
                        curve = Curve(kind, number(params["length"]), number(params["start_curvature"]), number(params["end_curvature"]))
                    elif kind == "straight":
                        curve = Curve(kind, number(params["length"]))
                    else:
                        raise ValueError(f"Unsupported primitive {kind!r}")
                    if curve.length <= 0:
                        raise ValueError("Section length must be positive")
                    ports = {"entry": (0, 0, math.pi), "exit": (*curve.point(curve.length), curve.yaw(curve.length))}
                section = Section(identifier, value, curve, ports)
                if section.width <= 0 or section.marking <= 0:
                    raise ValueError("Lane and marking widths must be positive")
                self.sections[identifier] = section
            except (ValueError, KeyError, TypeError) as exc:
                self.reporter.block("primitives", path, str(exc))

        for i, pair in enumerate(self.track.get("connections", [])):
            path = child("/connections", i)
            try:
                a, b = (tuple(p.split(".")) for p in pair)
                for sid, port in (a, b):
                    if sid not in self.sections or port not in self.sections[sid].ports:
                        raise ValueError("An endpoint primitive or port is unavailable")
                if a in self.links or b in self.links:
                    raise ValueError("A port has more than one connection")
                self.links[a], self.links[b] = (*b, 1), (*a, -1)
                self.edge_paths[a] = self.edge_paths[b] = path
            except (ValueError, TypeError, AttributeError):
                self.reporter.block("connections", path, "Connection has unusable endpoints")

    def assemble(self):
        root = self.track["root"]
        if root["section"] not in self.sections or root["port"] not in self.sections[root["section"]].ports:
            self.reporter.block("connections", "/root", "Root primitive/port is unavailable")
            self.root_component = -1
        starts = [root["section"]] if self.root_component == 0 else []
        starts += sorted(set(self.sections)-set(starts))
        component = 0
        for sid in starts:
            section = self.sections[sid]
            if section.component >= 0:
                continue
            section.component = component
            if sid == root["section"] and self.root_component == 0:
                x, y, yaw = section.ports[root["port"]]
                rotation = (math.pi if root["port"] == "entry" else 0) - yaw
                px, py = transform((x, y), (0, 0, rotation))
                section.pose = (-px, -py, rotation)
            queue = deque([sid])
            while queue:
                first = self.sections[queue.popleft()]
                for port in first.ports:
                    connection = self.links.get((first.identifier, port))
                    if not connection:
                        continue
                    other_id, other_port, _ = connection
                    other = self.sections[other_id]
                    p = first.port(port)
                    if other.component < 0:
                        local = other.ports[other_port]
                        rotation = p[2]+math.pi-local[2]
                        px, py = transform(local[:2], (0, 0, rotation))
                        other.pose = (p[0]-px, p[1]-py, angle(rotation))
                        other.component = component
                        queue.append(other_id)
                    q = other.port(other_port)
                    error, rotation_error = math.dist(p[:2], q[:2]), abs(angle(p[2]-q[2]-math.pi))
                    # Report each physical connection once, in its authored order.
                    if connection[2] == 1 and (error > self.tolerance or rotation_error > self.angle_tolerance):
                        self.inconsistent_components.add(component)
                        self.reporter.issue("connections", "closure", self.edge_paths[(first.identifier, port)],
                            "Connected ports do not agree in position/direction.", distance=error,
                            angle_degrees=math.degrees(rotation_error), tolerance=self.tolerance,
                            angle_tolerance_degrees=math.degrees(self.angle_tolerance), points=[p[:2], q[:2]])
                    if connection[2] == 1 and not first.intersection and not other.intersection:
                        delta = max(abs(first.half-other.half), abs(first.inner-other.inner))
                        if delta > self.tolerance:
                            self.reporter.issue("connections", "width_step", self.edge_paths[(first.identifier, port)],
                                "Outer road boundaries have a discontinuity.", lateral_step=delta,
                                sections=[first.identifier, other_id], tolerance=self.tolerance)
            component += 1
        if component > 1:
            self.reporter.block("overlaps", "/sections", "Relative positions of disconnected components are undefined")

    def arm(self, section, port):
        if port in section.arms:
            return section.arms[port]
        d = section.value["parameters"]["port_distance"]
        p = section.ports[port]
        u = ((p[0]-d)/d, p[1]/d)
        other = self.links.get((section.identifier, port))
        road = self.sections[other[0]] if other and not self.sections[other[0]].intersection else section
        result = {"u": u, "n": (-u[1], u[0]), "half": road.half,
                  "inner": road.inner, "boundary": road.boundary, "road": road,
                  "angle": math.atan2(u[1], u[0]) % math.tau}
        section.arms[port] = result
        return result

    def crossing_station(self, section, port):
        """Intersection interior begins where this axis enters the crossing road envelope."""
        a = self.arm(section, port)
        widths = [self.arm(section, p)["half"] / abs(cross(a["u"], self.arm(section, p)["u"]))
                  for p in section.ports if abs(cross(a["u"], self.arm(section, p)["u"])) > 1e-9]
        return max(widths, default=0)

    def intersection_polygon(self, section, tolerance=None):
        d = number(section.value["parameters"]["port_distance"])
        radius = number(section.value["parameters"]["corner_radius"])
        if radius < 0:
            raise ValueError("Corner radius must be non-negative")
        arms = sorted((self.arm(section, p) for p in section.ports), key=lambda a: a["angle"])
        points = []
        for a, b in zip(arms, arms[1:]+arms[:1]):
            delta = (b["angle"]-a["angle"]) % math.tau
            port = add((d, 0), scale(a["u"], d))
            points += [add(port, scale(a["n"], -a["half"])), add(port, scale(a["n"], a["half"]))]
            first = add((d, 0), scale(a["n"], a["half"]))
            second = add((d, 0), scale(b["n"], -b["half"]))
            if abs(delta-math.pi) < 1e-9:
                points += [first, second]
                continue
            if not 0 < delta < math.pi:
                raise ValueError("Adjacent intersection arms leave a gap greater than 180 degrees")
            vector = (second[0]-first[0], second[1]-first[1])
            vertex = add(first, scale(a["u"], cross(vector, b["u"])/cross(a["u"], b["u"])))
            r = max(0, radius-(a["road"].marking+b["road"].marking)/4)
            advance = r/math.tan(delta/2)
            start, end = add(vertex, scale(a["u"], advance)), add(vertex, scale(b["u"], advance))
            if max((start[0]-d)*a["u"][0]+start[1]*a["u"][1],
                   (end[0]-d)*b["u"][0]+end[1]*b["u"][1]) >= d:
                raise ValueError("port_distance is too short for road widths and rounded corners")
            points += self.circle_arc(add(start, scale(a["n"], r)), start, -(math.pi-delta), r, tolerance) if r else [vertex]
        return Polygon([transform(p, section.pose) for p in points])

    def circle_arc(self, center, start, sweep, radius, tolerance=None):
        tolerance = self.approximation if tolerance is None else tolerance
        count = max(1, math.ceil(abs(sweep)/max(1e-8, 2*math.acos(max(-1, 1-tolerance/radius)))))
        if count > 50000:
            raise ValueError("Arc exceeds the sampling workload")
        a = math.atan2(start[1]-center[1], start[0]-center[0])
        return [add(center, scale(unit(a+sweep*i/count), radius)) for i in range(count+1)]

    def road_shape(self, section, tolerance=None):
        """Compute a road envelope at the requested chord error bound."""
        if section.intersection:
            return self.intersection_polygon(section, tolerance)
        tolerance = self.approximation if tolerance is None else tolerance
        curve = section.curve
        if max(abs(curve.k0), abs(curve.k1))*section.half >= 1:
            raise ValueError("Road width reaches an offset-curve cusp or reverses the inner boundary")
        stations = curve.stations(tolerance, section.half)
        left = [section.point(s, section.half) for s in stations]
        right = [section.point(s, -section.half) for s in stations]
        if curve.kind == "arc" and abs(abs(curve.yaw(curve.length))-math.tau) < 1e-10:
            # A full circle is an annulus; a seam joining two rings would create
            # a self-touching polygon instead of a valid interior hole.
            outside, inside = (right, left) if curve.k0 > 0 else (left, right)
            return Polygon(outside[:-1], [inside[:-1]])
        return Polygon(left + right[::-1])

    def build_surfaces(self):
        for section in self.sections.values():
            try:
                if section.intersection:
                    shape = self.road_shape(section)
                    section.reference = LineString([section.point(0), section.point(section.curve.length)])
                else:
                    curve = section.curve
                    stations = curve.stations(self.approximation, section.half)
                    section.stations = stations
                    section.reference = LineString([section.point(s) for s in stations])
                    section.chord_lengths = [0.]
                    coords = list(section.reference.coords)
                    for a, b in zip(coords, coords[1:]):
                        section.chord_lengths.append(section.chord_lengths[-1]+math.dist(a, b))
                    shape = self.road_shape(section)
                    if not section.reference.is_simple:
                        raise ValueError("Section reference line crosses itself")
                if not shape.is_valid or shape.is_empty or shape.area <= 0:
                    raise ValueError("Road envelope is self-intersecting or degenerate")
                section.surface = shape
                self.surfaces.append((section.path, section.identifier, shape, False))
            except (ValueError, KeyError, TypeError) as exc:
                self.reporter.issue("surfaces", "invalid_surface", section.path, str(exc))

    def primary(self, section, incoming, discipline=False):
        if not section.intersection:
            return "exit" if incoming == "entry" else "entry"
        turn = section.value["parameters"].get("mandatory_turn", [])
        if not (discipline and self.track.get("discipline") == "freedrive_course") and incoming in turn:
            return turn[1] if incoming == turn[0] else turn[0]
        outgoing = OPPOSITE[incoming]
        if outgoing not in section.ports:
            raise PathError(f"{section.identifier}.{incoming} has no primary exit")
        return outgoing

    def next_state(self, section, outgoing, mode):
        link = self.links.get((section.identifier, outgoing))
        if link is None:
            raise PathError(f"Unconnected port {section.identifier}.{outgoing}")
        if link[2] != mode:
            raise PathError(f"{section.identifier}.{outgoing} opposes the requested nominal graph direction")
        return link[:2]

    def transitions(self, state, mode):
        section, incoming = self.sections[state[0]], state[1]
        candidates = section.ports if section.intersection else [self.primary(section, incoming)]
        for port in candidates:
            if port == incoming:
                continue
            try:
                yield port, self.next_state(section, port, mode)
            except PathError:
                continue

    def via_exit(self, state, target, mode):
        """Find a unique feeder from the next waypoint back to the nominal path."""
        nominal, seen, current = [], set(), state
        while current not in seen:
            seen.add(current)
            if current[0] == target:
                return self.primary(self.sections[state[0]], state[1])
            try:
                outgoing = self.primary(self.sections[current[0]], current[1])
                following = self.next_state(self.sections[current[0]], outgoing, mode)
            except PathError:
                nominal.append((current, None))
                break
            nominal.append((current, outgoing))
            current = following

        predecessors = defaultdict(list)
        for sid, section in self.sections.items():
            for port in section.ports:
                for outgoing, following in self.transitions((sid, port), mode):
                    predecessors[following].append(((sid, port), outgoing))
        nominal_indices = {s: i for i, (s, _) in enumerate(nominal)}
        stack = [((target, p), [], frozenset()) for p in self.sections[target].ports]
        candidates, iterations = [], 0
        while stack:
            current, suffix, visited = stack.pop()
            iterations += 1
            if iterations > 10000:
                raise PathError(f"Via search for {target!r} exceeds 10000 states")
            if current in nominal_indices and suffix:
                i = nominal_indices[current]
                path = nominal[:i] + suffix
                if path not in candidates:
                    candidates.append(path)
                if len(candidates) > 1:
                    raise PathError(f"Via {target!r} has multiple feeder paths to the nominal route")
                continue
            if current in visited:
                continue
            for previous, outgoing in predecessors[current]:
                stack.append((previous, [(previous, outgoing)]+suffix, visited | {current}))
        if len(candidates) != 1 or not candidates[0]:
            raise PathError(f"Via {target!r} is unreachable from the nominal route")
        return candidates[0][0][1]

    def junction_leg(self, section, incoming, outgoing):
        first, second = self.arm(section, incoming), self.arm(section, outgoing)
        d = section.value["parameters"]["port_distance"]
        if OPPOSITE[incoming] == outgoing:
            points = [section.port(incoming)[:2], section.port(outgoing)[:2]]
            return Leg(section.identifier, 0, 2*d, 2*d, LineString(points), incoming, outgoing)
        a, b = first, second
        reverse = False
        delta = (b["angle"]-a["angle"]) % math.tau
        if delta > math.pi:
            a, b, delta, reverse = b, a, math.tau-delta, True
        radius = section.value["parameters"]["corner_radius"]+(a["boundary"]+b["boundary"])/2
        advance = radius/math.tan(delta/2)
        if advance >= d:
            raise PathError(f"{section.identifier}: port_distance is too short for its tangent turn")
        start, end = add((d, 0), scale(a["u"], advance)), add((d, 0), scale(b["u"], advance))
        center = add(start, scale(a["n"], radius))
        arc = self.circle_arc(center, start, -(math.pi-delta), radius)
        local = [add((d, 0), scale(a["u"], d)), *arc, end, add((d, 0), scale(b["u"], d))]
        points = [transform(p, section.pose) for p in local]
        if reverse:
            points.reverse()
        length = 2*(d-advance)+radius*(math.pi-delta)
        return Leg(section.identifier, 0, length, length, LineString(points), incoming, outgoing)

    def linear_leg(self, section, start, end):
        stations = section.curve.stations(self.approximation, start=start, end=end)
        points = [section.point(s) for s in stations]
        return Leg(section.identifier, start, end, abs(end-start), LineString(points))

    def endpoint(self, value):
        section = self.sections[value["section"]]
        if section.intersection:
            return section, value["port"]
        s = number(value["distance"]) if "distance" in value else (0 if value["port"] == "entry" else section.curve.length)
        if not -self.tolerance <= s <= section.curve.length+self.tolerance:
            raise PathError(f"Position {s:g} exceeds {section.identifier}'s length {section.curve.length:g}")
        return section, min(section.curve.length, max(0, s))

    def resolve_span(self, span, direction="forward", discipline=False):
        """@brief Resolve a span into ordered visits, preserving repeated sections."""
        mode = 1 if direction == "forward" else -1
        section, current = self.endpoint(span["start"])
        target, end = self.endpoint(span["end"])
        vias = span.get("via", [])
        incoming = current if section.intersection else ("entry" if mode == 1 else "exit")
        index, distance, legs, seen = 0, 0.0, [], set()
        initial = True
        while True:
            state = (section.identifier, incoming)
            if not initial and index < len(vias) and section.identifier == vias[index]:
                index += 1
            token = (state, index)
            if section.identifier == target.identifier and index == len(vias):
                if section.intersection and incoming == end and distance > self.tolerance:
                    return legs
                if not section.intersection:
                    forward = incoming == "entry"
                    if (end-current)*(1 if forward else -1) >= -1e-10 and (distance > self.tolerance or abs(end-current) > self.tolerance):
                        if abs(end-current) > 1e-12:
                            legs.append(self.linear_leg(section, current, end))
                        return legs
            if token in seen:
                raise PathError("Path loops before reaching its end and all via visits")
            seen.add(token)
            initial = False
            if section.intersection:
                outgoing = self.via_exit(state, vias[index], mode) if index < len(vias) else self.primary(section, incoming, discipline)
                leg = self.junction_leg(section, incoming, outgoing)
                if section.identifier == target.identifier and outgoing == end and index == len(vias):
                    legs.append(leg)
                    return legs
            else:
                outgoing = "exit" if incoming == "entry" else "entry"
                finish = section.curve.length if outgoing == "exit" else 0
                leg = self.linear_leg(section, current, finish)
            if leg.length > 1e-12:
                legs.append(leg)
                distance += leg.length
            sid, incoming = self.next_state(section, outgoing, mode)
            section = self.sections[sid]
            current = incoming if section.intersection else (0 if incoming == "entry" else section.curve.length)

    def locate(self, section, station):
        """Carry a longitudinal placement across nominally connected sections."""
        seen, orientation = set(), 1
        while station < 0 or station > section.curve.length:
            if section.intersection:
                raise PathError("A linear position cannot use intersection station coordinates")
            port, travel = ("entry", -1) if station < 0 else ("exit", 1)
            along_travel = orientation*travel
            remaining = -station if travel == -1 else station-section.curve.length
            link = self.links.get((section.identifier, port))
            if link is None:
                raise PathError(f"Placement extends past open port {section.identifier}.{port}")
            sid, incoming = link[:2]
            if (sid, incoming, round(remaining, 10)) in seen:
                raise PathError("Cyclic placement could not be resolved")
            seen.add((sid, incoming, round(remaining, 10)))
            section = self.sections[sid]
            if section.intersection:
                outgoing = self.primary(section, incoming)
                leg = self.junction_leg(section, incoming, outgoing)
                if remaining <= leg.length:
                    point = leg.line.interpolate(remaining/leg.length, normalized=True)
                    eps = min(0.001, leg.line.length/100)
                    s = remaining*leg.line.length/leg.length
                    a, b = leg.line.interpolate(max(0, s-eps)), leg.line.interpolate(min(leg.line.length, s+eps))
                    yaw = math.atan2(b.y-a.y, b.x-a.x)+(math.pi if along_travel < 0 else 0)
                    return section, None, (point.x, point.y), yaw
                remaining -= leg.length
                sid, incoming = self.next_state(section, outgoing, link[2])
                section = self.sections[sid]
                if section.intersection:
                    raise PathError("Placement across consecutive intersections needs an explicit pose")
            orientation = along_travel*(1 if incoming == "entry" else -1)
            station = remaining if incoming == "entry" else section.curve.length-remaining
        return section, station, section.point(station), section.yaw(station)+(math.pi if orientation < 0 else 0)

    def register(self, value, path, section, owner=None, slot="", kind=""):
        instance = Instance(path, value, section, owner, slot, kind)
        self.instances.append(instance)
        self.by_path[path] = instance
        return instance

    def index_objects(self):
        def features(parent, value, path, section):
            for key, obj in objects(value.get("features")):
                self.register(obj, child(path+"/features", key), section, parent, key)

        def zones(parent, value, path, section):
            for key, obj in objects(value.get("zones")):
                instance = self.register(obj, child(path+"/zones", key), section, parent, key)
                features(instance, obj, instance.path, section)

        features(None, self.track, "", None)
        zones(None, self.track, "", None)
        for sid, section in self.sections.items():
            parent = self.register(section.value, section.path, sid, kind="section")
            features(parent, section.value, section.path, sid)
            zones(parent, section.value, section.path, sid)
            for key, obj in objects(section.value.get("elements")):
                element = self.register(obj, child(section.path+"/elements", key), sid, parent, key)
                features(element, obj, element.path, sid)
                if element.kind == "parking_area":
                    for lot_id in ("parallel_parking", "perpendicular_parking"):
                        lot = obj.get("parameters", {}).get(lot_id)
                        if not isinstance(lot, dict):
                            continue
                        lot_instance = self.register(lot, element.path+"/parameters/"+lot_id, sid, element, lot_id, "parking_lot")
                        features(lot_instance, lot, lot_instance.path, sid)
                        for spot_id, spot in objects(lot.get("spots")):
                            instance = self.register(spot, child(lot_instance.path+"/spots", spot_id), sid, lot_instance, spot_id, "parking_spot")
                            features(instance, spot, instance.path, sid)

    def intervals(self):
        for obj in self.instances:
            try:
                p = obj.value.get("parameters", {})
                section = self.sections.get(obj.section)
                if obj.kind == "parking_lot":
                    obj.start = obj.owner.start+number(obj.value["distance"])
                    obj.end = obj.start+number(obj.value["extent_along_road"])
                elif obj.kind == "parking_spot":
                    obj.start = obj.owner.start+number(obj.value["distance"])
                    obj.end = obj.start+number(obj.value["extent_along_road"])
                elif obj.kind in {"barred_area", "side_road_junction", "parking_area",
                                  "centerline", "missing_marking", "speed_limit", "no_passing", "expressway"} and "range" in p:
                    obj.start, obj.end = number(p["range"]["start"]), number(p["range"]["end"])
                elif obj.kind == "crosswalk":
                    first = number(p["distance"])
                    length = number(obj.value["rendering"]["stripe_length"])
                    obj.start = first if p["direction"] == "forward" else first-length
                    obj.end = obj.start+length
                elif obj.kind in {"start_box", "starting_line"}:
                    root = self.track["root"]
                    origin = section.curve.length if root["port"] == "exit" else 0
                    if obj.kind == "start_box":
                        # The bay belongs to the arc exit even when an explicit
                        # root.entry selects a different Map origin/start line.
                        obj.end = section.curve.length
                        obj.start = obj.end-number(p["length"])
                    else:
                        obj.start = number(p.get("distance", origin))
                        obj.end = obj.start+number(obj.value["rendering"]["width"])
                if obj.start is None:
                    continue
                if obj.end <= obj.start:
                    raise ValueError("Occupied interval must have positive length")
                is_root_line = obj.kind == "starting_line" and obj.slot == "starting_line" and obj.section == self.track["root"]["section"]
                if not is_root_line and (obj.start < -self.tolerance or obj.end > section.curve.length+self.tolerance):
                    raise ValueError(f"Interval [{obj.start:g}, {obj.end:g}] exceeds section length {section.curve.length:g}")
                if obj.kind == "start_box":
                    # Like the renderer, the straight starting bay is tangent
                    # to the root exit. Its union with the road encloses the
                    # clipped join to the outer arc boundary.
                    yaw = section.yaw(obj.end)
                    length = number(p["length"])
                    center = add(section.point(obj.end),
                                 add(scale(unit(yaw), -length/2),
                                     scale(unit(yaw+math.pi/2), -(section.width+section.marking)/2)))
                    obj.shape = rectangle(center, length, section.width+2*section.marking, yaw)
                    self.surfaces.append((obj.path, obj.section, obj.shape, True))
                elif obj.kind in {"parking_lot", "parking_spot"}:
                    if obj.start < obj.owner.start-self.tolerance or obj.end > obj.owner.end+self.tolerance:
                        raise ValueError("Parking interval exceeds its owning area/lot")
                    lot = obj if obj.kind == "parking_lot" else obj.owner
                    sign = -1 if lot.slot == "parallel_parking" else 1
                    if lot.owner.value["parameters"]["direction"] == "backward":
                        sign *= -1
                    depth = number(obj.value["extent_lateral"])
                    if depth <= 0:
                        raise ValueError("Parking depth must be positive")
                    obj.position = section.point((obj.start+obj.end)/2, sign*(section.boundary+depth/2))
                    obj.yaw = section.yaw((obj.start+obj.end)/2)
                    obj.shape = rectangle(obj.position, obj.end-obj.start, depth, obj.yaw)
                    if obj.kind == "parking_lot":
                        self.surfaces.append((obj.path, obj.section, obj.shape, True))
                    elif not obj.owner.shape.buffer(self.tolerance).covers(obj.shape):
                        raise ValueError("Parking spot extends beyond its lot footprint")
                elif obj.kind == "crosswalk":
                    length = number(p["waiting_area_longitudinal_extent"])
                    depth = number(p["waiting_area_lateral_extent"])
                    if min(length, depth) <= 0:
                        raise ValueError("Waiting-area dimensions must be positive")
                    for side in p["waiting_area_sides"]:
                        sign = 1 if side == "left" else -1
                        center = section.point((obj.start+obj.end)/2, sign*(section.boundary+depth/2))
                        area = rectangle(center, length, depth, section.yaw(obj.start))
                        self.surfaces.append((obj.path+"/waiting/"+side, obj.section, area, True))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self.reporter.issue("placements", "extent", obj.path, str(exc))

    def resolve_zones(self):
        for obj in self.instances:
            if obj.kind not in {"centerline", "missing_marking", "speed_limit", "no_passing", "expressway", "grade"}:
                continue
            if obj.kind == "grade":
                continue
            try:
                p = obj.value["parameters"]
                if "span" in p:
                    obj.route = self.resolve_span(p["span"], p["direction"])
                elif obj.start is not None:
                    section = self.sections[obj.section]
                    a, b = (obj.start, obj.end) if p["direction"] == "forward" else (obj.end, obj.start)
                    obj.route = [self.linear_leg(section, a, b)]
            except (PathError, ValueError, KeyError, TypeError) as exc:
                self.reporter.issue("spans", "span_unresolved", obj.path, str(exc))

    def sign_panel_size(self, value):
        """@return Effective panel width and height for placement and rule measurements."""
        variants = self.bundle.catalog("features/$defs/traffic_sign").value["x-caudri"]["variants"]
        prefix = variants[value["parameters"]["sign"]]["size"]
        rendering = value["rendering"]
        width = number(rendering["round_diameter"] if prefix == "round" else rendering[prefix+"_width"])
        height = number(rendering["round_diameter"] if prefix == "round" else rendering[prefix+"_height"])
        if min(width, height) <= 0:
            raise ValueError("Sign panel dimensions must be positive")
        return width, height

    def sign_default(self, obj):
        owner, p = obj.owner, obj.value.get("parameters", {})
        if owner is None:
            raise PathError("An additional sign needs at or pose")
        if owner.kind != "section":
            category = "elements" if owner.kind in {"crosswalk", "barred_area", "parking_area"} else "zones"
            try:
                recipes = self.bundle.catalog(category+"/$defs/"+owner.kind).value["x-caudri"]["expansion"]
                if obj.slot not in recipes or recipes[obj.slot].get("type") != "traffic_sign":
                    raise KeyError(obj.slot)
            except KeyError as exc:
                raise PathError("An additional sign needs at or pose") from exc
        section = self.sections.get(owner.section)
        params = owner.value.get("parameters", {})
        direction = 1 if params.get("direction", "forward") == "forward" else -1
        # The parameter specifies free edge-to-edge clearance, whereas the
        # resolved pose locates the center of the physical sign panel.
        panel_width, _ = self.sign_panel_size(obj.value)
        lateral = number(p["lateral_offset"])+panel_width/2
        if owner.kind == "section" and section.intersection:
            port, role = obj.slot.split("_", 1)
            arm = self.arm(section, port)
            station = self.crossing_station(section, port)
            advance = params["mandatory_sign_distance"] if role == "mandatory_sign" else params["priority_sign_distance"]
            anchor = add((params["port_distance"], 0), scale(arm["u"], station))
            local = add(add(anchor, scale(arm["u"], advance)), scale(arm["n"], arm["half"]+lateral))
            return section, None, transform(local, section.pose), angle(section.pose[2]+arm["angle"]+math.pi)
        if owner.kind == "crosswalk":
            direction = 1 if obj.slot == "sign_forward" else -1
            station = owner.start-params["sign_distance"] if direction == 1 else owner.end+params["sign_distance"]
        elif owner.kind in {"barred_area", "parking_area"}:
            advance = params["priority_sign_distance"] if owner.kind == "barred_area" else params["announcement_sign_distance"]
            station = owner.start-advance if direction == 1 else owner.end+advance
        elif owner.kind in {"speed_limit", "no_passing", "expressway"}:
            if not owner.route:
                raise PathError("Zone path is unavailable for its endpoint sign")
            leg = owner.route[0] if obj.slot.startswith("begin") else owner.route[-1]
            section = self.sections[leg.section]
            station = leg.start if obj.slot.startswith("begin") else leg.end
            if section.intersection:
                port = leg.incoming if obj.slot.startswith("begin") else leg.outgoing
                arm = self.arm(section, port)
                inbound = obj.slot.startswith("begin")
                yaw = section.pose[2]+arm["angle"]+(math.pi if inbound else 0)
                center = section.port(port)[:2]
                return section, None, add(center, scale(unit(yaw-math.pi/2), arm["half"]+lateral)), yaw
            direction = 1 if leg.end > leg.start else -1
        else:
            raise PathError("No geometric placement binding for this sign slot")
        placed, s, center, yaw = self.locate(section, station)
        yaw += math.pi if direction == -1 else 0
        return placed, s, add(center, scale(unit(yaw-math.pi/2), placed.half+lateral)), yaw

    def place_features(self):
        for obj in self.instances:
            if obj.kind not in {"traffic_sign", "static_obstacle", "artifact", "landmark"}:
                continue
            if obj.owner and obj.owner.kind == "grade":
                continue
            try:
                value, section = obj.value, self.sections.get(obj.section)
                if "pose" in value:
                    pose = value["pose"]
                    point, yaw = (number(pose["x"]), number(pose["y"])), math.radians(number(pose["yaw"]))
                    if section and section.component != self.root_component:
                        raise PathError("Map pose cannot be compared with an unanchored component")
                    placed, station = section, None
                elif "at" in value:
                    at = value["at"]
                    section = self.sections[at.get("section", obj.section)]
                    if section.intersection:
                        point = transform((number(at["x"]), number(at["y"])), section.pose)
                        placed, station, yaw = section, None, section.pose[2]
                    else:
                        placed, station, point, yaw = self.locate(section, number(at["distance"]))
                        point = add(point, scale(unit(yaw+math.pi/2), number(at.get("offset", 0))))
                    yaw += math.radians(number(at.get("yaw", 0)))
                elif obj.kind == "static_obstacle" and obj.owner and obj.owner.kind == "parking_spot":
                    spot, lot = obj.owner, obj.owner.owner
                    point, yaw = spot.position, spot.yaw
                    if lot.slot == "perpendicular_parking":
                        yaw += math.pi/2
                    placed, station = section, (spot.start+spot.end)/2
                    obj.parked = True
                elif obj.kind == "traffic_sign":
                    placed, station, point, yaw = self.sign_default(obj)
                else:
                    raise PathError("Object placement is unresolved")
                if point is None:
                    raise PathError("Owning parking space has no resolved footprint")
                if obj.kind == "traffic_sign":
                    faces = value["parameters"]["faces"]
                    if "at" in value or "pose" in value:
                        yaw += math.pi if faces == "backward" else 0
                    else:
                        owner = obj.owner
                        expected = ("backward" if obj.slot == "sign_backward" else "forward")
                        if owner.kind in {"speed_limit", "no_passing", "expressway"}:
                            expected = owner.value["parameters"]["direction"]
                        yaw += math.pi if faces != expected else 0
                obj.position, obj.yaw = point, yaw
                obj.placed_section, obj.station = placed.identifier if placed else None, station
                if obj.owner and obj.owner.kind in {"parking_spot", "parking_lot"}:
                    obj.parked = obj.kind == "static_obstacle"
                if obj.kind in {"static_obstacle", "artifact"} and "size" in value:
                    size = value["size"]
                    length, width = number(size["length"]), number(size["width"])
                    if min(length, width) <= 0:
                        raise ValueError("Object extents must be positive")
                    obj.shape = rectangle(point, length, width, yaw)
                if obj.parked and obj.shape is not None and obj.owner.shape is not None:
                    if not obj.owner.shape.buffer(self.tolerance).covers(obj.shape):
                        self.reporter.issue("placements", "parking_obstacle_outside", obj.path,
                            "Parked obstacle extends outside its owning parking space.", geometry=mapping(obj.shape))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self.reporter.block("placements", obj.path, str(exc))

        roads = [(s, s.surface) for s in self.sections.values() if s.surface is not None]
        for obj in self.instances:
            if obj.kind != "traffic_sign" or obj.position is None:
                continue
            owner = self.sections.get(obj.placed_section)
            component = owner.component if owner else self.root_component
            if component in self.inconsistent_components:
                self.reporter.block("placements", obj.path, "Road placement depends on an inconsistent loop closure")
                continue
            for section, surface in roads:
                if section.component == component and surface.buffer(-self.tolerance).covers(Point(obj.position)):
                    self.reporter.issue("placements", "sign_on_road", obj.path,
                        "Traffic sign is placed inside a road envelope.", section=section.identifier,
                        point=obj.position, related_paths=[section.path])

    def overlap_checks(self):
        if "overlaps" in self.reporter.skip or not self.surfaces:
            return
        shapes = [entry[2] for entry in self.surfaces]
        tree = STRtree(shapes)
        for i, (path, sid, shape, attached) in enumerate(self.surfaces):
            for j in tree.query(shape.buffer(2*self.approximation)):
                j = int(j)
                if j <= i:
                    continue
                other_path, other_id, other, other_attached = self.surfaces[j]
                if self.sections[sid].component in self.inconsistent_components:
                    self.reporter.block("overlaps", path, "Relative road placement depends on an inconsistent loop closure")
                    continue
                if self.sections[sid].component != self.sections[other_id].component:
                    continue
                if sid == other_id and attached != other_attached:
                    continue
                # A declared joint permits numerical overlap only at that joint.
                seams = []
                for port in self.sections[sid].ports:
                    link = self.links.get((sid, port))
                    if link and link[0] == other_id:
                        p = self.sections[sid].port(port)
                        q = self.sections[other_id].port(link[1])
                        half = max(self.sections[sid].half, self.sections[other_id].half)
                        seam = LineString([add(p[:2], scale(unit(p[2]+math.pi/2), half)),
                                           add(p[:2], scale(unit(p[2]+math.pi/2), -half))])
                        if math.dist(p[:2], q[:2]) <= self.tolerance:
                            seams.append(seam)
                first_section, second_section = self.sections[sid], self.sections[other_id]
                curved = [not att and (s.intersection or s.curve.k0 != 0 or s.curve.k1 != 0)
                          for s, att in ((first_section, attached), (second_section, other_attached))]
                first_shape, second_shape = shape, other
                for refinement in (1, 100):
                    tolerance = self.approximation/refinement
                    bound = tolerance*sum(curved)
                    if refinement > 1:
                        try:
                            first_shape = self.road_shape(first_section, tolerance) if curved[0] else shape
                            second_shape = self.road_shape(second_section, tolerance) if curved[1] else other
                        except ValueError as exc:
                            self.reporter.block("overlaps", path+"|"+other_path, str(exc))
                            break
                    intersection = first_shape.intersection(second_shape)
                    for seam in seams:
                        intersection = intersection.difference(seam.buffer(self.tolerance+bound))
                    if intersection.area <= 1e-12:
                        if seams or not bound or first_shape.distance(second_shape) > bound:
                            break
                    elif not bound or not intersection.buffer(-bound).is_empty:
                        self.reporter.issue("overlaps", "road_overlap", path,
                            "Occupied road/element areas overlap outside an allowed attachment.",
                            related_paths=[other_path], area=intersection.area, geometry=mapping(intersection),
                            approximation_bound=bound)
                        break
                else:
                    self.reporter.block("overlaps", path+"|"+other_path,
                                        "Road contact remains within the refined approximation bound")


def validate_geometry(track, *, bundle=None, source_uri=None, skip=(), check_schema=False):
    """@brief Expand and check planar geometry, retaining a reusable GeometryModel.

    @param track Authored YAML/JSON document, dictionary or ExpansionResult.
    @param skip Geometry check groups to omit from the report's executed checks.
    @return GeometryResult with diagnostics, prerequisites and calculated model.
    """
    expansion, bundle, schema = prepare(track, bundle, source_uri, check_schema)
    result = GeometryResult(expansion=expansion, schema_result=schema,
                            diagnostics=list(schema.diagnostics),
                            prerequisites=[{"stage": "schema", "valid": schema.valid}])
    report = Reporter(result, expansion, CHECKS, skip)
    if not expansion.complete:
        for group in CHECKS:
            report.block(group, "", "Expansion is incomplete")
        return report.finish()
    model = result.model = GeometryModel(expansion, bundle, report)
    model.build_sections()
    model.assemble()
    model.build_surfaces()
    model.index_objects()
    model.intervals()
    model.resolve_zones()
    model.place_features()
    model.overlap_checks()
    return report.finish()


def run_cli(stage, function, checks, argv=None):
    """Shared CLI formatting and report output for the geometry/rule entry points."""
    parser = argparse.ArgumentParser(description=f"Validate CAuDri track {stage}.")
    parser.add_argument("track", type=Path)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--skip", choices=list(checks), action="append", default=[])
    parser.add_argument("--check-schema", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() == args.track.resolve():
        parser.error("Input and report must use different files")
    try:
        result = function(load_track(args.track), skip=args.skip, check_schema=args.check_schema)
        code = 1 if result.valid is False else 0
    except (InputError, OSError, ValueError) as exc:
        result = StageResult(stage, diagnostics=[Diagnostic(f"{stage}.input", "error", "", str(exc))],
                             checks=[CheckResult(c, "blocked", "Input or configuration could not be loaded.") for c in checks])
        code = 2
    output = json.dumps(result.report(), indent=2, ensure_ascii=False, allow_nan=False)+"\n"
    try:
        if args.report:
            write_file(args.report, output)
        if args.format == "json":
            sys.stdout.write(output)
        stream = sys.stderr if args.format == "json" else sys.stdout
        for finding in result.diagnostics:
            print(finding.format(args.verbose), file=stream)
            if args.verbose:
                details = {k: v for k, v in finding.details.items() if k not in {"check", "geometry"}}
                if details:
                    print("  " + json.dumps(details, ensure_ascii=False, allow_nan=False), file=stream)
        if args.verbose:
            for check in result.checks:
                print(f"{check.status.upper()} {check.code}: {check.message}", file=stream)
                for path, reason in check.details.get("blocked_paths", {}).items():
                    print(f"  {path or '/'}: {reason}", file=stream)
        state = {True: "passed", False: "failed", None: "partially checked"}[result.valid]
        errors = sum(d.severity == "error" for d in result.diagnostics)
        warnings = sum(d.severity == "warning" for d in result.diagnostics)
        print(f"{stage.capitalize()} validation {state}: {errors} error(s), {warnings} warning(s).", file=stream)
    except (OSError, ValueError) as exc:
        print(f"ERROR {stage}.output: {exc}", file=sys.stderr)
        return 2
    return code


def main(argv=None):
    return run_cli("geometry", validate_geometry, CHECKS, argv)


if __name__ == "__main__":
    raise SystemExit(main())
