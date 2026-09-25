"""Site manifest validation: JSON Schema first, then the rules a schema
cannot express (docs/ai-harness/SITE.md).

Errors are ``"<json pointer>: <message>"`` strings, so the agent can fix the
reported location with a JSON Patch on the same path.
"""

from __future__ import annotations

import functools
import json
import posixpath
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry, Resource

from ..core.errors import ValidationFailed
from ..core.ids import COMMANDABLE_TYPES, OBJECT_TYPES, PointRef
from . import nodedocs

SCHEMA_DIR = Path(__file__).parent / "schemas"
SCHEMA_BASE = "https://bacnet-uc.local/schemas/"
SITE_SCHEMA = SCHEMA_BASE + "site.schema.json"

BACNET_PROTOCOLS = ("bacnet-uc", "bacnet-ip")
STEP_KINDS = ("force", "release", "write", "wait", "expect")
MAX_IO_POINTS = 32
#: BACnet priorities 1..8 (life safety up to manual operator) outrank the
#: agent; nothing it writes or configures uses them (DESIGN.md section 10).
RESERVED_PRIORITIES = range(1, 9)
_MESSAGE_LIMIT = 240
#: Channel name prefix -> BACnet object types it may be bound to (io.schema.json).
_CHANNEL_TYPES = {
    "di": ("binary-input", "multi-state-input"),
    "do": ("binary-output", "binary-value"),
    "ai": ("analog-input",),
    "ao": ("analog-output", "analog-value"),
}
_CHANNEL_RE = re.compile(r"^(di|do|ai|ao)[0-9]+$")


@functools.cache
def registry() -> Registry:
    """Every vendored schema, keyed by its ``$id``."""
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


@functools.cache
def validator(uri: str) -> jsonschema.Draft202012Validator:
    """A validator for the schema (or schema fragment) at ``uri``."""
    return jsonschema.Draft202012Validator(
        {"$ref": uri},
        registry=registry(),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def pointer(parts: Iterable[Any]) -> str:
    """RFC 6901 JSON Pointer for a path, ``<root>`` for the document itself."""
    tokens = [str(p).replace("~", "~0").replace("/", "~1") for p in parts]
    return "/" + "/".join(tokens) if tokens else "<root>"


def _clip(text: str) -> str:
    return text if len(text) <= _MESSAGE_LIMIT else text[: _MESSAGE_LIMIT - 1] + "…"


def _discriminator(error: ValidationError) -> tuple[Sequence[Any], str] | None:
    """For a oneOf whose every branch fails a ``const`` at the same place (the
    ``kind`` of a transport): that place and a message listing the allowed values."""
    branches: dict[Any, list[ValidationError]] = {}
    for sub in error.context or []:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    consts: dict[tuple[Any, ...], list[Any]] = {}
    for subs in branches.values():
        for sub in subs:
            if sub.validator == "const":
                consts.setdefault(tuple(sub.relative_path), []).append(sub.validator_value)
    for path, values in consts.items():
        if path and len(values) == len(branches):
            found = next(s for s in error.context or [] if tuple(s.relative_path) == path)
            return tuple(found.absolute_path), f"{found.instance!r} is not one of {values!r}"
    return None


def _schema_message(error: ValidationError, locate: Callable[[Sequence[Any]], str]) -> str:
    if error.validator in ("oneOf", "anyOf") and error.context:
        found = _discriminator(error)
        if found is not None:
            return f"{locate(found[0])}: {_clip(found[1])}"
        closest = best_match(error.context)
        return (f"{locate(error.absolute_path)}: {_clip(error.message)} "
                f"(closest match fails at {locate(closest.absolute_path)}: {_clip(closest.message)})")
    return f"{locate(error.absolute_path)}: {_clip(error.message)}"


def schema_errors(instance: Any, uri: str = SITE_SCHEMA, prefix: Sequence[Any] = (),
                  translate: Callable[[tuple[Any, ...]], tuple[Any, ...]] | None = None) -> list[str]:
    """Schema violations as readable messages, in document order. ``prefix``
    is prepended to every path (for documents embedded in a larger one);
    ``translate`` maps a path in ``instance`` to the manifest's path when
    ``instance`` was derived from the manifest in another shape.

    "Unevaluated properties" errors are dropped for objects that have other
    errors: a failing conditional branch leaves its properties unevaluated,
    so they would be reported as unexpected although they are not."""

    def locate(path: Sequence[Any]) -> str:
        parts = tuple(path)
        return pointer((*prefix, *(translate(parts) if translate else parts)))

    errors = sorted(
        validator(uri).iter_errors(instance),
        key=lambda e: ([str(p) for p in e.absolute_path], e.message),
    )
    other = [tuple(e.absolute_path) for e in errors if e.validator != "unevaluatedProperties"]
    kept = [
        e for e in errors
        if e.validator != "unevaluatedProperties"
        or not any(p[: len(e.absolute_path)] == tuple(e.absolute_path) for p in other)
    ]
    return [_schema_message(e, locate) for e in kept]


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: Sequence[Any], message: str) -> None:
        self.items.append(f"{pointer(path)}: {message}")


def semantic_errors(doc: dict[str, Any]) -> list[str]:
    """Rules the schema cannot express. ``doc`` must already match the schema."""
    return _Checker(doc).run()


def validate_site(doc: Any) -> None:
    """Raise ``ValidationFailed`` listing every problem of a site document."""
    errors = schema_errors(doc)
    if not errors:
        errors = semantic_errors(doc)
    if errors:
        noun = "error" if len(errors) == 1 else "errors"
        raise ValidationFailed(f"site manifest is invalid ({len(errors)} {noun})", errors)


def _param_paths(keys: list[str]) -> Callable[[tuple[Any, ...]], tuple[Any, ...]]:
    """apps.json holds params as a ``[{"key", "value"}]`` list, the manifest as
    a mapping: ``params/1/value`` of the entry is ``params/<key>`` there."""

    def translate(path: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(path) >= 2 and path[0] == "params" and isinstance(path[1], int) and path[1] < len(keys):
            return ("params", keys[path[1]])
        return path

    return translate


def is_relative_inside(path: str) -> bool:
    """True for a relative POSIX path that stays inside its base directory."""
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    norm = posixpath.normpath(path)
    return norm != "." and norm != ".." and not norm.startswith("../")


class _Checker:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.site: str = doc["metadata"]["name"]
        self.system: dict[str, Any] = doc.get("system") or {}
        self.nodes: list[dict[str, Any]] = self.system.get("nodes") or []
        self.externals: list[dict[str, Any]] = doc.get("external_devices") or []
        self.e = _Errors()
        #: device name -> protocol
        self.protocols: dict[str, str] = {}
        #: device name -> BACnet device instance
        self.instances: dict[str, int | None] = {}
        self.node_index: dict[str, int] = {}
        #: generic-json device name -> its configured point ids
        self.json_points: dict[str, set[str]] = {}
        self.life_safety: set[PointRef] = set()

    def run(self) -> list[str]:
        self.devices()
        self.spaces_and_placement()
        self.safety_and_tags()
        self.io()
        links = self.links()
        self.bridges(links)
        self.apps(links)
        self.tests()
        self.policy()
        return self.e.items

    # -- devices ---------------------------------------------------------------
    def devices(self) -> None:
        seen: dict[str, str] = {}
        by_instance: dict[int, str] = {}
        by_address: dict[str, str] = {}
        entries: list[tuple[tuple[Any, ...], dict[str, Any], str]] = [
            (("system", "nodes", i), n, "bacnet-uc") for i, n in enumerate(self.nodes)
        ]
        entries += [(("external_devices", i), d, d["protocol"]) for i, d in enumerate(self.externals)]
        for path, entry, protocol in entries:
            name = entry["name"]
            if name in seen:
                self.e.add((*path, "name"), f"device name {name!r} is already used by {seen[name]}")
                continue
            seen[name] = pointer(path)
            self.protocols[name] = protocol
            if protocol == "bacnet-uc":
                self.node_index[name] = path[-1]
                instance: int | None = entry["device"]["instance"]
                address = self._smp_address(entry.get("transport") or {})
                if address is not None:
                    if address in by_address:
                        self.e.add((*path, "transport"),
                                   f"management address {address} is also used by {by_address[address]}")
                    else:
                        by_address[address] = name
                transport = entry["transport"]
                if transport["kind"] == "udp" and not 0 < transport.get("port", 1337) <= 65535:
                    self.e.add((*path, "transport", "port"), f"port {transport['port']} is not in 1..65535")
                ipath: tuple[Any, ...] = (*path, "device", "instance")
            else:
                instance = entry.get("device_instance")
                ipath = (*path, "device_instance")
                self._unique_points(path, entry)
                _, colon, port = str(entry.get("address", "")).rpartition(":")
                if colon and not 0 < int(port) <= 65535:
                    self.e.add((*path, "address"), f"port {port} is not in 1..65535")
            self.instances[name] = instance
            if instance is not None:
                if instance in by_instance:
                    self.e.add(ipath, f"BACnet device instance {instance} is also used by {by_instance[instance]}")
                else:
                    by_instance[instance] = name

    def _unique_points(self, path: tuple[Any, ...], entry: dict[str, Any]) -> None:
        """Point ids of a generic-json device and a bacnet-ip allow-list are
        unique (the drivers refuse the device otherwise)."""
        key = "obj" if entry["protocol"] == "bacnet-ip" else "id"
        seen: dict[str, int] = {}
        for j, point in enumerate(entry.get("points") or []):
            if point[key] in seen:
                self.e.add((*path, "points", j, key),
                           f"point {point[key]!r} is already listed as points/{seen[point[key]]}")
            else:
                seen[point[key]] = j
        if entry["protocol"] == "mqtt" and entry.get("profile") == "generic-json":
            self.json_points[entry["name"]] = set(seen)

    @staticmethod
    def _smp_address(transport: dict[str, Any]) -> str | None:
        kind = transport.get("kind")
        if kind == "udp":
            return f"udp {transport['host']}:{transport.get('port', 1337)}"
        if kind == "serial":
            return f"serial {transport['device']}"
        if kind == "sim" and transport.get("ipv4"):
            return f"sim {transport['ipv4']}"
        return None

    # -- spaces ------------------------------------------------------------------
    def spaces_and_placement(self) -> None:
        spaces = self.doc.get("spaces") or []
        parents: dict[str, str | None] = {}
        index: dict[str, int] = {}
        for i, space in enumerate(spaces):
            sid = space["id"]
            if sid in parents:
                self.e.add(("spaces", i, "id"), f"duplicate space id {sid!r}")
                continue
            parents[sid] = space.get("parent")
            index[sid] = i
        for sid, parent in parents.items():
            if parent is not None and parent not in parents:
                self.e.add(("spaces", index[sid], "parent"), f"unknown space {parent!r}")
        reported: set[frozenset[str]] = set()
        for start in parents:
            chain: list[str] = []
            current: str | None = start
            while current is not None and current in parents and current not in chain:
                chain.append(current)
                current = parents[current]
            if current is not None and current in chain:
                cycle = frozenset(chain[chain.index(current):])
                if cycle not in reported:
                    reported.add(cycle)
                    members = " -> ".join([*chain[chain.index(current):], current])
                    self.e.add(("spaces", index[current], "parent"), f"parent cycle: {members}")
        for device, space in (self.doc.get("placement") or {}).items():
            if device not in self.protocols:
                self.e.add(("placement", device), f"unknown device {device!r}")
            if space not in parents:
                self.e.add(("placement", device), f"unknown space {space!r}")

    # -- point ids -----------------------------------------------------------------
    def point(self, text: str, path: Sequence[Any], what: str = "point") -> PointRef | None:
        try:
            ref = PointRef.parse(text, default_site=self.site)
        except ValueError as e:
            self.e.add(path, str(e))
            return None
        if ref.site != self.site:
            self.e.add(path, f"{what} {text!r} names site {ref.site!r}, but this site is {self.site!r}")
            return None
        if ref.device not in self.protocols:
            self.e.add(path, f"{what} {text!r}: unknown device {ref.device!r}")
            return None
        return ref

    def bacnet_object(self, ref: PointRef, path: Sequence[Any], what: str) -> bool:
        parsed = ref.bacnet
        if parsed is None:
            self.e.add(path, f"{what} {ref.obj!r} is not a BACnet object (<type>:<instance>)")
            return False
        if parsed[0] not in OBJECT_TYPES or parsed[0] == "device":
            known = ", ".join(t for t in OBJECT_TYPES if t != "device")
            self.e.add(path, f"{what}: unknown BACnet object type {parsed[0]!r} (known: {known})")
            return False
        return True

    def object_form(self, ref: PointRef, path: Sequence[Any], what: str) -> bool:
        """The object part names something the device can have: a
        ``<type>:<instance>`` on BACnet devices, a configured point id on
        generic-json devices. A typo would otherwise attach a safety class
        or tag to a point that never exists."""
        protocol = self.protocols[ref.device]
        if protocol in BACNET_PROTOCOLS and ref.bacnet is None:
            self.e.add(path, f"{what} {ref.obj!r} is not a BACnet object (<type>:<instance>) of {protocol} "
                             f"device {ref.device}")
            return False
        points = self.json_points.get(ref.device)
        if points is not None and ref.obj not in points:
            self.e.add(path, f"{what}: {ref.device} has no point {ref.obj!r} (points: {', '.join(sorted(points))})")
            return False
        return True

    def safety_and_tags(self) -> None:
        for section in ("safety", "tags"):
            seen: dict[PointRef, str] = {}
            for key, value in (self.doc.get(section) or {}).items():
                path = (section, key)
                ref = self.point(key, path, "key")
                if ref is None or not self.object_form(ref, path, "key"):
                    continue
                if ref in seen:
                    self.e.add(path, f"same point as {seen[ref]}")
                    continue
                seen[ref] = pointer(path)
                if section == "safety" and value == "life-safety":
                    self.life_safety.add(ref)

    # -- nodes -------------------------------------------------------------------
    def io(self) -> None:
        for i, node in enumerate(self.nodes):
            points = node.get("io") or []
            if len(points) > MAX_IO_POINTS:
                self.e.add(("system", "nodes", i, "io"),
                           f"{len(points)} io points; io.json holds at most {MAX_IO_POINTS}")
            channels: dict[str, int] = {}
            objects: dict[tuple[str, int], int] = {}
            for j, p in enumerate(points):
                path = ("system", "nodes", i, "io", j)
                channel = p["channel"]
                if channel in channels:
                    self.e.add((*path, "channel"), f"channel {channel!r} is already bound by io/{channels[channel]}")
                else:
                    channels[channel] = j
                obj = (p["type"], p["instance"])
                if obj in objects:
                    self.e.add((*path, "instance"),
                               f"object {obj[0]}:{obj[1]} is already defined by io/{objects[obj]}")
                else:
                    objects[obj] = j
                m = _CHANNEL_RE.match(channel)
                if m and p["type"] not in _CHANNEL_TYPES[m.group(1)]:
                    allowed = " or ".join(_CHANNEL_TYPES[m.group(1)])
                    self.e.add((*path, "type"), f"{m.group(1)} channel {channel} can be bound to {allowed}, "
                                                f"not {p['type']}")

    def links(self) -> list[nodedocs.Link] | None:
        """Parsed links, or None when any link is invalid."""
        ok = True
        links: list[nodedocs.Link] = []
        for i, entry in enumerate(self.system.get("links") or []):
            path = ("system", "links", i)
            src = self.point(entry["from"], (*path, "from"), "link source")
            dst = self.point(entry["to"], (*path, "to"), "link destination")
            if src is None or dst is None:
                ok = False
                continue
            valid = self.bacnet_object(src, (*path, "from"), "link source")
            valid = self.bacnet_object(dst, (*path, "to"), "link destination") and valid
            if self.protocols[src.device] not in BACNET_PROTOCOLS:
                self.e.add((*path, "from"), f"link source device {src.device} is {self.protocols[src.device]}; "
                                            "links copy between BACnet devices (use bridges for MQTT)")
                valid = False
            elif self.instances.get(src.device) is None:
                self.e.add((*path, "from"), f"link source device {src.device} needs a device_instance")
                valid = False
            if self.protocols[dst.device] != "bacnet-uc":
                self.e.add((*path, "to"), f"link destination device {dst.device} is not a bacnet-uc node "
                                          "(the uc-link app runs on the destination node)")
                valid = False
            if src == dst:
                self.e.add(path, "link source and destination are the same point")
                valid = False
            if entry.get("priority") in RESERVED_PRIORITIES:
                self.e.add((*path, "priority"), f"priority {entry['priority']} is reserved for life safety, critical "
                                                "equipment and operators; use 0 (none) or 9..16")
                valid = False
            if valid:
                links.append(nodedocs.Link.from_entry(i, entry, self.site))
            ok = ok and valid
        return links if ok else None

    def bridges(self, links: list[nodedocs.Link] | None) -> None:
        destinations: dict[PointRef, str] = {}
        for lk in links or []:
            to = ("system", "links", lk.index, "to")
            if lk.dest in self.life_safety:
                self.e.add(to, f"{lk.dest} is a life-safety point")
            if lk.dest in destinations:
                self.e.add(to, f"{lk.dest} is already written by {destinations[lk.dest]}")
            else:
                destinations[lk.dest] = pointer(to[:-1])
        for i, bridge in enumerate(self.doc.get("bridges") or []):
            path = ("bridges", i)
            src = self.point(bridge["from"], (*path, "from"), "bridge source")
            dst = self.point(bridge["to"], (*path, "to"), "bridge destination")
            if src is None or dst is None:
                continue
            self.object_form(src, (*path, "from"), "bridge source")
            if self.protocols[dst.device] not in BACNET_PROTOCOLS:
                self.e.add((*path, "to"), f"bridge destination device {dst.device} is "
                                          f"{self.protocols[dst.device]}; it must be bacnet-uc or bacnet-ip")
            else:
                self.bacnet_object(dst, (*path, "to"), "bridge destination")
            if src == dst:
                self.e.add(path, "bridge source and destination are the same point")
            if dst in self.life_safety:
                self.e.add((*path, "to"), f"{dst} is a life-safety point")
            if dst in destinations:
                self.e.add((*path, "to"), f"{dst} is already written by {destinations[dst]}")
            else:
                destinations[dst] = pointer(path)

    def apps(self, links: list[nodedocs.Link] | None) -> None:
        names: dict[tuple[str, str], int] = {}
        per_node: dict[str, list[nodedocs.DesiredApp]] = {}
        for i, app in enumerate(self.system.get("apps") or []):
            path = ("system", "apps", i)
            node = app["node"]
            if node not in self.node_index:
                what = f"a {self.protocols[node]} device" if node in self.protocols else "unknown"
                self.e.add((*path, "node"), f"{node!r} is not a bacnet-uc node ({what})")
                continue
            key = (node, app["name"])
            if key in names:
                self.e.add((*path, "name"), f"app {app['name']!r} is already on {node} (system/apps/{names[key]})")
                continue
            names[key] = i
            for field in ("wasm", "source"):
                if field in app and not is_relative_inside(app[field]):
                    self.e.add((*path, field), f"{app[field]!r} must be a relative path inside the "
                                               "manifest directory")
            try:
                desired = nodedocs.user_app(app, i, self.system, self.doc)
            except KeyError as e:
                self.e.add((*path, "params"), str(e.args[0]))
                continue
            if desired.file == nodedocs.UC_LINK_FILE:
                self.e.add((*path, "name"), f"{desired.file} is reserved for the stock uc-link module")
            self.e.items.extend(schema_errors(
                nodedocs.apps_entry(desired), SCHEMA_BASE + "apps.schema.json#/$defs/app", path,
                _param_paths(list(desired.manifest["params"]))))
            per_node.setdefault(node, []).append(desired)
        if links is not None:
            for node in self.node_index:
                generated = nodedocs.link_apps(node, self.instances.get(node), links, self.instances)
                for app in generated:
                    if (node, app.name) in names:
                        self.e.add(("system", "apps", names[(node, app.name)], "name"),
                                   f"app name {app.name!r} is reserved on {node} for the generated uc-link "
                                   "app of its links")
                per_node.setdefault(node, []).extend(generated)
        for node, apps in per_node.items():
            if len(apps) > nodedocs.MAX_APPS_PER_NODE:
                generated_count = sum(1 for a in apps if a.link)
                self.e.add(("system", "nodes", self.node_index[node]),
                           f"{len(apps)} apps on {node} (including {generated_count} generated uc-link apps); "
                           f"a node holds at most {nodedocs.MAX_APPS_PER_NODE}")

    def tests(self) -> None:
        names: dict[str, int] = {}
        for i, test in enumerate(self.system.get("tests") or []):
            if test["name"] in names:
                self.e.add(("system", "tests", i, "name"),
                           f"duplicate test name {test['name']!r} (system/tests/{names[test['name']]})")
            else:
                names[test["name"]] = i
            for j, step in enumerate(test["steps"]):
                path = ("system", "tests", i, "steps", j)
                kind = next(iter(step))
                if kind not in STEP_KINDS:
                    self.e.add(path, f"unknown step {kind!r} (expected one of {', '.join(STEP_KINDS)})")
                    continue
                body = step[kind]
                if kind in ("force", "release"):
                    node = body["node"]
                    if node not in self.node_index:
                        self.e.add((*path, kind, "node"), f"{node!r} is not a bacnet-uc node")
                    elif kind == "force":
                        bound = self._channel_point(node, body["channel"])
                        if bound in self.life_safety:
                            self.e.add((*path, kind, "channel"), f"tests may not force {node} channel "
                                                                 f"{body['channel']}: it is bound to the "
                                                                 f"life-safety point {bound}")
                elif kind in ("write", "expect"):
                    ref = self.point(body["point"], (*path, kind, "point"))
                    if ref is None or not self.object_form(ref, (*path, kind, "point"), "point"):
                        continue
                    if kind == "write" and ref in self.life_safety:
                        self.e.add((*path, kind, "point"), f"tests may not write the life-safety point {ref}")
                    if kind == "write":
                        self._test_write_priority(ref, body, (*path, kind))

    def _test_write_priority(self, ref: PointRef, body: dict[str, Any], path: tuple[Any, ...]) -> None:
        """The runner relinquishes prioritised writes when the test ends; a
        write without a priority to an output would stay at priority 16."""
        priority = body.get("priority")
        if priority in RESERVED_PRIORITIES:
            self.e.add((*path, "priority"), f"tests may not write at priority {priority}; 1..8 are reserved "
                                             "for life safety, critical equipment and operators")
        parsed = ref.bacnet
        if (priority is None and parsed is not None and parsed[0] in COMMANDABLE_TYPES
                and body.get("property", "present-value") == "present-value"):
            self.e.add((*path, "priority"), f"a test write to {parsed[0]}:{parsed[1]} needs a priority (9..16), "
                                             "so that it is relinquished when the test ends")

    def _channel_point(self, node: str, channel: str) -> PointRef | None:
        """The BACnet object an IO channel of a node is bound to in its io.json."""
        for p in self.nodes[self.node_index[node]].get("io") or []:
            if p["channel"] == channel:
                return PointRef(self.site, node, f"{p['type']}:{p['instance']}")
        return None

    def policy(self) -> None:
        policy = self.doc.get("policy") or {}
        default = policy.get("default_lease_s", 300)
        maximum = policy.get("max_lease_s", 3600)
        if default > maximum:
            self.e.add(("policy", "default_lease_s"), f"default_lease_s {default} exceeds max_lease_s {maximum}")
