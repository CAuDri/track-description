#!/usr/bin/env python3
"""This module expands a CAuDri track description into a fully equivalent expanded track, applying defaults, constants and catalog-based variants.

Addional diagnostics are collected for any missing or conflicting values, and the provenance of each value is recorded. (i.e. whether it was manually defined or derived from a default).

Run this file directly, or import expand_track/load_track.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urldefrag, urljoin, unquote, urlparse
from urllib.request import urlopen

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

__all__ = ["Diagnostic", "Document", "ExpansionResult", "InputError", "SchemaBundle",
           "expand_track", "load_track"]


### File input/output and source locations

def pointer(parts) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def child(path: str, key) -> str:
    return path + pointer([key])


class InputError(ValueError):
    """Input could not be represented faithfully as JSON-compatible data."""


class TrackLoader(yaml.SafeLoader):
    pass


# Keep yes/no/on/off and dates as strings; recognize true/false as booleans.
TrackLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in rules
          if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}]
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
TrackLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF")
)

# Accept scientific notation such as 1e3 in YAML.
TrackLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][-+]?[0-9]+$"),
    list("-+0123456789.")
)

# Require unique string keys so every input value is unambiguous.
def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise InputError(f"{key_node.start_mark}: mapping keys must be strings")
        if key in result:
            raise InputError(f"{key_node.start_mark}: duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result

TrackLoader.add_constructor("tag:yaml.org,2002:map", _mapping)

# Expansion expects an acyclic tree of JSON-compatible values.
def assert_json(value, path="", ancestors=frozenset()):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise InputError(f"{path or '/'}: recursive YAML aliases are not supported")
        ancestors = ancestors | {id(value)}
        for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
            if isinstance(value, dict) and not isinstance(key, str):
                raise InputError(f"{path or '/'}: mapping keys must be strings")
            assert_json(item, child(path, key), ancestors)
        return
    raise InputError(f"{path or '/'}: expected JSON-compatible data, got {value!r}")


@dataclass
class Document:
    """@brief Parsed JSON-compatible data with source locations by JSON Pointer."""

    data: dict
    uri: str
    locations: dict[str, dict] = field(default_factory=dict)


def as_uri(path_or_uri) -> str:
    value = str(path_or_uri)
    return value if urlparse(value).scheme else Path(value).resolve().as_uri()

def json_document(text: str, uri: str) -> Document:
    """Parse a JSON document and record source locations for each value."""
    def reject_constant(value):
        raise InputError(f"{uri}: {value} is not a JSON number")

    data = json.loads(text, parse_constant=reject_constant)
    assert_json(data)
    if not isinstance(data, dict):
        raise InputError(f"{uri}: the document must be an object")

    # Syntax is already checked. Walk the tokens only for source locations and
    # duplicate keys, including differently escaped spellings of the same key.
    tokens = iter(re.finditer(r'"(?:[^"\\]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,]+', text))
    line_starts = [0] + [match.end() for match in re.finditer("\n", text)]
    locations = {}

    def location(token):
        line = bisect_right(line_starts, token.start())
        return {"uri": uri, "line": line, "column": token.start() - line_starts[line - 1] + 1}

    def visit(token, path=""):
        locations[path] = location(token)
        if token.group() == "{":
            member = next(tokens)
            keys = set()
            while member.group() != "}":
                key = json.loads(member.group())
                if key in keys:
                    source = location(member)
                    raise InputError(f"{uri}:{source['line']}:{source['column']}: duplicate key {key!r}")
                keys.add(key)
                next(tokens)  # colon
                visit(next(tokens), child(path, key))
                member = next(tokens)
                if member.group() == ",":
                    member = next(tokens)
        elif token.group() == "[":
            item, index = next(tokens), 0
            while item.group() != "]":
                visit(item, child(path, index))
                index += 1
                item = next(tokens)
                if item.group() == ",":
                    item = next(tokens)

    visit(next(tokens))
    return Document(data, uri, locations)


def read_document(path_or_uri) -> Document:
    uri = as_uri(path_or_uri)
    parsed = urlparse(uri)

    # The CAuDri JSON Schema can be referenced from a local file or a web address.
    # TODO: Some kind of sanity/security check for remote resources?
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise InputError(f"Unsupported file URI host: {parsed.netloc}")
        text = Path(unquote(parsed.path)).read_text(encoding="utf-8")
    elif parsed.scheme in {"http", "https"}:
        with urlopen(uri, timeout=15) as response:
            text = response.read().decode("utf-8")
    else:
        raise InputError(f"Unsupported document URI: {uri}")
    try:
        return json_document(text, uri)
    except json.JSONDecodeError as exc:
        # A .json file must be strict JSON. Elsewhere accept either notation,
        # including JSON content in a .yaml file or an extensionless resource.
        if unquote(parsed.path).lower().endswith(".json"):
            raise InputError(f"{uri}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}") from exc
    loader = TrackLoader(text)
    try:
        # Check 
        node = loader.get_single_node()
        if node is None:
            raise InputError(f"{uri}: empty document")
        data = loader.construct_document(node)
        assert_json(data)
        if not isinstance(data, dict):
            raise InputError(f"{uri}: the document must be an object")
        locations = {}

        def visit(current, path="", ancestors=frozenset()):
            if id(current) in ancestors:
                return
            locations[path] = {"uri": uri, "line": current.start_mark.line + 1,
                               "column": current.start_mark.column + 1}
            ancestors = ancestors | {id(current)}
            if isinstance(current, yaml.MappingNode):
                for key, value in current.value:
                    visit(value, child(path, key.value), ancestors)
            elif isinstance(current, yaml.SequenceNode):
                for i, value in enumerate(current.value):
                    visit(value, child(path, i), ancestors)

        visit(node)
        return Document(data, uri, locations)
    except yaml.YAMLError as exc:
        raise InputError(f"{uri}: {exc}") from exc
    finally:
        loader.dispose()


def load_track(path_or_uri) -> Document:
    """@brief Read track description from a local file or URI as JSON or YAML.

    @param path_or_uri Local path or file/HTTP(S) URI.
    @return Document retaining source locations for later diagnostics.
    @throws InputError If the input cannot represent unambiguous JSON data.
    """
    return read_document(path_or_uri)


def check_track_header(data):
    """Check the header fields needed to identify and expand a track."""
    if not isinstance(data, dict):
        raise InputError("A Track Description must be an object")

    for key in ("$schema", "id", "root", "sections"):
        if key not in data:
            raise InputError(f"/{key}: missing Track Description header field")

    for key in ("$schema", "id"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise InputError(f"/{key}: expected a non-empty string in the track header")

    if not isinstance(data["root"], dict):
        raise InputError("/root: expected a section/port reference object")
    for key in ("section", "port"):
        if key == "port" and key not in data["root"]:
            continue  # The root section determines the omitted port during expansion.
        if not isinstance(data["root"].get(key), str) or not data["root"][key]:
            raise InputError(f"/root/{key}: expected a non-empty reference in the track header")

    if not isinstance(data["sections"], dict) or not data["sections"]:
        raise InputError("/sections: expansion needs a non-empty section mapping")


def source_location(document, path, provenance=None):
    """Find the authored location of an effective value, falling back to its owner."""
    if document is None:
        return None
    provenance = provenance or {}
    ancestor = path
    while ancestor not in provenance and ancestor:
        ancestor = ancestor.rsplit("/", 1)[0]
    if ancestor in provenance:
        origin = provenance[ancestor].get("source_path", ancestor)
        path = origin + path[len(ancestor):]
    while path not in document.locations and path:
        path = path.rsplit("/", 1)[0]
    return document.locations.get(path)


def leaves(value, path=""):
    if isinstance(value, (dict, list)) and value:
        for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
            yield from leaves(item, child(path, key))
    else:
        yield path, value


### Schema bundle and expansion

@dataclass(frozen=True)
class Node:
    """@brief A subschema together with its resource URI and JSON Pointer."""

    value: object
    uri: str
    path: str = ""

    @property
    def ref(self):
        return self.uri + "#" + self.path

    def sub(self, key):
        return Node(self.value[key], self.uri, child(self.path, key))


class SchemaBundle:
    """@brief Load the selected schema/catalog bundle and resolve default recipes."""
    def __init__(self, schema_uri):
        self.main_uri = as_uri(schema_uri)
        self.documents: dict[str, Document] = {}
        self.origins = {}
        self._registry = Registry(retrieve=self._retrieve)
        self._load(self.main_uri)
        metadata = self.documents[self.main_uri].data.get("x-caudri", {})
        if not isinstance(metadata, dict) or not all(isinstance(metadata.get(k), str) and metadata[k]
                                                     for k in ("catalog", "regulations")):
            raise InputError("The entry schema does not identify a CAuDri catalog/regulations bundle")
        main_base = urljoin(self.main_uri, self.documents[self.main_uri].data.get("$id", self.main_uri))
        self.catalog_uri = urljoin(main_base, metadata["catalog"])
        self.regulations_uri = urljoin(main_base, metadata["regulations"])
        self._load(self.catalog_uri)
        self._load(self.regulations_uri)

    @classmethod
    def for_track(cls, document: Document):
        """@brief Resolve the track's selected bundle relative to its source URI."""
        reference = document.data.get("$schema")
        if not isinstance(reference, str) or not reference:
            raise InputError("A Track Description needs a non-empty $schema reference")
        return cls(urljoin(document.uri, reference))

    def _load(self, uri):
        if uri in self.documents:
            return
        doc = read_document(uri)
        if doc.data.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise InputError(f"{uri}: expected JSON Schema Draft 2020-12")
        self.documents[uri] = doc

        def index(value, path=""):
            if isinstance(value, dict):
                if path and "$id" in value:
                    raise InputError(f"{uri}#{path}: nested $id resources are not supported by this expander")
                self.origins[id(value)] = (uri, path)
                for key, item in value.items():
                    index(item, child(path, key))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    index(item, child(path, i))

        index(doc.data)
        resource_data = dict(doc.data, **{"$id": urljoin(uri, doc.data.get("$id", uri))})
        resource = Resource(resource_data, specification=DRAFT202012)
        self._registry = self._registry.with_resource(uri, resource)
        canonical = resource_data["$id"]
        self._registry = self._registry.with_resource(canonical, resource)
        self.documents.setdefault(canonical, doc)

    def _retrieve(self, uri):
        self._load(uri)
        data = self.documents[uri].data
        return Resource(dict(data, **{"$id": urljoin(uri, data.get("$id", uri))}),
                        specification=DRAFT202012)

    def resolve(self, ref, base=None):
        """@brief Resolve a native JSON Pointer reference to a schema node."""
        full = urljoin(base or self.main_uri, ref)
        uri, fragment = urldefrag(full)
        self._load(uri)
        data = self.documents[uri].data
        path = unquote(fragment)
        if path and not path.startswith("/"):
            raise InputError(f"{full}: use JSON Pointer references, not schema anchors")
        try:
            for segment in path.split("/")[1:]:
                key = segment.replace("~1", "/").replace("~0", "~")
                data = data[int(key)] if isinstance(data, list) else data[key]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise InputError(f"Unresolved schema reference: {full}") from exc
        return Node(data, uri, path)

    def catalog(self, path):
        return self.resolve("#/$defs/" + path, self.catalog_uri)

    def validator(self, node):
        return Draft202012Validator({"$ref": node.ref}, registry=self._registry)

    def matches(self, value, node):
        """Evaluate a variant predicate."""
        return self.validator(node).is_valid(value)

    def active(self, node, value, stack=()):
        """Collect applicable schema branches for the given value."""
        if not isinstance(node.value, dict):
            return []
        if node.ref in stack:
            raise InputError(f"Cyclic schema composition at {node.ref}")
        stack = (*stack, node.ref)
        schema = node.value
        result = [node]
        if "$ref" in schema:
            base = urljoin(node.uri, self.documents[node.uri].data.get("$id", node.uri))
            result += self.active(self.resolve(schema["$ref"], base), value, stack)
        for i in range(len(schema.get("allOf", []))):
            result += self.active(node.sub("allOf").sub(i), value, stack)
        if "if" in schema:
            branch = "then" if self.matches(value, node.sub("if")) else "else"
            if branch in schema:
                result += self.active(node.sub(branch), value, stack)
        for keyword in ("oneOf", "anyOf"):
            if keyword not in schema:
                continue
            result += self._alternative_nodes(node.sub(keyword), value, stack)
        return list({n.ref: n for n in result}.values())

    def _alternative_nodes(self, alternatives, value, stack):
        """Select a branch by its discriminator, required keys or structural shape."""
        branches = [alternatives.sub(i) for i in range(len(alternatives.value))]
        candidates = {}

        def nodes_for(branch):
            if branch.ref not in candidates:
                candidates[branch.ref] = self.active(branch, value, stack)
            return candidates[branch.ref]

        selected = []
        if isinstance(value, dict) and "type" in value:
            selected = [branch for branch in branches if any(
                n.value.get("properties", {}).get("type", {}).get("const") == value["type"]
                for n in nodes_for(branch))]
        if not selected and isinstance(value, dict):
            # Position/range unions can be selected from their required keys.
            selected = [branch for branch in branches
                        if isinstance(branch.value, dict) and branch.value.get("required")
                        and set(branch.value["required"]) <= value.keys()]
        if not selected:
            selected = [branch for branch in branches
                        if all(self._shape_matches(value, n.value) for n in nodes_for(branch))]
        # Defaults require an unambiguous branch selection.
        return nodes_for(selected[0]) if len(selected) == 1 else []

    def applicable(self, schemas, value):
        """Collect applicable conjuncts once, even when references share a target."""
        return list({n.ref: n for schema in schemas for n in self.active(schema, value)}.values())

    @staticmethod
    def _shape_matches(value, schema):
        types = schema.get("type")
        if types is not None:
            types = [types] if isinstance(types, str) else types
            if not any(Draft202012Validator.TYPE_CHECKER.is_type(value, kind) for kind in types):
                return False
        if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
            return False
        if isinstance(value, dict):
            if not set(schema.get("required", [])) <= value.keys():
                return False
            discriminator = schema.get("properties", {}).get("type", {}).get("const")
            if discriminator is not None and value.get("type") != discriminator:
                return False
        return True


# Track expansion and diagnostics
# -------------------------------

MISSING = object()
COLLECTIONS = {"sections", "elements", "features", "zones", "actors", "spots",
               "parallel_parking", "perpendicular_parking"}
CONTAINERS = {"parameters", "rendering", "simulation", "road", "faces", "qr_profiles", "behavior"}


def merge(base, override):
    """Object members merge recursively; every other value replaces its base."""
    if isinstance(base, dict) and isinstance(override, dict):
        result = deepcopy(base)
        for key, value in override.items():
            result[key] = merge(result[key], value) if key in result else deepcopy(value)
        return result
    return deepcopy(override)


@dataclass
class Diagnostic:
    """@brief Machine-readable finding with an effective path and authored origin."""

    code: str
    severity: str
    path: str
    message: str
    source: dict | None = None
    schema_ref: str | None = None
    details: dict = field(default_factory=dict)

    def format(self, verbose=False) -> str:
        """@return Human-readable finding, optionally with authored/schema locations."""
        lines = [f"{self.severity.upper()} {self.code} {self.path or '/'}: {self.message}"]
        if verbose:
            if self.source:
                lines.append(f"  {self.source['uri']}:{self.source['line']}:{self.source['column']}")
            if self.schema_ref:
                lines.append(f"  Schema: {self.schema_ref}")
        return "\n".join(lines)


@dataclass
class ExpansionResult:
    """@brief Resolved snapshot, provenance and expansion diagnostics."""

    track: dict
    provenance: dict
    diagnostics: list[Diagnostic]
    source: Document | None = field(default=None, repr=False)

    @property
    def complete(self):
        """@return Whether all applicable expansion operations could be performed."""
        return not any(d.severity == "error" for d in self.diagnostics)

    def report(self):
        """@return JSON-compatible technical diagnostics and provenance, separate from the track."""
        return {"complete": self.complete,
                "diagnostics": [asdict(d) for d in self.diagnostics],
                "provenance": self.provenance}


class Expander:
    def __init__(self, document, bundle):
        self.document = document
        self.bundle = bundle
        # Copy YAML aliases into independent objects for each section/feature ID.
        self.track = json.loads(json.dumps(document.data, allow_nan=False))
        self.provenance = {path: {"kind": "explicit", "source_path": path}
                           for path, _ in leaves(document.data)}
        self.diagnostics = []
        self._diagnostic_keys = set()
        self.asset_base = None

    def issue(self, code, path, message, severity="error", schema_ref=None, **details):
        key = (code, path, message, schema_ref)
        if key in self._diagnostic_keys:
            return
        self._diagnostic_keys.add(key)
        source = source_location(self.document, path, self.provenance)
        self.diagnostics.append(Diagnostic(code, severity, path, message, source, schema_ref, details))

    def record(self, value, path, kind, source_path=None, **details):
        for leaf_path, _ in leaves(value, path):
            if leaf_path not in self.provenance:
                origin = source_path if source_path is not None else path
                if kind == "inherited":
                    origin += leaf_path[len(path):]
                self.provenance[leaf_path] = {
                    "kind": kind, "source_path": origin, **details}

    def derived(self, obj, key, value, path, source_path, **details):
        if key not in obj:
            obj[key] = deepcopy(value)
            self.record(value, child(path, key), "derived", source_path, **details)

    def fill(self, value, schemas, path, *, container=False):
        """Collect only applicable defaults/prescribed constants, preserving input."""
        probe = {} if value is MISSING else value
        nodes = self.bundle.applicable(schemas, probe)
        if value is MISSING:
            choices = [(n.value["default"], n, "default") for n in nodes if "default" in n.value]
            if not choices:
                choices = [(n.value["const"], n, "prescribed") for n in nodes if "const" in n.value]
            if choices:
                fingerprints = {json.dumps(v, sort_keys=True) for v, _, _ in choices}
                if len(fingerprints) != 1:
                    self.issue("expansion.conflicting_defaults", path,
                               "Applicable schema branches provide different fallback values.",
                               candidates=[{"value": v, "schema_ref": n.ref} for v, n, _ in choices])
                    return MISSING
                value, node, kind = choices[0]
                value = deepcopy(value)
                self.record(value, path, kind, schema_ref=node.ref)
            elif container:
                value = {}
            else:
                return MISSING
        if isinstance(value, list):
            result = []
            for i, item in enumerate(value):
                item_schemas = []
                for n in nodes:
                    if i < len(n.value.get("prefixItems", [])):
                        item_schemas.append(n.sub("prefixItems").sub(i))
                    elif isinstance(n.value.get("items"), dict):
                        item_schemas.append(n.sub("items"))
                result.append(self.fill(item, item_schemas, child(path, i)))
            return result
        if not isinstance(value, dict):
            return value
        result = deepcopy(value)

        for _ in range(8):
            before = deepcopy(result)
            nodes = self.bundle.applicable(schemas, result)
            properties = {}
            for node in nodes:
                for key in node.value.get("properties", {}):
                    properties.setdefault(key, []).append(node.sub("properties").sub(key))
            for key in set(result) - properties.keys():
                candidates = [n.sub("additionalProperties") for n in nodes
                              if isinstance(n.value.get("additionalProperties"), dict)]
                if candidates:
                    properties[key] = candidates
            for key, child_schemas in properties.items():
                if key in COLLECTIONS:
                    continue
                item = self.fill(result.get(key, MISSING), child_schemas, child(path, key),
                                 container=key in CONTAINERS or path.endswith("/qr_profiles"))
                if item is not MISSING and (item != {} or key in result or key == "parameters"):
                    result[key] = item
            if before == result:
                break
        else:
            self.issue("expansion.unstable_defaults", path, "Default application did not converge.")
        return result

    def typed(self, category, obj, path):
        if not isinstance(obj, dict):
            self.issue("expansion.object_expected", path, "Expected an object with a catalog type.")
            return None
        kind = obj.get("type")
        definitions = self.bundle.catalog(category).value.get("$defs", {})
        if not isinstance(kind, str) or kind not in definitions or kind in {"patch", "parameters"}:
            self.issue("expansion.unknown_type", child(path, "type"),
                       f"Unknown {category} type: {kind!r}.")
            return None
        return self.bundle.catalog(category + "/$defs/" + kind)

    def generate(self, owner, path, recipes, bindings=None):
        if not recipes:
            return
        slots = owner.setdefault("features", {})
        if not isinstance(slots, dict):
            self.issue("expansion.slots_expected", child(path, "features"), "Feature slots must be an object.")
            return
        for slot, recipe in recipes.items():
            slot_path = child(child(path, "features"), slot)
            patch = slots.get(slot, {})
            if patch is False:
                continue
            if not isinstance(patch, dict):
                self.issue("expansion.patch_expected", slot_path, "A slot needs an object or false.")
                continue
            base = {key: deepcopy(value) for key, value in recipe.items()
                    if key not in {"parameters_from", "when"}}
            if not isinstance(patch.get("parameters", {}), dict):
                self.issue("expansion.parameters_expected", child(slot_path, "parameters"),
                           "Feature parameters must be an object.")
                slots[slot] = merge(base, patch)
                continue
            for parameter, specification in recipe.get("parameters_from", {}).items():
                # Explicit patches take precedence over derived recipe values.
                if parameter in patch.get("parameters", {}):
                    continue
                reference = specification if isinstance(specification, str) else specification["path"]
                source = owner
                source_path = path
                try:
                    for segment in reference.removeprefix("owner.").split("."):
                        source = source[segment]
                        source_path = child(source_path, segment)
                    if isinstance(specification, dict):
                        if (specification.get("transform") != "absolute"
                                or not isinstance(source, (float, int)) or isinstance(source, bool)):
                            raise ValueError("unsupported value transform")
                        source = abs(source)
                    base.setdefault("parameters", {})[parameter] = deepcopy(source)
                    self.record(source, child(child(slot_path, "parameters"), parameter),
                                "derived", source_path)
                except (KeyError, TypeError, ValueError):
                    self.issue("expansion.recipe_input_missing", slot_path,
                               f"Cannot derive {parameter!r} from {reference!r}.")
            slots[slot] = merge(base, patch)
            self.record(base, slot_path, "generated", path, slot=slot)
            if bindings and slot in bindings:
                self.provenance.setdefault(slot_path, {"kind": "generated", "source_path": path})[
                    "binding"] = bindings[slot]

    def features(self, owner, path):
        slots = owner.get("features", {})
        if not isinstance(slots, dict):
            self.issue("expansion.slots_expected", child(path, "features"), "Feature slots must be an object.")
            return
        for key, value in slots.items():
            if value is False:
                continue
            feature_path = child(child(path, "features"), key)
            node = self.typed("features", value, feature_path)
            if node is None:
                continue
            value = self.fill(value, [node], feature_path)
            params = value.get("parameters", {})
            if not isinstance(params, dict):
                slots[key] = value
                continue
            kind = value.get("type")
            if kind == "traffic_sign":
                sign = params.get("sign")
                variant = (node.value.get("x-caudri", {}).get("variants", {}).get(sign)
                           if isinstance(sign, str) else None)
                if not variant:
                    self.issue("expansion.unknown_sign", child(child(feature_path, "parameters"), "sign"),
                               f"No catalog variant for traffic sign {params.get('sign')!r}.")
                else:
                    rendering = value.setdefault("rendering", {})
                    if isinstance(rendering, dict):
                        self.derived(rendering, "asset", variant["asset"], child(feature_path, "rendering"),
                                     feature_path, schema_ref=node.ref + "/x-caudri/variants/" + params["sign"])
            elif kind == "surface_marking":
                variants = node.value.get("$defs", {}).get("rendering", {}).get("$defs", {})
                if not isinstance(params.get("marking"), str) or params["marking"] not in variants:
                    self.issue("expansion.unknown_marking", child(child(feature_path, "parameters"), "marking"),
                               f"No rendering variant for {params.get('marking')!r}.")
            elif kind == "landmark" and "identifier" in params:
                rendering = value.setdefault("rendering", {})
                if isinstance(rendering, dict):
                    self.derived(rendering, "qr_payload", str(params["identifier"]),
                                 child(feature_path, "rendering"), child(child(feature_path, "parameters"), "identifier"))
            self.resolve_asset(value, feature_path)
            slots[key] = value

    def resolve_asset(self, obj, path):
        rendering = obj.get("rendering", {})
        if not isinstance(rendering, dict):
            return
        asset = rendering.get("asset")
        if not isinstance(asset, str):
            return
        params = obj.get("parameters", {})
        try:
            asset = asset.format_map(params)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            self.issue("expansion.asset_parameter_missing", child(child(path, "rendering"), "asset"),
                       f"Cannot resolve asset template: {exc}.")
            return
        rendering["asset"] = urljoin(self.asset_base, asset)

    def intersection(self, obj, node, path):
        params = obj.get("parameters", {})
        if not isinstance(params, dict):
            self.issue("expansion.recipe_input_missing", child(path, "parameters"),
                       "Cannot select intersection feature recipes without a parameter object.")
            return {}
        metadata = node.value.get("x-caudri", {})
        layout = params.get("layout")
        variant = metadata.get("variants", {}).get(layout) if isinstance(layout, str) else None
        if variant is None:
            self.issue("expansion.unknown_intersection_layout", path,
                       f"No expansion recipe for layout {params.get('layout')!r}.")
            return {}
        optional_ports = params.get("ports", [])
        if not isinstance(optional_ports, list) or any(not isinstance(p, str) or p not in {"left", "straight", "right"}
                                                      for p in optional_ports):
            self.issue("expansion.intersection_ports", path, "Cannot expand the specified intersection arms.")
            return {}
        ports = ["entry", *optional_ports]
        controlled = params.get("controlled_ports", [])
        if not isinstance(controlled, list) or any(p not in ports for p in controlled):
            self.issue("expansion.controlled_port_missing", path,
                       "A controlled approach must be an available intersection port.")
            return {}
        recipes, bindings = {}, {}
        for port in ports:
            group = "controlled" if port in controlled else "uncontrolled"
            template = variant.get("expansion", {}).get("all",
                        variant.get("expansion", {}).get(group, {}))
            for name, recipe in template.items():
                slot = f"{port}_{name}"
                recipes[slot] = recipe
                bindings[slot] = {"kind": "intersection_approach", "port": port, "role": name}
        turn = params.get("mandatory_turn")
        if turn is not None:
            if (not isinstance(turn, list) or len(turn) != 2 or not all(isinstance(p, str) for p in turn) or len(set(turn)) != 2
                    or any(p not in ports for p in turn)):
                self.issue("expansion.mandatory_turn", path, "A mandatory turn needs two different available ports.")
            else:
                ring = ["entry", "right", "straight", "left"]
                template = metadata["mandatory_direction"]["expansion"]
                for source, target in (turn, turn[::-1]):
                    delta = (ring.index(target) - ring.index(source)) % 4
                    if delta not in {1, 3}:
                        self.issue("expansion.mandatory_turn", path,
                                   "Opposite arms do not specify a left/right mandatory turn.")
                        break
                    direction = "right" if delta == 1 else "left"
                    sign_slot, marking_slot = f"{source}_mandatory_sign", f"{source}_mandatory_marking"
                    recipes[sign_slot] = {"type": "traffic_sign", "parameters": {
                        "sign": template["sign_by_turn"][direction]}}
                    recipes[marking_slot] = {"type": "surface_marking", "parameters": {
                        "marking": template["surface_marking"], "target": direction}}
                    for slot in (sign_slot, marking_slot):
                        bindings[slot] = {"kind": "intersection_turn", "port": source, "target": target}
        self.generate(obj, path, recipes, bindings)
        return recipes

    def zones(self, owner, path, *, global_scope=False):
        zones = owner.get("zones", {})
        if not isinstance(zones, dict):
            self.issue("expansion.zones_expected", child(path, "zones"), "Zones must be an object.")
            return
        for key, value in zones.items():
            zone_path = child(child(path, "zones"), key)
            node = self.typed("zones", value, zone_path)
            if node is None:
                continue
            value = self.fill(value, [node], zone_path)
            recipes = deepcopy(node.value.get("x-caudri", {}).get("expansion", {}))
            params = value.get("parameters", {})
            bindings = {}
            for slot in recipes:
                endpoint = "start" if slot.startswith("begin_") else "end"
                if value["type"] == "grade":
                    # Keep a semantic uphill/downhill approach binding. Geometry
                    # consumers use the signed grade and direction to place it.
                    bindings[slot] = {"kind": "grade_approach", "role": slot}
                else:
                    bindings[slot] = {"kind": "zone_endpoint", "endpoint": endpoint,
                                      "scope": "span" if global_scope else "range"}
                if isinstance(params, dict) and value["type"] != "grade":
                    recipes[slot].setdefault("parameters", {})["faces"] = params.get("direction", "forward")
                    if recipes[slot]["type"] != "traffic_sign":
                        recipes[slot]["parameters"].pop("faces", None)
            self.generate(value, zone_path, recipes, bindings)
            self.features(value, zone_path)
            zones[key] = value

    def parking(self, obj, path):
        params = obj.get("parameters", {})
        if not isinstance(params, dict):
            return
        for name, typename in (("parallel_parking", "parallel_parking_lot"),
                               ("perpendicular_parking", "perpendicular_parking_lot")):
            lot = params.get(name)
            if lot is None or lot is False:
                continue
            lot_path = child(child(path, "parameters"), name)
            if not isinstance(lot, dict):
                self.issue("expansion.parking_lot", lot_path, "A parking lot needs an object or false.")
                continue
            node = self.bundle.catalog("elements/$defs/parking_area/$defs/" + typename)
            lot = self.fill(lot, [node], lot_path)
            interval = params.get("range", {})
            if "extent_along_road" not in lot:
                if isinstance(interval, dict) and all(isinstance(n, (int, float)) and not isinstance(n, bool)
                        for n in (interval.get("start"), interval.get("end"), lot.get("distance"))):
                    self.derived(lot, "extent_along_road", interval["end"] - interval["start"] - lot["distance"],
                                 lot_path, child(child(path, "parameters"), "range"))
                else:
                    self.issue("expansion.recipe_input_missing", child(lot_path, "extent_along_road"),
                               "Cannot derive parking-lot extent without numeric range endpoints and lot distance.")
            spots = lot.get("spots", {})
            if not isinstance(spots, dict):
                self.issue("expansion.parking_spots", child(lot_path, "spots"), "Parking spots must be an object.")
                params[name] = lot
                continue
            spot_node = self.bundle.catalog("elements/$defs/parking_area/$defs/parking_spots").sub("additionalProperties")
            for slot, spot in spots.items():
                spot_path = child(child(lot_path, "spots"), slot)
                if not isinstance(spot, dict):
                    self.issue("expansion.parking_spot", spot_path, "A parking spot must be an object.")
                    continue
                if "extent_lateral" in lot:
                    self.derived(spot, "extent_lateral", lot["extent_lateral"], spot_path,
                                 child(lot_path, "extent_lateral"))
                spot = self.fill(spot, [spot_node], spot_path)
                recipes = {}
                if spot.get("state") == "blocked":
                    recipe = spot_node.value.get("x-caudri", {}).get("expansion", {}).get("obstacle", {})
                    recipes = {"obstacle": recipe}
                    self.generate(spot, spot_path, recipes,
                                  {"obstacle": {"kind": "parking_spot", "lot": name}})
                self.features(spot, spot_path)
                spots[slot] = spot
            self.features(lot, lot_path)
            params[name] = lot

    def elements(self, section, path):
        elements = section.get("elements", {})
        if not isinstance(elements, dict):
            self.issue("expansion.elements_expected", child(path, "elements"), "Elements must be an object.")
            return
        for key, value in elements.items():
            if value is False:
                continue
            element_path = child(child(path, "elements"), key)
            node = self.typed("elements", value, element_path)
            if node is None:
                continue
            value = self.fill(value, [node], element_path)
            recipes = node.value.get("x-caudri", {}).get("expansion", {})
            self.generate(value, element_path, recipes,
                          {slot: {"kind": "element_slot", "role": slot} for slot in recipes})
            self.features(value, element_path)
            if value.get("type") == "parking_area":
                self.parking(value, element_path)
            elements[key] = value

    def asset_settings(self):
        """Resolve the shared asset base before expanding object asset paths."""
        rendering = self.track.get("rendering", {})
        if not isinstance(rendering, dict) or not isinstance(rendering.get("asset_path"), str):
            self.issue("expansion.asset_base", "/rendering", "Cannot determine the catalog asset base.")
            self.asset_base = self.bundle.catalog_uri
        else:
            self.asset_base = urljoin(self.bundle.catalog_uri, rendering["asset_path"])
            if not self.asset_base.endswith("/"):
                self.asset_base += "/"
            rendering["asset_path"] = self.asset_base

    def root_exit_connected(self):
        """Return whether another section is attached to the selected root exit."""
        root_section = self.track["root"]["section"]
        endpoint = root_section + ".exit"
        connections = self.track.get("connections", [])
        if not isinstance(connections, list):
            return False
        for pair in connections:
            if not isinstance(pair, list) or len(pair) != 2 or endpoint not in pair:
                continue
            if any(isinstance(other, str) and other.rsplit(".", 1)[0] != root_section
                   for other in pair):
                return True
        return False

    def root_elements(self, section, path):
        """Materialize enabled root slots, preserving explicit patches and false."""
        elements = section.setdefault("elements", {})
        if not isinstance(elements, dict):
            return  # The elements pass reports the unusable container.
        parameters = section.get("parameters", {})
        parameters = parameters if isinstance(parameters, dict) else {}
        for kind, definition in self.bundle.catalog("elements").value["$defs"].items():
            default = definition.get("x-caudri", {}).get("defaults", {})
            if section["type"] not in default.get("section_types", [section["type"]]):
                continue
            if "direction" in default and parameters.get("direction") != default["direction"]:
                continue
            if default.get("connected_root_exit") and self.track["root"]["port"] == "exit":
                # An implicit line needs actual road beyond the root. Explicit
                # slots are retained and validated even when their support is missing.
                if not self.root_exit_connected():
                    continue
            if default.get("enabled"):
                slot = default["slot"]
                if slot not in elements:
                    elements[slot] = {"type": kind}
                    self.record(elements[slot], child(child(path, "elements"), slot),
                                "generated", "/root", slot=slot)

    def sections(self):
        """Inherit road settings, then expand each section and its owned objects."""
        sections = self.track["sections"]
        root_section = self.track["root"]["section"]
        if root_section not in sections:
            self.issue("expansion.root_section_missing", "/root", "Cannot generate the root elements: the referenced section does not exist.")
        road = self.track.get("road", {})
        if not isinstance(road, dict):
            self.issue("expansion.inheritance_source", "/road",
                       "Cannot inherit section road settings from a non-object value.")
        for key, section in sections.items():
            path = child("/sections", key)
            node = self.typed("sections", section, path)
            if node is None:
                continue
            if isinstance(road, dict):
                inherited = merge(road, section.get("road", {}))
                self.record(road, child(path, "road"), "inherited", "/road")
                section["road"] = inherited
            section = self.fill(section, [node], path)
            if key == root_section:
                if "port" not in self.document.data["root"]:
                    overrides = node.value.get("x-caudri", {}).get("root_port_by_direction", {})
                    parameters = section.get("parameters", {})
                    direction = parameters.get("direction") if isinstance(parameters, dict) else None
                    port = overrides.get(direction) if isinstance(direction, str) else None
                    if port is not None:
                        self.track["root"]["port"] = port
                        self.provenance.pop("/root/port", None)
                        self.record(port, "/root/port", "derived", path + "/parameters/direction",
                                    schema_ref=node.ref + "/x-caudri/root_port_by_direction")
                self.root_elements(section, path)
            if section["type"] == "intersection":
                self.intersection(section, node, path)
            self.features(section, path)
            self.elements(section, path)
            self.zones(section, path)
            sections[key] = section

    def actors(self):
        actors = self.track.get("actors", {})
        if isinstance(actors, dict):
            for key, value in actors.items():
                path = child("/actors", key)
                node = self.typed("actors", value, path)
                if node is not None:
                    value = self.fill(value, [node], path)
                    self.resolve_asset(value, path)
                    actors[key] = value

    def run(self):
        root_node = self.bundle.resolve(self.bundle.main_uri)
        self.track = self.fill(self.track, [root_node], "")
        self.track["$schema"] = self.bundle.main_uri
        self.asset_settings()
        self.sections()
        self.features(self.track, "")
        self.zones(self.track, "", global_scope=True)
        self.actors()
        return ExpansionResult(self.track, self.provenance, self.diagnostics, self.document)


def prepare_track(track, *, bundle=None, source_uri=None) -> Document:
    """Normalize input and check its header before loading resources."""
    # Apply the same input contract to parsed documents and in-memory dictionaries.
    data = track.data if isinstance(track, Document) else track
    assert_json(data)
    check_track_header(data)
    if not isinstance(track, Document):
        if source_uri is None and bundle is None:
            raise InputError("Supply source_uri or bundle when expanding an in-memory dictionary")
        track = Document(data, as_uri(source_uri or bundle.main_uri))
    return track


def expand_track(track: Document | dict, *, bundle: SchemaBundle | None = None,
                 source_uri: str | None = None) -> ExpansionResult:
    """@brief Expand a copy of the track using the selected catalog recipes.

    @param track Parsed document or JSON-compatible dictionary.
    @param bundle Optional reusable catalog/schema bundle.
    @param source_uri Base URI for relative references in a plain dictionary.
    @return Snapshot, provenance and technical expansion diagnostics.
    @throws InputError If the header or required schema resources cannot be read.
    """
    document = prepare_track(track, bundle=bundle, source_uri=source_uri)
    selected = bundle or SchemaBundle.for_track(document)
    try:
        return Expander(document, selected).run()
    except Unresolvable as exc:
        raise InputError(f"Cannot resolve a schema reference: {exc}") from exc


# Command-line interface
# ----------------------

def write_file(path: Path, text: str):
    """Write through a temporary file and atomically replace the destination."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Resolve defaults and generated features in a CAuDri Track Description.")
    parser.add_argument("track", type=Path, help="source YAML/JSON Track Description")
    parser.add_argument("-o", "--output", type=Path, help="expanded output file (default: stdout)")
    parser.add_argument("--format", choices=["json", "yaml"], default="json",
                        help="output serialization (default: json; use yaml for manual inspection)")
    parser.add_argument("--report", type=Path, help="write provenance and diagnostics as a separate JSON file")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="also emit incomplete data for inspection; the exit status remains nonzero")
    parser.add_argument("-v", "--verbose", action="store_true", help="include source and schema references in messages")
    args = parser.parse_args(argv)
    paths = [p.resolve() for p in (args.track, args.output, args.report) if p is not None]
    if len(paths) != len(set(paths)):
        parser.error("Input, expanded output and report must use different files.")
    try:
        result = expand_track(load_track(args.track))
        for finding in result.diagnostics:
            print(finding.format(args.verbose), file=sys.stderr)
        if args.report:
            write_file(args.report, json.dumps(result.report(), indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        if result.complete or args.allow_incomplete:
            text = (json.dumps(result.track, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                    if args.format == "json" else yaml.safe_dump(result.track, sort_keys=False, allow_unicode=True))
            if args.output:
                write_file(args.output, text)
            else:
                sys.stdout.write(text)
        state = "complete" if result.complete else "incomplete"
        print(f"Expansion {state}.", file=sys.stderr)
        return 0 if result.complete else 1
    except (InputError, OSError, ValueError) as exc:
        print(f"ERROR expansion.input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
