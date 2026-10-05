#!/usr/bin/env python3
"""Check measurable CAuDri competition rules on an expanded planar track."""

from __future__ import annotations

from collections import defaultdict
import heapq
import math

from shapely.geometry import Point
from shapely.strtree import STRtree

if __package__:
    from .expand_track import child
    from .validate_schema import CheckResult
    from .validate_geometry import (Reporter, StageResult, PathError, objects,
                                    number, angle, validate_geometry, run_cli)
else:
    from expand_track import child
    from validate_schema import CheckResult
    from validate_geometry import (Reporter, StageResult, PathError, objects,
                                   number, angle, validate_geometry, run_cli)


__all__ = ["validate_regulations"]
CHECKS = ("values", "network", "route", "dimensions", "spacing", "zones",
          "signs", "guide_signs", "parking", "landmarks", "discipline")
ZONE_TYPES = {"centerline", "missing_marking", "speed_limit", "no_passing", "expressway", "grade"}
CHALLENGES = {"crosswalk", "barred_area", "parking_area"}


class Regulations:
    def __init__(self, geometry, reporter):
        self.geometry, self.model, self.reporter = geometry, geometry.model, reporter
        self.track, self.bundle = self.model.track, self.model.bundle
        self.objects = self.model.instances
        self.zones = [o for o in self.objects if o.kind in ZONE_TYPES and o.kind != "grade"]
        self.challenges = []
        self.port_graph = None
        self.route = None

    def rule(self, path):
        return self.bundle.resolve(self.bundle.regulations_uri+"#/$defs/"+path)

    def limit(self, path, keyword="minimum"):
        return self.rule(path).value[keyword]

    def measure(self, group, rule, value, path, message, **details):
        """Validate a measured value against its referenced rule, with machine precision rounding."""
        node = self.rule(rule)
        measured = round(value, 10) if isinstance(value, float) else value
        errors = list(self.bundle.validator(node).iter_errors(measured))
        if errors:
            self.reporter.issue(group, "measurement", path, message, schema_ref=node.ref,
                                actual=value, expected=node.value, **details)
        return not errors

    def ignored_rule(self, finding):
        if finding.path.startswith("/actors/"):
            return True
        return any(finding.path == obj.path or finding.path.startswith(obj.path+"/")
                   for obj in self.model.instances if obj.kind == "grade")

    def check_values(self):
        for finding in self.geometry.schema_result.diagnostics:
            if finding.code != "schema.rule_constraint" or self.ignored_rule(finding):
                continue
            if "/$defs/disciplines/" in (finding.schema_ref or ""):
                continue
            self.reporter.issue("values", "value", finding.path, finding.message,
                                schema_ref=finding.schema_ref,
                                source_check=finding.code, **{k: v for k, v in finding.details.items() if k != "check"})

    def check_network(self):
        model = self.model
        components = {s.component for s in model.sections.values()}
        if len(model.sections) != len(self.track.get("sections", {})):
            self.reporter.block("network", "/sections", "Some section primitives are unavailable")
        if len(components) > 1:
            self.reporter.issue("network", "disconnected", "/sections",
                                "The road network has disconnected components.", components=len(components))
        for sid, section in model.sections.items():
            for port in section.ports:
                if (sid, port) not in model.links:
                    self.reporter.issue("network", "open_port", section.path,
                                        f"The closed network has an open port: {sid}.{port}.", port=port)

    def check_route(self):
        discipline = self.track.get("discipline")
        if discipline is None:
            self.reporter.result.checks.append(self.inapplicable("route", "No discipline selected."))
            return
        try:
            node = self.rule("disciplines/$defs/"+discipline)
            requirement = node.value["x-caudri"]["course"]["designated_route"]["requirement"]
        except (KeyError, TypeError):
            self.reporter.block("route", "/discipline", "Discipline has no route policy")
            return
        if requirement != "required":
            self.reporter.result.checks.append(self.inapplicable("route", "Selected discipline permits a track without a designated route."))
            return
        root = self.track["root"]
        try:
            self.route = self.model.resolve_span({"start": root, "end": root}, discipline=True)
            self.reporter.result.checks.append(self.passed("route", "Designated route returns to the start.",
                length=sum(leg.length for leg in self.route),
                visits=[{"section": leg.section, "incoming": leg.incoming, "outgoing": leg.outgoing,
                         "start": leg.start, "end": leg.end} for leg in self.route]))
        except (PathError, KeyError, TypeError, ValueError) as exc:
            self.reporter.issue("route", "designated_route", "/root", str(exc))

    @staticmethod
    def passed(code, message, **details):
        return CheckResult(code, "passed", message, details)

    @classmethod
    def inapplicable(cls, code, message):
        result = cls.passed(code, message, applicable=False)
        result.status = "skipped"
        return result

    def zone_intervals(self, obj):
        return [(leg.section, min(leg.start, leg.end), max(leg.start, leg.end), leg.end >= leg.start)
                for leg in obj.route or [] if not self.model.sections[leg.section].intersection]

    def styles(self, sid, station):
        section = self.model.sections[sid]
        inherited = section.value["road"]["centerline"]
        applied = set()
        for obj in self.zones:
            if obj.kind not in {"centerline", "no_passing"}:
                continue
            for owner, a, b, forward in self.zone_intervals(obj):
                if owner == sid and a <= station < b:
                    p = obj.value["parameters"]
                    style = p["style"] if obj.kind == "centerline" else p["centerline"]
                    dashed_side = (forward if obj.kind == "centerline" else not forward) if style == "mixed" else None
                    applied.add((style, dashed_side))
        return applied or {(inherited, None)}

    def check_dimensions(self):
        for sid, section in self.model.sections.items():
            if section.intersection:
                continue
            k = max(abs(section.curve.k0), abs(section.curve.k1))
            if k:
                self.measure("dimensions", "sections/$defs/arc/$defs/validation/properties/inner_radius",
                             1/k-section.inner, section.path, "Inner drivable radius is below the rule minimum.")
            boundaries = {0, section.curve.length}
            for obj in self.zones:
                for owner, a, b, _ in self.zone_intervals(obj):
                    if owner == sid:
                        boundaries.update((a, b))
            ordered = sorted(boundaries)
            for a, b in zip(ordered, ordered[1:]):
                styles = self.styles(sid, (a+b)/2)
                if len(styles) > 1:
                    self.reporter.block("dimensions", section.path,
                                        "Conflicting centerline zones leave the effective lane width unresolved")
                    continue
                style = next(iter(styles))[0]
                road = section.value["road"]
                half_center = section.marking/2 if style == "dashed" else section.marking+road["rendering"]["centerline_double_line_spacing"]/2
                if style != "dashed":
                    self.measure("dimensions", "road/$defs/rendering/properties/centerline_double_total_width",
                                 2*half_center, section.path+"/road/rendering", "Derived double-centerline width violates the rule.", range=[a, b])
                effective = section.inner-half_center
                self.measure("dimensions", "road/$defs/parameters/properties/lane_width", effective,
                             section.path+"/road/lane_width", "Effective clear lane width violates the rule.", range=[a, b])
        for obj in self.objects:
            if obj.kind in {"barred_area", "side_road_junction"} and obj.start is not None:
                self.measure("dimensions", "elements/$defs/"+obj.kind+"/$defs/validation/properties/length",
                             obj.end-obj.start, obj.path, "Element length exceeds the rule limit.")
        roots = [o for o in self.objects if o.kind == "starting_line"]
        self.measure("dimensions", "elements/$defs/starting_line/$defs/validation/properties/maximum_count",
                     len(roots), "/sections", "Competition track requires exactly one starting line.")
        root = self.track["root"]
        for obj in roots:
            if obj.section != root["section"] or obj.slot != "starting_line":
                self.reporter.issue("dimensions", "starting_line_root", obj.path, "Starting line must occupy the reserved root slot.")
            elif obj.start is not None:
                section = self.model.sections[obj.section]
                self.measure("dimensions", "elements/$defs/starting_line/$defs/validation/properties/trailing_edge_at_root_exit",
                             abs(obj.start-section.curve.length) <= self.model.tolerance, obj.path,
                             "The starting line's trailing edge must coincide with the root exit.")
        section = self.model.sections.get(root["section"])
        if section:
            base = "elements/$defs/start_box/$defs/validation/properties/"
            self.measure("dimensions", base+"root_section_type", section.value["type"], section.path,
                         "The start box requires an arc root section.")
            if section.value["type"] == "arc":
                self.measure("dimensions", base+"root_section_direction", section.value["parameters"]["direction"],
                             section.path, "The start box requires a left arc.")
            if section.value.get("elements", {}).get("start_box") is False:
                self.reporter.issue("dimensions", "start_box_missing", section.path+"/elements/start_box", "Root start box is disabled.")

    def build_port_graph(self):
        graph = defaultdict(list)
        for key, (sid, port, _) in self.model.links.items():
            graph[key].append(((sid, port), 0))
        for sid, section in self.model.sections.items():
            if section.intersection:
                for a in section.ports:
                    for b in section.ports:
                        if a == b:
                            continue
                        try:
                            length = self.model.junction_leg(section, a, b).length
                            graph[(sid, a)].append(((sid, b), length))
                        except (PathError, ValueError):
                            continue
            else:
                graph[(sid, "entry")].append(((sid, "exit"), section.curve.length))
                graph[(sid, "exit")].append(((sid, "entry"), section.curve.length))
        self.port_graph = graph

    def distances(self, anchors):
        heap, best = [], {}
        for node, cost in anchors:
            if cost < best.get(node, math.inf):
                best[node] = cost
                heapq.heappush(heap, (cost, node))
        while heap:
            cost, node = heapq.heappop(heap)
            if cost != best[node]:
                continue
            for target, weight in self.port_graph[node]:
                proposed = cost+weight
                if proposed < best.get(target, math.inf):
                    best[target] = proposed
                    heapq.heappush(heap, (proposed, target))
        return best

    def anchors(self, challenge):
        anchors = []
        for sid, a, b in challenge["intervals"]:
            section = self.model.sections[sid]
            anchors += [((sid, "entry"), max(0, a)), ((sid, "exit"), max(0, section.curve.length-b))]
        sid = challenge.get("intersection")
        if sid:
            section = self.model.sections[sid]
            d = section.value["parameters"]["port_distance"]
            anchors += [((sid, p), max(0, d-self.model.crossing_station(section, p))) for p in section.ports]
        return anchors

    def build_challenges(self):
        root_items = []
        for obj in self.objects:
            if obj.kind in CHALLENGES and obj.start is not None:
                self.challenges.append({"path": obj.path, "intervals": [(obj.section, obj.start, obj.end)]})
            elif obj.kind == "section" and self.model.sections[obj.section].intersection:
                self.challenges.append({"path": obj.path, "intervals": [], "intersection": obj.section})
            elif obj.kind in {"start_box", "starting_line"} and obj.section == self.track["root"]["section"] and obj.slot in {"start_box", "starting_line"} and obj.start is not None:
                section = self.model.sections[obj.section]
                a, b = obj.start, min(obj.end, section.curve.length)
                root_items.append((obj.section, a, b))
                if obj.end > section.curve.length:
                    link = self.model.links.get((obj.section, "exit"))
                    if link and not self.model.sections[link[0]].intersection:
                        other = self.model.sections[link[0]]
                        length = obj.end-section.curve.length
                        root_items.append((link[0], 0, length) if link[1] == "entry" else (link[0], other.curve.length-length, other.curve.length))
        if root_items:
            self.challenges.append({"path": "/root", "intervals": root_items})
        distance_limit = self.limit("road/$defs/validation/properties/artifact_marking_spacing")
        roads = [s for s in self.model.sections.values() if s.surface is not None]
        for obj in self.objects:
            if obj.kind != "static_obstacle" or obj.parked:
                continue
            if obj.shape is None:
                self.reporter.block("spacing", obj.path, "Obstacle footprint is unavailable")
                continue
            owner = self.model.sections.get(obj.placed_section)
            component = owner.component if owner else self.model.root_component
            if component in self.model.inconsistent_components:
                self.reporter.block("spacing", obj.path, "Obstacle-to-road placement depends on an inconsistent loop closure")
                continue
            nearby = [s for s in roads if s.component == component and obj.shape.distance(s.surface) <= distance_limit]
            if not nearby:
                continue
            intervals = []
            for section in nearby:
                if section.intersection:
                    if obj.shape.intersects(section.surface):
                        self.reporter.issue("spacing", "obstacle_in_intersection", obj.path,
                                            "Static obstacle occupies an intersection.", related_paths=[section.path])
                    continue
                projected = [section.project(p) for p in obj.shape.exterior.coords]
                intervals.append((section.identifier, min(projected), max(projected)))
            if intervals:
                self.challenges.append({"path": obj.path, "intervals": intervals})

    def check_spacing(self):
        self.build_challenges()
        self.build_port_graph()
        for i, first in enumerate(self.challenges):
            distances = self.distances(self.anchors(first))
            for second in self.challenges[i+1:]:
                best = min((distances.get(node, math.inf)+cost for node, cost in self.anchors(second)), default=math.inf)
                for sid, a, b in first["intervals"]:
                    for other, c, d in second["intervals"]:
                        if sid == other:
                            best = min(best, max(0, max(a, c)-min(b, d)))
                if math.isfinite(best):
                    self.measure("spacing", "road/$defs/validation/properties/road_element_spacing", best,
                                 first["path"], "Challenges are too close along the reference line.",
                                 related_paths=[second["path"]])
        self.neighbor_spacing()

    def neighbor_spacing(self):
        roads = [s for s in self.model.sections.values() if s.surface is not None]
        if not roads:
            return
        limit = self.limit("road/$defs/validation/properties/neighboring_road_spacing")
        epsilon = 2*self.model.approximation
        tree = STRtree([s.surface for s in roads])
        for i, first in enumerate(roads):
            for j in tree.query(first.surface.buffer(limit+epsilon)):
                j = int(j)
                if j <= i:
                    continue
                second = roads[j]
                if first.component != second.component:
                    continue
                if first.component in self.model.inconsistent_components:
                    self.reporter.block("spacing", first.path, "Road spacing depends on an inconsistent loop closure")
                    continue
                if any(link[0] == second.identifier for (sid, _), link in self.model.links.items() if sid == first.identifier):
                    continue
                distance = first.surface.distance(second.surface)
                bound = self.model.approximation*sum(s.intersection or s.curve.k0 != 0 or s.curve.k1 != 0 for s in (first, second))
                # Refine close calls. Exact straight rectangles only need a
                # floating-point epsilon, not the curved-surface error bound.
                if bound and abs(distance-limit) <= bound:
                    refined = self.model.approximation/100
                    distance = self.model.road_shape(first, refined).distance(self.model.road_shape(second, refined))
                    bound /= 100
                if bound and abs(distance-limit) <= bound:
                    self.reporter.block("spacing", first.path+"|"+second.path,
                                        "Road spacing lies within the approximation uncertainty of its rule limit")
                elif distance < limit-1e-10:
                    self.measure("spacing", "road/$defs/validation/properties/neighboring_road_spacing", distance,
                                 first.path, "Neighboring roads are too close.", related_paths=[second.path],
                                 approximation_bound=bound)

    def after_zone(self, obj, limit):
        last = obj.route[-1]
        section = self.model.sections[last.section]
        current, forward = last.end, last.end >= last.start
        walked, visited = 0., set()
        if section.intersection:
            link = self.model.links.get((section.identifier, last.outgoing))
            if link is None:
                self.reporter.block("zones", obj.path, "Zone ends at an open port")
                return
            section = self.model.sections[link[0]]
            current, forward = (0, True) if link[1] == "entry" else (section.curve.length, False)
        while walked < limit:
            token = (section.identifier, forward)
            if token in visited:
                return
            visited.add(token)
            for target in self.challenges:
                for sid, a, b in target["intervals"]:
                    if sid == section.identifier and (b >= current if forward else a <= current):
                        distance = walked+max(0, a-current if forward else current-b)
                        if distance < limit-1e-10:
                            self.measure("zones", "zones/$defs/no_passing/$defs/validation/properties/post_zone_element_clearance",
                                         distance, obj.path, "A challenge follows the no-passing zone too closely.",
                                         related_paths=[target["path"]])
            walked += section.curve.length-current if forward else current
            outgoing = "exit" if forward else "entry"
            link = self.model.links.get((section.identifier, outgoing))
            if link is None:
                if walked < limit:
                    self.reporter.block("zones", obj.path, "Open road prevents checking the full post-zone clearance")
                return
            section = self.model.sections[link[0]]
            if section.intersection:
                distance = walked+section.value["parameters"]["port_distance"]-self.model.crossing_station(section, link[1])
                self.measure("zones", "zones/$defs/no_passing/$defs/validation/properties/post_zone_element_clearance",
                             distance, obj.path, "An intersection follows the no-passing zone too closely.",
                             related_paths=[section.path])
                return
            current, forward = (0, True) if link[1] == "entry" else (section.curve.length, False)

    def check_zones(self):
        for obj in self.zones:
            if obj.kind == "missing_marking":
                continue
            if not obj.route:
                self.reporter.block("zones", obj.path, "Zone path is unresolved")
                continue
            if obj.kind == "expressway":
                self.measure("zones", "zones/$defs/expressway/$defs/validation/properties/length",
                             sum(leg.length for leg in obj.route), obj.path, "Expressway is too short.")
                for leg in obj.route:
                    section = self.model.sections[leg.section]
                    if section.intersection and leg.incoming in section.value["parameters"]["controlled_ports"]:
                        self.reporter.issue("zones", "expressway_priority", obj.path,
                                            "Expressway approach must have priority.", related_paths=[section.path])
            if obj.kind in {"no_passing", "expressway"}:
                for obstacle in self.objects:
                    if obstacle.kind != "static_obstacle" or obstacle.parked or obstacle.shape is None:
                        continue
                    for sid, a, b, _ in self.zone_intervals(obj):
                        section = self.model.sections[sid]
                        if section.surface is not None and obstacle.shape.intersects(section.surface):
                            if section.component in self.model.inconsistent_components:
                                self.reporter.block("zones", obj.path, "Obstacle placement depends on an inconsistent loop closure")
                                continue
                            projected = [section.project(p) for p in obstacle.shape.exterior.coords]
                            if max(projected) >= a and min(projected) <= b:
                                self.reporter.issue("zones", "forbidden_obstacle", obstacle.path,
                                    f"Static obstacle lies in a {obj.kind} zone.", related_paths=[obj.path])
                                break
            if obj.kind == "no_passing":
                if not self.port_graph:
                    self.build_challenges()
                    self.build_port_graph()
                self.after_zone(obj, self.limit("zones/$defs/no_passing/$defs/validation/properties/post_zone_element_clearance"))

        for i, first in enumerate(self.zones):
            for second in self.zones[i+1:]:
                same_effect = first.kind == second.kind and first.kind in {"speed_limit", "centerline", "no_passing"}
                center_effect = {first.kind, second.kind} <= {"centerline", "no_passing"}
                if not same_effect and not center_effect:
                    continue
                for sid, a, b, forward in self.zone_intervals(first):
                    for other, c, d, other_forward in self.zone_intervals(second):
                        if sid != other or min(b, d)-max(a, c) <= 1e-10:
                            continue
                        if first.kind == "speed_limit":
                            conflict = forward == other_forward and first.value["parameters"]["value_kmh"] != second.value["parameters"]["value_kmh"]
                        else:
                            conflict = len(self.styles(sid, (max(a, c)+min(b, d))/2)) > 1
                        if conflict:
                            self.reporter.issue("zones", "zone_conflict", first.path,
                                "Overlapping zones prescribe conflicting effects; neither has priority.",
                                related_paths=[second.path], section=sid,
                                range=[max(a, c), min(b, d)])
                            self.reporter.block("zones", first.path+"|"+second.path, "Effective zone value is ambiguous")

    def expected_slots(self, owner):
        if owner.kind == "section":
            section = self.model.sections[owner.section]
            if not section.intersection:
                return {}
            p = owner.value["parameters"]
            result = {}
            for port in section.ports:
                if p["layout"] == "priority_to_right":
                    continue
                controlled = port in p["controlled_ports"]
                slot = "stop_sign" if p["layout"] == "stop" else "give_way_sign"
                sign = "stop" if p["layout"] == "stop" else "give_way"
                result[port+"_"+(slot if controlled else "priority_sign")] = {"sign": sign if controlled else "priority_road"}
            turn = p.get("mandatory_turn", [])
            ring = ["entry", "right", "straight", "left"]
            if len(turn) == 2:
                for a, b in (turn, turn[::-1]):
                    delta = (ring.index(b)-ring.index(a)) % 4
                    result[a+"_mandatory_sign"] = {"sign": "mandatory_right" if delta == 1 else "mandatory_left"}
            return result
        if owner.kind in {"crosswalk", "barred_area", "parking_area", "speed_limit", "no_passing", "expressway"}:
            category = "elements" if owner.kind in CHALLENGES else "zones"
            node = self.bundle.catalog(category+"/$defs/"+owner.kind)
            recipes = node.value.get("x-caudri", {}).get("expansion", {})
            result = {}
            for slot, recipe in recipes.items():
                if recipe.get("type") == "traffic_sign":
                    params = dict(recipe.get("parameters", {}))
                    if owner.kind == "speed_limit":
                        params["displayed_value"] = owner.value["parameters"]["value_kmh"]
                    if owner.kind in {"speed_limit", "no_passing", "expressway"}:
                        params["faces"] = owner.value["parameters"]["direction"]
                    result[slot] = params
            return result
        return {}

    def approach_distance(self, obj, section, anchor, forward):
        """Measure upstream from a challenge, through preceding sections as needed."""
        if obj.placed_section == section.identifier:
            return (anchor-section.project(obj.position))*(1 if forward else -1)
        walked = anchor if forward else section.curve.length-anchor
        port = "entry" if forward else "exit"
        seen = set()
        while (section.identifier, port) not in seen:
            seen.add((section.identifier, port))
            link = self.model.links.get((section.identifier, port))
            if link is None:
                raise PathError("Sign is not on the connected approach to its challenge")
            section = self.model.sections[link[0]]
            incoming = link[1]
            if section.intersection:
                port = self.model.primary(section, incoming)
                leg = self.model.junction_leg(section, incoming, port)
                if obj.placed_section == section.identifier:
                    raise PathError("Sign on an intersection needs an explicit approach association")
                walked += leg.length
            else:
                if obj.placed_section == section.identifier:
                    station = section.project(obj.position)
                    return walked+(station if incoming == "entry" else section.curve.length-station)
                walked += section.curve.length
                port = "exit" if incoming == "entry" else "entry"
        raise PathError("Sign is outside the challenge's unique approach path")

    def sign_binding(self, obj):
        owner = obj.owner
        if owner is None:
            return
        if owner.kind in CHALLENGES:
            forward = owner.value["parameters"]["direction"] == "forward"
            if owner.kind == "crosswalk":
                forward = obj.slot == "sign_forward"
                field = "sign_distance"
            else:
                field = "priority_sign_distance" if owner.kind == "barred_area" else "announcement_sign_distance"
            section = self.model.sections[owner.section]
            anchor = owner.start if forward else owner.end
            try:
                advance = self.approach_distance(obj, section, anchor, forward)
                self.measure("signs", "elements/$defs/"+owner.kind+"/$defs/parameters/properties/"+field,
                             advance, obj.path, "Sign is outside its challenge's permitted approach range.")
            except (PathError, TypeError, ValueError) as exc:
                self.reporter.block("signs", obj.path, str(exc))
        elif owner.kind in {"speed_limit", "no_passing", "expressway"}:
            try:
                _, _, expected, _ = self.model.sign_default(obj)
                error = math.dist(obj.position, expected)
                if error > self.model.tolerance:
                    self.reporter.issue("signs", "zone_sign_position", obj.path,
                        "Zone sign is displaced from its endpoint binding.", distance=error,
                        expected_position=expected, actual_position=obj.position)
            except (PathError, KeyError, ValueError) as exc:
                self.reporter.block("signs", obj.path, str(exc))

    def check_signs(self):
        for owner in self.objects:
            expected = self.expected_slots(owner)
            slots = owner.value.get("features", {})
            for slot, parameters in expected.items():
                value = slots.get(slot)
                path = child(owner.path+"/features", slot)
                if not isinstance(value, dict):
                    self.reporter.issue("signs", "required_sign_missing", path, "Required traffic-sign slot is disabled or absent.", expected=parameters)
                    continue
                if value.get("type") != "traffic_sign" or any(value.get("parameters", {}).get(k) != v for k, v in parameters.items()):
                    self.reporter.issue("signs", "sign_semantics", path,
                                        "Traffic sign differs from its owner's prescribed meaning.", expected=parameters,
                                        actual=value.get("parameters"))
            if owner.kind == "section" and self.model.sections[owner.section].intersection:
                params = owner.value["parameters"]
                if params["layout"] in {"stop", "give_way"}:
                    self.measure("signs", "sections/$defs/intersection/$defs/validation/$defs/"+params["layout"]+"/properties/controlled_port_count",
                                 len(params["controlled_ports"]), owner.path, "Intersection has an invalid number of controlled approaches.")

        for obj in self.objects:
            if obj.kind != "traffic_sign":
                continue
            if obj.owner and obj.owner.kind == "grade":
                continue
            if obj.position is None:
                self.reporter.block("signs", obj.path, "Sign position is unavailable")
                continue
            panel_width, _ = self.model.sign_panel_size(obj.value)
            assigned = self.model.sections.get(obj.placed_section)
            component = assigned.component if assigned else self.model.root_component
            if component in self.model.inconsistent_components:
                self.reporter.block("signs", obj.path, "Sign-to-road placement depends on an inconsistent loop closure")
                continue
            section = self.model.sections.get(obj.placed_section)
            if section is None:
                roads = [s for s in self.model.sections.values() if s.component == self.model.root_component and s.surface is not None]
                if not roads:
                    self.reporter.block("signs", obj.path, "No anchored road geometry for the sign")
                    continue
                distances = sorted((s.surface.distance(Point(obj.position)), s.identifier) for s in roads)
                if len(distances) > 1 and abs(distances[0][0]-distances[1][0]) < self.model.tolerance:
                    self.reporter.block("signs", obj.path, "Sign has multiple equally near roads; specify its section")
                    continue
                section = self.model.sections[distances[0][1]]
                obj.placed_section = section.identifier
            if section.intersection:
                owner = obj.owner
                if owner and owner.kind == "section" and obj.slot.split("_", 1)[0] in section.ports:
                    port = obj.slot.split("_", 1)[0]
                    arm = self.model.arm(section, port)
                    dx, dy = obj.position[0]-section.pose[0], obj.position[1]-section.pose[1]
                    c, s = math.cos(section.pose[2]), math.sin(section.pose[2])
                    local = (c*dx+s*dy-section.value["parameters"]["port_distance"], -s*dx+c*dy)
                    yaw = section.pose[2]+arm["angle"]
                    panel_reach = panel_width/2*abs(math.cos(obj.yaw-yaw))
                    lateral = local[0]*arm["n"][0]+local[1]*arm["n"][1]-arm["half"]-panel_reach
                    station = local[0]*arm["u"][0]+local[1]*arm["u"][1]
                    advance = station-self.model.crossing_station(section, port)
                    field = "mandatory_sign_distance" if obj.slot.endswith("mandatory_sign") else "priority_sign_distance"
                    self.measure("signs", "sections/$defs/intersection/$defs/parameters/properties/"+field, advance,
                                 obj.path, "Intersection sign is outside its permitted longitudinal range.")
                else:
                    self.reporter.block("signs", obj.path, "An independent intersection sign needs an unambiguous approach association")
                    continue
            else:
                station = section.project(obj.position)
                center = section.point(station)
                yaw = section.yaw(station)
                normal = (-math.sin(yaw), math.cos(yaw))
                signed = (obj.position[0]-center[0])*normal[0]+(obj.position[1]-center[1])*normal[1]
                # An explicit rotated panel contributes only its transverse
                # width; the vertical height of the plan-view icon is irrelevant.
                panel_reach = panel_width/2*abs(math.cos(obj.yaw-yaw))
                lateral = abs(signed)-section.half-panel_reach
                if (signed > 0) == (math.cos(obj.yaw-yaw) > 0):
                    self.reporter.issue("signs", "sign_side", obj.path, "Traffic sign faces the lane on the wrong roadside.")
            self.measure("signs", "features/$defs/traffic_sign/$defs/parameters/properties/lateral_offset",
                         lateral, obj.path, "Measured sign offset violates the roadside clearance rule.")
            self.sign_binding(obj)

    def check_guide_signs(self):
        radius_limit = self.limit("features/$defs/traffic_sign/$defs/validation/properties/guide_curve_inner_radius", "exclusiveMaximum")
        straight_limit = self.limit("features/$defs/traffic_sign/$defs/validation/properties/guide_preceding_straight_length")
        guides = [o for o in self.objects if o.kind == "traffic_sign" and o.position is not None and
                  o.value.get("parameters", {}).get("sign", "").startswith("guide_")]
        for sid, section in self.model.sections.items():
            if section.value["type"] != "arc" or 1/abs(section.curve.k0)-section.inner >= radius_limit:
                continue
            for incoming in ("entry", "exit"):
                link = self.model.links.get((sid, incoming))
                length, straight_sections, seen = 0., set(), set()
                while link and link[0] not in seen:
                    previous = self.model.sections[link[0]]
                    if previous.value["type"] != "straight":
                        break
                    seen.add(previous.identifier)
                    straight_sections.add(previous.identifier)
                    length += previous.curve.length
                    other = "entry" if link[1] == "exit" else "exit"
                    link = self.model.links.get((previous.identifier, other))
                if length < straight_limit:
                    continue
                forward = incoming == "entry"
                turn_left = (section.curve.k0 > 0) == forward
                names = {"guide_left", "guide_left_multiple"} if turn_left else {"guide_right", "guide_right_multiple"}
                relevant = [o for o in guides if o.value["parameters"]["sign"] in names and
                            o.placed_section in straight_sections | {sid}]
                relevant = [o for o in relevant if math.cos(o.yaw-(section.port(incoming)[2]+math.pi)) > 0]
                if not relevant:
                    self.reporter.issue("guide_signs", "guide_signs_required", section.path,
                        f"The {incoming} approach requires curve guide signs; add them explicitly.",
                        schema_ref=self.rule("features/$defs/traffic_sign/$defs/validation").ref,
                        approach=incoming, inner_radius=1/abs(section.curve.k0)-section.inner,
                        preceding_straight_length=length, expected_signs=sorted(names))

    def check_parking(self):
        areas = [o for o in self.objects if o.kind == "parking_area"]
        if len(areas) > 1:
            self.reporter.issue("parking", "parking_area_count", "/sections",
                                "At most one parking area is allowed.", actual=len(areas), maximum=1,
                                policy="agreed-track-conventions")
        base = "elements/$defs/parking_area/$defs/validation/properties/"
        for area in areas:
            if area.start is None or area.end is None:
                self.reporter.block("parking", area.path, "Parking interval is unresolved")
                continue
            boundaries = {area.start, area.end}
            for zone in self.zones:
                for sid, a, b, _ in self.zone_intervals(zone):
                    if sid == area.section:
                        boundaries.update(x for x in (a, b) if area.start < x < area.end)
            ordered = sorted(boundaries)
            for a, b in zip(ordered, ordered[1:]):
                styles = self.styles(area.section, (a+b)/2)
                if len(styles) != 1:
                    self.reporter.block("parking", area.path, "Parking centerline is ambiguous")
                    continue
                self.measure("parking", base+"centerline", next(iter(styles))[0], area.path,
                             "Parking area requires a dashed centerline.", range=[a, b])
            section = self.model.sections[area.section]
            for obj in self.objects:
                if obj.kind != "static_obstacle" or obj.parked or obj.shape is None or section.surface is None:
                    continue
                if section.component in self.model.inconsistent_components:
                    self.reporter.block("parking", area.path, "Road placement depends on an inconsistent loop closure")
                    break
                if obj.shape.intersects(section.surface):
                    stations = [section.project(p) for p in obj.shape.exterior.coords]
                    if max(stations) >= area.start and min(stations) <= area.end:
                        self.reporter.issue("parking", "parking_lane_obstacle", obj.path,
                            "Static obstacle occupies the parking area's driving lanes.", related_paths=[area.path])
            for name in ("parallel_parking", "perpendicular_parking"):
                lot = self.model.by_path.get(area.path+"/parameters/"+name)
                if lot is None:
                    self.reporter.issue("parking", "parking_type_missing", area.path,
                                        "A parking area must contain both parking types.", missing=name)
                    continue
                spots = [o for o in self.objects if o.kind == "parking_spot" and o.owner is lot]
                free = [s for s in spots if s.value["state"] == "free"]
                rule = "parallel_spot_count" if name == "parallel_parking" else "perpendicular_spot_count"
                self.measure("parking", base+rule, len(free), lot.path, "Parking bay needs a usable free spot.")
                ordered = sorted(spots, key=lambda s: s.start if s.start is not None else math.inf)
                for a, b in zip(ordered, ordered[1:]):
                    if a.end is not None and b.start is not None and a.end > b.start+self.model.tolerance:
                        self.reporter.issue("parking", "spot_overlap", a.path, "Parking spots overlap longitudinally.", related_paths=[b.path])
                if name == "parallel_parking" and free:
                    free.sort(key=lambda s: s.start if s.start is not None else math.inf,
                              reverse=area.value["parameters"]["direction"] == "backward")
                    lengths = [s.value["extent_along_road"] for s in free]
                    if any(a >= b for a, b in zip(lengths, lengths[1:])):
                        self.reporter.issue("parking", "parking_sizes", lot.path,
                                            "Free parallel parking spots must grow in approach order.", lengths=lengths)
                    self.measure("parking", base+"parallel_final_spot_length", lengths[-1], free[-1].path,
                                 "Last parallel parking spot is too short.")
                    threshold = self.limit(base+"parallel_invalid_gap_below", "const")
                    for spot in free:
                        if spot.value["extent_along_road"] < threshold:
                            self.reporter.issue("parking", "invalid_parking_gap", spot.path,
                                                "This free gap is too short to count as a parking spot.", minimum=threshold)
                for spot in spots:
                    if spot.value["state"] == "blocked":
                        obstacle = spot.value.get("features", {}).get("obstacle")
                        if not isinstance(obstacle, dict):
                            self.reporter.issue("parking", "blocked_without_obstacle", spot.path,
                                                "A blocked spot requires its obstacle slot.")
                    for obstacle in self.objects:
                        if not obstacle.parked or obstacle.owner is not spot or obstacle.shape is None or spot.shape is None:
                            continue
                        # Lateral gaps run along the road for perpendicular
                        # parking and across the bay for parallel parking.
                        axis = spot.yaw+(math.pi/2 if name == "parallel_parking" else 0)
                        extent = spot.value["extent_lateral"] if name == "parallel_parking" else spot.end-spot.start
                        offsets = [(x-spot.position[0])*math.cos(axis)+(y-spot.position[1])*math.sin(axis)
                                   for x, y in obstacle.shape.exterior.coords]
                        rendering = area.value["rendering"]
                        width = rendering["parallel_outer_marking_width"] if name == "parallel_parking" else rendering["perpendicular_separator_width"]
                        widths = {"negative": width, "positive": width}
                        if name == "parallel_parking":
                            near = "positive" if area.value["parameters"]["direction"] == "forward" else "negative"
                            far = "negative" if near == "positive" else "positive"
                            widths[near] = max(width, section.marking)
                            if abs(spot.value["extent_lateral"]-lot.value["extent_lateral"]) <= self.model.tolerance:
                                widths[far] = max(width, rendering["boundary_outline_width"])
                        # Spot dimensions end at marking centerlines; free gaps
                        # end at the painted edge facing the obstacle instead.
                        for side, gap in (("negative", extent/2+min(offsets)), ("positive", extent/2-max(offsets))):
                            self.measure("parking", base+name.split("_")[0]+"_obstacle_lateral_gap",
                                         gap-widths[side]/2, obstacle.path, "Parked obstacle's lateral gap violates the rule.", side=side)

    def check_landmarks(self):
        landmarks = [o for o in self.objects if o.kind == "landmark"]
        identifiers = [o.value["parameters"]["identifier"] for o in landmarks]
        if sorted(identifiers) != list(range(len(identifiers))):
            self.reporter.issue("landmarks", "landmark_ids", "/features",
                                "Landmark identifiers must be unique and contiguous from zero.", actual=identifiers,
                                related_paths=[o.path for o in landmarks])

    def check_discipline(self):
        discipline = self.track.get("discipline")
        if discipline is None:
            self.reporter.result.checks.append(self.inapplicable("discipline", "No discipline selected."))
            return
        for finding in self.geometry.schema_result.diagnostics:
            if finding.code == "schema.rule_constraint" and "/$defs/disciplines/" in (finding.schema_ref or "") and not self.ignored_rule(finding):
                self.reporter.issue("discipline", "discipline_type", finding.path, finding.message, schema_ref=finding.schema_ref)
        try:
            counts = self.rule("disciplines/$defs/"+discipline+"/$defs/counts").value["properties"]
        except (KeyError, TypeError):
            self.reporter.block("discipline", "/discipline", "Discipline count rules are unavailable")
            return
        for key in counts:
            if key.startswith("actors."):
                continue
            kind = key.split(".")[-1]
            self.measure("discipline", "disciplines/$defs/"+discipline+"/$defs/counts/properties/"+key,
                         sum(o.kind == kind for o in self.objects), "/sections" if kind == "static_obstacle" else "/features",
                         f"Discipline requires a different number of {kind} objects.")


def validate_regulations(track=None, *, geometry=None, bundle=None, source_uri=None, skip=(), check_schema=False):
    """@brief Check competition rules, reusing a GeometryResult when supplied.

    @param track Authored or expanded track accepted by validate_geometry.
    @param geometry Matching GeometryResult to reuse in-memory calculations.
    @param skip Regulation check groups to omit.
    @return StageResult with rule measurements, sources and prerequisite outcomes.
    """
    if geometry is None:
        geometry = validate_geometry(track, bundle=bundle, source_uri=source_uri, check_schema=check_schema)
    result = StageResult("regulations", diagnostics=[d for d in geometry.diagnostics if d.code != "schema.rule_constraint"],
                         prerequisites=[{"stage": "schema", "valid": geometry.schema_result.valid},
                                        {"stage": "geometry", "valid": geometry.valid}])
    reporter = Reporter(result, geometry.expansion, CHECKS, skip)
    if geometry.model is None:
        for group in CHECKS:
            reporter.block(group, "", "Geometry preparation is unavailable")
        return reporter.finish()
    checks = Regulations(geometry, reporter)
    for group in CHECKS:
        if group in reporter.skip:
            continue
        try:
            getattr(checks, "check_"+group)()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            reporter.block(group, "", f"Required geometry or rule input is unavailable: {exc}")
    special = {c.code: c for c in result.checks}
    result.checks.clear()
    reporter.finish()
    result.checks = [special.get(c.code, c) if c.status == "passed" else c for c in result.checks]
    return result


def main(argv=None):
    return run_cli("regulations", validate_regulations, CHECKS, argv)


if __name__ == "__main__":
    raise SystemExit(main())
