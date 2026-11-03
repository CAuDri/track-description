#!/usr/bin/env python3
"""This validation step checks the syntax and structural integrity of a CAuDri track description, including the presence and placement of required fields, the consistency of references, and the ordering of local parameters.

A full list of all performed checks and usage instructions can be found in `docs/validation.md`.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

if __package__:
    from .expand_track import (
        Diagnostic, Document, ExpansionResult, InputError, Node, SchemaBundle,
        child, expand_track, load_track, pointer, prepare_track, source_location, write_file,
    )
else:
    from expand_track import (
        Diagnostic, Document, ExpansionResult, InputError, Node, SchemaBundle,
        child, expand_track, load_track, pointer, prepare_track, source_location, write_file,
    )

__all__ = ["CheckResult", "ValidationResult", "validate_schema"]


# Reports are independent of CLI wording so an editor can link findings to its
# objects. Check status and diagnostic severity have different meanings: a rule
# check can fail with warning severity while the track remains technically usable.
CHECKS = {
    "native": "Native JSON Schema constraints",
    "features": "Complete effective feature schemas",
    "references": "Section, port, actor and crosswalk references",
    "connections": "Connection endpoints, nominal direction and unique port use",
    "intervals": "Section-local interval ordering",
    "placements": "Explicit and generated placement bindings",
    "section_parameters": "Local section parameter relationships",
}


@dataclass
class CheckResult:
    """@brief Outcome of a named check group, including skipped/blocked work."""

    code: str
    status: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class ValidationResult:
    """@brief Structural findings and outcomes of the requested check groups."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    schema_valid: bool | None = None
    expansion_complete: bool = False

    @property
    def valid(self) -> bool | None:
        """@return False for errors, None for unperformed checks, True otherwise.

        Rule warnings can coexist with valid=True and schema_valid=False. The
        latter retains the strict native schema result rather than relaxing it.
        """
        if any(d.severity == "error" for d in self.diagnostics):
            return False
        # A group with warnings can also contain blocked work; include both in the result.
        if not self.expansion_complete or not self.checks or any(
            c.status in {"skipped", "blocked", "unsupported"} or c.details.get("blocked_paths")
            for c in self.checks
        ):
            return None
        return True

    def report(self) -> dict:
        """@return JSON-compatible results with stable codes and source pointers."""
        return {
            "stage": "schema", "valid": self.valid,
            "schema_valid": self.schema_valid,
            "expansion_complete": self.expansion_complete,
            "checks": [asdict(check) for check in self.checks],
            "diagnostics": [asdict(diagnostic) for diagnostic in self.diagnostics],
            "not_run": ["geometry", "regulations"],
        }


@dataclass
class _Object:
    value: dict
    path: str
    category: str
    section: str | None = None
    global_zone: bool = False


def _objects(value):
    """Iterate only actual object mappings; malformed containers are schema errors."""
    return value.items() if isinstance(value, dict) else ()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Validator:
    def __init__(self, expansion, bundle):
        self.expansion = expansion
        self.bundle = bundle
        self.track = expansion.track
        self.sections = self.track.get("sections", {})
        self.result = ValidationResult(expansion_complete=True)
        self.group = "native"
        self._group_findings = set()
        self._group_blocked = {}
        self._seen = {}
        self.instances = []
        self._index_objects()
        self.by_path = {obj.path: obj for obj in self.instances}

    def issue(self, code, path, message, *, severity="error", schema_ref=None, **details):
        provenance = self.expansion.provenance
        source_path = provenance.get(path, {}).get("source_path", path)
        # One authored override may be inherited by every section. Preserve the
        # affected pointers but avoid repeating its warning in a human report.
        identity = (code, schema_ref, source_path, message)
        self._group_findings.add(identity)
        if identity in self._seen:
            prior = self._seen[identity]
            groups = prior.details.setdefault("checks", [prior.details["check"]])
            if self.group not in groups:
                groups.append(self.group)
            related = prior.details.setdefault("related_paths", [])
            if path != prior.path and path not in related:
                related.append(path)
            return

        finding = Diagnostic(
            code, severity, path, message,
            source_location(self.expansion.source, path, provenance),
            schema_ref, {"check": self.group, **details},
        )
        self._seen[identity] = finding
        self.result.diagnostics.append(finding)

    def block(self, path, message):
        """Record a check blocked by a missing prerequisite."""
        self._group_blocked[path] = message

    def _index_objects(self):
        """Walk the typed object containers defined by the catalog."""
        def features(owner, path, section=None, global_zone=False):
            for key, value in _objects(owner.get("features")):
                if isinstance(value, dict):
                    self.instances.append(_Object(value, child(child(path, "features"), key),
                                                  "features", section, global_zone))

        def zones(owner, path, section=None):
            for key, value in _objects(owner.get("zones")):
                if isinstance(value, dict):
                    zone_path = child(child(path, "zones"), key)
                    self.instances.append(_Object(value, zone_path, "zones", section))
                    features(value, zone_path, section, global_zone=section is None)

        def parking_features(element, path, section):
            params = element.get("parameters", {})
            if element.get("type") != "parking_area" or not isinstance(params, dict):
                return
            for name in ("parallel_parking", "perpendicular_parking"):
                lot = params.get(name)
                if not isinstance(lot, dict):
                    continue
                lot_path = child(child(path, "parameters"), name)
                features(lot, lot_path, section)
                for spot_id, spot in _objects(lot.get("spots")):
                    if isinstance(spot, dict):
                        features(spot, child(child(lot_path, "spots"), spot_id), section)

        features(self.track, "")
        zones(self.track, "")

        for section_id, section in _objects(self.sections):
            if not isinstance(section, dict):
                continue
            section_path = child("/sections", section_id)
            self.instances.append(_Object(section, section_path, "sections", section_id))
            features(section, section_path, section_id)
            zones(section, section_path, section_id)

            for element_id, element in _objects(section.get("elements")):
                if not isinstance(element, dict):
                    continue
                path = child(child(section_path, "elements"), element_id)
                self.instances.append(_Object(element, path, "elements", section_id))
                features(element, path, section_id)
                parking_features(element, path, section_id)

        for key, value in _objects(self.track.get("actors")):
            if isinstance(value, dict):
                self.instances.append(_Object(value, child("/actors", key), "actors"))

    # Native schemas: recover useful leaf errors through references/compositions.

    def _error_node(self, node, schema_path, seen=frozenset()):
        """Follow a validation path whose $ref hops were omitted by jsonschema.

        A schema may have both a $ref and its own properties. Trying only the
        local properties can enter the wrong branch; backtrack to the reference
        if that path does not reach the reported constraint.
        """
        schema_path = tuple(schema_path)
        identity = (node.ref, schema_path)
        if identity in seen:
            return None
        seen = seen | {identity}
        if not schema_path:
            if isinstance(node.value, dict) and isinstance(node.value.get("$ref"), str):
                target = self.bundle.resolve(node.value["$ref"], node.uri)
                return self._error_node(target, (), seen)
            return node
        try:
            found = self._error_node(node.sub(schema_path[0]), schema_path[1:], seen)
            if found is not None:
                return found
        except (KeyError, IndexError, TypeError):
            pass
        if isinstance(node.value, dict) and "$ref" in node.value:
            target = self.bundle.resolve(node.value["$ref"], node.uri)
            return self._error_node(target, schema_path, seen)
        return None

    def _relevant_errors(self, error):
        # Select the object's type branch to report relevant oneOf errors.
        if error.context and isinstance(error.instance, dict) and "type" in error.instance:
            origin = self.bundle.origins.get(id(error.schema))
            if origin:
                parent = Node(error.schema, *origin)
                alternatives = error.schema.get(error.validator, [])
                for i in range(len(alternatives)):
                    branch = parent.sub(error.validator).sub(i)
                    constants = [n.value.get("properties", {}).get("type", {}).get("const")
                                 for n in self.bundle.active(branch, error.instance)]
                    if error.instance["type"] in constants:
                        for nested in error.context:
                            if nested.schema_path and nested.schema_path[0] == i:
                                yield from self._relevant_errors(nested)
                        return
        yield error

    @staticmethod
    def _error_targets(error, constraint, value):
        """Recover property pointers omitted by jsonschema for boolean schemas."""
        parts = list(error.absolute_path)
        if error.schema is False and isinstance(constraint.value, dict):
            owner = value
            for segment in parts:
                owner = owner[segment]
            if isinstance(owner, dict):
                # Several forbidden properties may have equal values. Report
                # each present property; the diagnostic collector deduplicates.
                matches = [(parts + [key], child(constraint.path, key))
                           for key, rule in constraint.value.items()
                           if rule is False and key in owner and owner[key] == error.instance]
                if matches:
                    return matches
        return [(parts, constraint.path)]

    def _schema_findings(self, value, node, path=""):
        errors = list(self.bundle.validator(node).iter_errors(value))
        for outer in errors:
            for error in self._relevant_errors(outer):
                origin = self.bundle.origins.get(id(error.schema))
                constraint = (Node(error.schema, *origin) if origin
                              else self._error_node(node, error.absolute_schema_path)) or node

                if error.validator == "unevaluatedProperties" and isinstance(error.instance, dict) and origin:
                    applied = self.bundle.active(Node(error.schema, *origin), error.instance)
                    allowed = {key for n in applied for key in n.value.get("properties", {})}
                    if set(error.instance) <= allowed:
                        # A failed referenced branch may fail to annotate known
                        # properties. Its real constraint error is already reported.
                        continue

                regulatory = constraint.uri == self.bundle.regulations_uri
                for instance_parts, constraint_path in self._error_targets(error, constraint, value):
                    self.issue(
                        "schema.rule_constraint" if regulatory else "schema.constraint",
                        path + pointer(instance_parts), error.message,
                        severity="warning" if regulatory else "error",
                        schema_ref=constraint.uri + "#" + constraint_path,
                        keyword=error.validator,
                    )
        return not errors

    def check_native(self):
        self.result.schema_valid = self._schema_findings(
            self.track, self.bundle.resolve(self.bundle.main_uri))

    def check_features(self):
        # The exchange schema permits partial feature patches. An effective
        # feature must also satisfy its complete catalog type after expansion.
        for obj in self.instances:
            if obj.category != "features":
                continue
            kind = obj.value.get("type")
            definitions = self.bundle.catalog("features").value["$defs"]
            if isinstance(kind, str) and kind in definitions and kind != "patch":
                node = self.bundle.catalog("features/$defs/" + kind)
                self._schema_findings(obj.value, node, obj.path)

    # Section, port and actor references.

    def _section(self, section_id, path):
        if not isinstance(section_id, str):
            self.block(path, "A section reference is missing or is not a string.")
            return None  # The native type/required check explains this case.
        section = self.sections.get(section_id)
        if not isinstance(section, dict):
            self.issue("schema.section_missing", path,
                       f"Section {section_id!r} does not exist.", section=section_id)
            return None
        return section

    def _ports(self, section):
        kind = section.get("type")
        if not isinstance(kind, str):
            return set()
        node = self.bundle.catalog("sections/$defs/" + kind)
        spec = node.value.get("x-caudri", {}).get("ports", {})
        ports = set(spec.get("fixed", []))
        params = section.get("parameters", {})
        optional = params.get("ports", []) if isinstance(params, dict) else []
        if isinstance(optional, list):
            ports.update(p for p in optional if isinstance(p, str) and p in spec.get("optional", []))
        return ports

    def _port(self, section_id, port, path):
        section = self._section(section_id, path)
        if not isinstance(port, str):
            self.block(path, "A port reference is missing or is not a string.")
            return False
        if section is None:
            return False
        available = self._ports(section)
        if port not in available:
            self.issue("schema.port_missing", path,
                       f"Section {section_id!r} has no available port {port!r}.",
                       section=section_id, port=port, available=sorted(available))
            return False
        return True

    def _span(self, span, path):
        if not isinstance(span, dict):
            if span is not None:
                self.block(path, "A span must be an object before its references can be checked.")
            return
        for name in ("start", "end"):
            endpoint = span.get(name)
            if not isinstance(endpoint, dict):
                self.block(child(path, name), "A span endpoint is missing or is not an object.")
                continue
            endpoint_path = child(path, name)
            section_id = endpoint.get("section")
            if "port" in endpoint:
                self._port(section_id, endpoint["port"], child(endpoint_path, "port"))
            else:
                section = self._section(section_id, child(endpoint_path, "section"))
                if section is not None and section.get("type") == "intersection" and "distance" in endpoint:
                    self.issue("schema.span_position", endpoint_path,
                               "An intersection span endpoint needs a named port, not a linear distance.")
        for i, section_id in enumerate(span.get("via", []) if isinstance(span.get("via", []), list) else []):
            self._section(section_id, child(child(path, "via"), i))

    def check_references(self):
        root = self.track["root"]
        self._port(root.get("section"), root.get("port"), "/root/port")

        for obj in self.instances:
            if obj.category == "features" and isinstance(obj.value.get("at"), dict):
                at = obj.value["at"]
                if obj.section is None and not obj.global_zone:
                    self._section(at.get("section"), child(child(obj.path, "at"), "section"))

            params = obj.value.get("parameters", {})
            params_path = child(obj.path, "parameters")
            if not isinstance(params, dict):
                if obj.category in {"zones", "actors"}:
                    self.block(params_path, "Parameter references need an object to inspect.")
                params = {}  # Spawn/behavior references can still be checked.
            if obj.category == "zones":
                self._span(params.get("span"), child(params_path, "span"))

            if obj.category == "actors":
                self._actor_references(obj, params)

    def _actor_references(self, obj, params):
        spawn = obj.value.get("spawn", {})
        if isinstance(spawn, dict) and isinstance(spawn.get("at"), dict):
            self._section(spawn["at"].get("section"), obj.path + "/spawn/at/section")
        self._span(params.get("route"), obj.path + "/parameters/route")

        reference = params.get("crosswalk")
        if isinstance(reference, str) and reference.count(".") == 1:
            section_id, element_id = reference.split(".")
            ref_path = obj.path + "/parameters/crosswalk"
            section = self._section(section_id, ref_path)
            if section is not None:
                elements = section.get("elements", {})
                element = elements.get(element_id) if isinstance(elements, dict) else None
                if not isinstance(element, dict) or element.get("type") != "crosswalk":
                    self.issue("schema.crosswalk_missing", ref_path,
                               f"{reference!r} does not identify an enabled crosswalk.")

        simulation = obj.value.get("simulation", {})
        behavior = simulation.get("behavior", {}) if isinstance(simulation, dict) else {}
        settings = behavior.get("parameters", {}) if isinstance(behavior, dict) else {}
        if not isinstance(settings, dict):
            return
        base = obj.path + "/simulation/behavior/parameters"
        stops = settings.get("stops", [])
        for i, stop in enumerate(stops if isinstance(stops, list) else []):
            if isinstance(stop, dict):
                path = child(base + "/stops", i)
                section = self._section(stop.get("section"), child(path, "section"))
                if section is not None and section.get("type") == "intersection":
                    self.issue("schema.stop_position", path,
                               "A distance-based actor stop needs a linear section.")
        for section_id, port in _objects(settings.get("intersection_exits")):
            ref_path = child(base + "/intersection_exits", section_id)
            section = self._section(section_id, ref_path)
            if section is not None:
                if section.get("type") != "intersection":
                    self.issue("schema.intersection_expected", ref_path,
                               f"Section {section_id!r} is not an intersection.")
                else:
                    self._port(section_id, port, ref_path)

    def check_connections(self):
        connections = self.track.get("connections", [])
        used = {}
        if not isinstance(connections, list):
            self.block("/connections", "Connections must be an array before endpoint checks can run.")
            return
        for i, connection in enumerate(connections):
            if not isinstance(connection, list) or len(connection) != 2:
                self.block(child("/connections", i), "A connection must contain two port references.")
                continue
            parsed = []
            for side, reference in enumerate(connection):
                path = child(child("/connections", i), side)
                if not isinstance(reference, str) or reference.count(".") != 1:
                    self.block(path, "A port reference must have section.port form.")
                    continue
                section_id, port = reference.split(".")
                if not self._port(section_id, port, path):
                    continue
                parsed.append((section_id, port))
                if reference in used:
                    self.issue("schema.port_reused", path,
                               f"Port {reference!r} is connected more than once.",
                               port=reference, first_path=used[reference])
                else:
                    used[reference] = path

            # Two-port sections keep one nominal direction along a branch.
            # Junction exits have distinct names (left/straight/right), so they
            # can still connect to either end of a two-port section.
            if len(parsed) == 2 and all(port == "entry" for _, port in parsed):
                self.issue("schema.entry_to_entry", child("/connections", i),
                           "Two entry ports cannot be connected under the track's connection convention.")
            elif len(parsed) == 2 and all(port == "exit" for _, port in parsed):
                self.issue("schema.exit_to_exit", child("/connections", i),
                           "Two exit ports of two-port sections cannot be connected: "
                           "a branch must retain a consistent nominal direction. "
                           "Connect exit to entry, or to an intersection's left, straight or right port.",
                           endpoints=connection)

    # Local parameter relationships.

    def check_intervals(self):
        for obj in self.instances:
            if obj.category not in {"elements", "zones"}:
                continue
            params = obj.value.get("parameters", {})
            interval = params.get("range") if isinstance(params, dict) else None
            needs_range = (obj.category == "zones" and obj.section is not None) or (
                obj.category == "elements" and obj.value.get("type") in
                {"barred_area", "side_road_junction", "parking_area"})
            if not isinstance(interval, dict):
                if needs_range:
                    self.block(obj.path + "/parameters/range", "Range endpoints are unavailable.")
                continue
            start, end = interval.get("start"), interval.get("end")
            if not (_number(start) and _number(end)):
                self.block(obj.path + "/parameters/range", "Range ordering needs two numeric endpoints.")
            elif start >= end:
                self.issue("schema.range_order", obj.path + "/parameters/range",
                           f"Range start ({start}) must be smaller than end ({end}).",
                           start=start, end=end)

        # A span can return to an earlier position after a loop; ranges use start < end.

    def _position(self, at, section_id, path):
        if not isinstance(at, dict):
            return
        section = self.sections.get(section_id) if isinstance(section_id, str) else None
        if not isinstance(section, dict):
            self.block(path, "Position kind cannot be checked without its referenced section.")
            return  # The reference check reports the cause.
        if section.get("type") == "intersection":
            if "distance" in at or not all(key in at for key in ("x", "y")):
                self.issue("schema.position_kind", path,
                           "Intersection positions use local x/y coordinates, not distance.")
        elif "distance" not in at or "x" in at or "y" in at:
            self.issue("schema.position_kind", path,
                       "Positions on a linear section use distance, not local x/y coordinates.")
        elif _number(at["distance"]):
            # Explicit positions stay on their named/owning section. Automatic
            # sign advances use geometric route traversal instead of this field.
            parameters = section.get("parameters", {})
            length = parameters.get("length") if isinstance(parameters, dict) else None
            if section.get("type") == "arc" and isinstance(parameters, dict):
                radius, angle = parameters.get("radius"), parameters.get("angle")
                if _number(radius) and _number(angle):
                    length = radius * math.radians(angle)
            if not _number(length) or not math.isfinite(length) or length <= 0:
                self.block(path + "/distance", "Position bounds need a usable section length.")
            elif not 0 <= at["distance"] <= length:
                self.issue("schema.position_range", path + "/distance",
                           "Explicit distance must lie between section entry and exit.",
                           section=section_id, distance=at["distance"], length=length)

    def _feature_placement(self, obj):
        value, path = obj.value, obj.path
        binding = self.expansion.provenance.get(path, {}).get("binding")
        if (obj.section is not None or obj.global_zone) and not binding:
            if "at" not in value and "pose" not in value:
                self.issue("schema.placement_missing", path,
                           "An additional feature needs at or pose; it has no generated slot binding.")
            elif obj.global_zone and "pose" not in value:
                self.issue("schema.zone_section_ambiguous", path,
                           "An extra global-zone feature needs pose, or a root feature with at.section.")
        at = value.get("at")
        if not isinstance(at, dict):
            return
        section_id = at.get("section", obj.section)
        if section_id is None and binding and binding.get("kind") == "zone_endpoint":
            # Generated global-zone slots inherit their own endpoint. The index
            # avoids rescanning every object for every generated zone sign.
            owner = self.by_path.get(path.rsplit("/features/", 1)[0])
            params = owner.value.get("parameters", {}) if owner else {}
            span = params.get("span", {}) if isinstance(params, dict) else {}
            endpoint = span.get(binding["endpoint"], {}) if isinstance(span, dict) else {}
            section_id = endpoint.get("section") if isinstance(endpoint, dict) else None
        self._position(at, section_id, child(path, "at"))

    def check_placements(self):
        root_id = self.track["root"].get("section")
        for obj in self.instances:
            value, path = obj.value, obj.path
            if obj.category == "features":
                self._feature_placement(obj)

            elif obj.category == "actors":
                spawn = value.get("spawn", {})
                if isinstance(spawn, dict) and isinstance(spawn.get("at"), dict):
                    self._position(spawn["at"], spawn["at"].get("section"), path + "/spawn/at")

            elif obj.category == "elements":
                params = value.get("parameters", {})
                if value.get("type") == "starting_line" and isinstance(params, dict):
                    root_slot = obj.section == root_id and path.endswith("/elements/starting_line")
                    if not root_slot and "distance" not in params:
                        self.issue("schema.starting_line_position", path + "/parameters",
                                   "A starting line outside the root slot needs an explicit distance.")
                if value.get("type") == "start_box" and obj.section != root_id:
                    self.issue("schema.root_only", path, "A start box is bound to the root section.")

    def check_section_parameters(self):
        for obj in self.instances:
            if obj.category != "sections":
                continue
            params = obj.value.get("parameters", {})
            if obj.value.get("type") == "clothoid":
                start = params.get("start_curvature") if isinstance(params, dict) else None
                end = params.get("end_curvature") if isinstance(params, dict) else None
                if not (_number(start) and _number(end)):
                    self.block(obj.path + "/parameters", "Curvature comparison needs two numeric values.")
                elif start == end:
                    self.issue("schema.clothoid_curvature", obj.path + "/parameters",
                               "A clothoid needs different start and end curvature; use a straight or arc otherwise.")

        # A positive but overly wide lane is a rule warning. A non-positive lane
        # has no usable road area, even though the catalog only states rule limits.
        roads = [(self.track.get("road"), "/road")]
        roads += [(obj.value.get("road"), obj.path + "/road")
                  for obj in self.instances if obj.category == "sections"]
        for road, path in roads:
            width = road.get("lane_width") if isinstance(road, dict) else None
            if _number(width) and width <= 0:
                self.issue("schema.lane_width_positive", child(path, "lane_width"),
                           "Lane width must be positive to define a road area.", actual=width)

    def run(self, skip):
        for name, title in CHECKS.items():
            self.group = name
            if name in skip:
                self.result.checks.append(CheckResult(name, "skipped", "Disabled by the caller."))
                continue
            self._group_findings = set()
            self._group_blocked = {}
            getattr(self, "check_" + name)()
            count = len(self._group_findings)
            blocked = len(self._group_blocked)
            self.result.checks.append(CheckResult(
                name, "failed" if count else "blocked" if blocked else "passed",
                f"{title}: {count} finding(s), {blocked} blocked operation(s)."
                if count or blocked else f"{title}: no issues found.",
                {"blocked_paths": self._group_blocked} if blocked else {},
            ))
        return self.result


# Public API and command-line entry point
# --------------------------------------

def validate_schema(track: Document | dict | ExpansionResult, *, bundle: SchemaBundle | None = None,
                    source_uri: str | None = None, skip=(), check_schema=False) -> ValidationResult:
    """@brief Expand first, then check structure, references and local values.

    @param track Authored document/dictionary or a reusable ExpansionResult.
    @param bundle Optional matching bundle to reuse across editor operations.
    @param source_uri Base URI when expanding an in-memory authored dictionary.
    @param skip Check-group names to report as explicitly skipped.
    @param check_schema Also check the schema documents against the meta-schema; off by default.
    @return Structured findings and check outcomes.
    @throws InputError For unreadable resources, failed schema self-checks or unknown check names.

    An incomplete expansion blocks validation.
    """
    skip = set(skip)
    unknown = skip - CHECKS.keys()
    if unknown:
        raise InputError(f"Unknown schema check(s): {', '.join(sorted(unknown))}")

    if isinstance(track, ExpansionResult):
        expansion = track
    else:
        # Share the loaded resources between expansion and validation.
        document = prepare_track(track, bundle=bundle, source_uri=source_uri)
        bundle = bundle or SchemaBundle.for_track(document)
        expansion = expand_track(document, bundle=bundle)

    if not expansion.complete:
        return ValidationResult(
            diagnostics=list(expansion.diagnostics),
            checks=[CheckResult(name, "skipped" if name in skip else "blocked",
                                "Disabled by the caller." if name in skip
                                else "Expansion could not resolve all required operations.")
                    for name in CHECKS],
            expansion_complete=False,
        )

    selected = bundle or SchemaBundle(expansion.track["$schema"])
    try:
        if check_schema:
            for document in {doc.uri: doc for doc in selected.documents.values()}.values():
                Draft202012Validator.check_schema(document.data)
        return _Validator(expansion, selected).run(skip)
    except SchemaError as exc:
        raise InputError(f"Invalid schema bundle: {exc.message}") from exc
    except Unresolvable as exc:
        raise InputError(f"Cannot resolve a schema reference: {exc}") from exc


def main(argv=None):
    """@brief CLI entry point; return 0 for no errors, 1 for errors, 2 for input/configuration failure."""
    parser = argparse.ArgumentParser(description="Expand, then validate CAuDri track structure and references.")
    parser.add_argument("track", type=Path, help="authored or expanded YAML/JSON Track Description")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="stdout format (default: text)")
    parser.add_argument("--report", type=Path, help="write a separate JSON report")
    parser.add_argument("--check-schema", action="store_true",
                        help="also validate schema/catalog/regulations files against the JSON Schema meta-schema")
    parser.add_argument("--skip", choices=list(CHECKS), action="append", default=[], help="skip one named check group; repeatable")
    parser.add_argument("-v", "--verbose", action="store_true", help="include passed/skipped checks and source/schema locations")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() == args.track.resolve():
        parser.error("Input and report must use different files.")

    try:
        result = validate_schema(load_track(args.track), skip=args.skip, check_schema=args.check_schema)
        exit_code = 1 if result.valid is False else 0
    except (InputError, OSError, ValueError) as exc:
        result = ValidationResult(
            diagnostics=[Diagnostic("schema.input", "error", "", str(exc))],
            checks=[CheckResult(name, "blocked", "Input or schema resources could not be loaded.") for name in CHECKS],
        )
        exit_code = 2

    report = json.dumps(result.report(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    try:
        if args.report:
            write_file(args.report, report)
        if args.format == "json":
            sys.stdout.write(report)
            stream = sys.stderr
        else:
            stream = sys.stdout
        for finding in result.diagnostics:
            print(finding.format(args.verbose), file=stream)
        if args.verbose:
            for check in result.checks:
                print(f"{check.status.upper()} {check.code}: {check.message}", file=stream)
        errors = sum(d.severity == "error" for d in result.diagnostics)
        warnings = sum(d.severity == "warning" for d in result.diagnostics)
        state = {True: "passed", False: "failed", None: "partially checked"}[result.valid]
        print(f"Schema validation {state}: {errors} error(s), {warnings} warning(s).", file=stream)
    except (OSError, ValueError) as exc:
        print(f"ERROR schema.output: {exc}", file=sys.stderr)
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
