"""Loading the site manifest (``site.yaml``) and the ``SiteManifest`` view of it.

YAML is parsed with a restricted safe loader: no aliases (no "billion
laughs"), no duplicate keys (a silently dropped key would change the desired
state), string keys only, timestamps kept as text and no NaN/Infinity, so
every accepted document is plain JSON data. The document is then validated
(``validate.validate_site``) before a ``SiteManifest`` is built from it.
"""

from __future__ import annotations

import copy
import fnmatch
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import ValidationFailed
from ..core.ids import PointRef
from ..core.types import ProtocolName, SafetyClass
from . import nodedocs
from .nodedocs import DesiredApp, Link
from .validate import validate_site

#: Larger manifests are refused before parsing.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
#: Deeper nesting is refused. A manifest needs about ten levels, and the
#: recursive YAML composer (and every later tree walk) would otherwise run
#: into Python's recursion limit on a hostile document.
MAX_DEPTH = 64

TOP_LEVEL_ORDER = (
    "apiVersion", "kind", "metadata", "spaces", "placement", "system",
    "external_devices", "bridges", "tags", "safety", "policy",
)
SYSTEM_ORDER = ("apiVersion", "kind", "metadata", "nodes", "apps", "links", "tests")

POLICY_DEFAULTS: dict[str, Any] = {
    "agent_write_priority": 12,
    "default_lease_s": 300,
    "max_lease_s": 3600,
    "deny": [],
    "max_writes_per_minute": 30,
    "max_devices_per_stage": 5,
    "approval_ttl_s": 1800,
}
BRIDGE_DEFAULTS: dict[str, Any] = {"max_age_s": 300, "scale": 1.0, "offset": 0.0}


# -- YAML ---------------------------------------------------------------------------
class _Loader(yaml.SafeLoader):
    """SafeLoader without aliases, duplicate keys, timestamps, non-string keys
    or nesting deeper than MAX_DEPTH."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(
                None, None, "aliases (*name) are not allowed", event.start_mark)
        if self._depth >= MAX_DEPTH:
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, f"nested deeper than {MAX_DEPTH} levels", event.start_mark)
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, f"expected a mapping, found {node.id}", node.start_mark)
        self.flatten_mapping(node)
        out: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"keys must be strings, found {key!r}", key_node.start_mark)
            if key in out:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"duplicate key {key!r}", key_node.start_mark)
            out[key] = self.construct_object(value_node, deep=deep)
        return out


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _Loader.construct_yaml_str)


def _check_json_data(value: Any, path: str = "", depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ValidationFailed(f"manifest is nested deeper than {MAX_DEPTH} levels", [f"{path}: too deep"])
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationFailed("manifest contains a non-finite number", [f"{path or '<root>'}: {value!r}"])
    if isinstance(value, dict):
        for k, v in value.items():
            _check_json_data(v, f"{path}/{k}", depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_json_data(v, f"{path}/{i}", depth + 1)
    elif not (value is None or isinstance(value, (str, int, float))):
        raise ValidationFailed("manifest contains a non-JSON value", [f"{path or '<root>'}: {type(value).__name__}"])


def parse_yaml(text: str, source: str = "site.yaml") -> Any:
    """Parse one YAML document into plain JSON data."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_MANIFEST_BYTES:
        raise ValidationFailed(f"{source} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        data = yaml.load(text, Loader=_Loader)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "<unknown>"
        raise ValidationFailed(f"{source} is not valid YAML", [f"{where}: {e.problem or e.context}"]) from None
    except yaml.YAMLError as e:
        raise ValidationFailed(f"{source} is not valid YAML", [str(e)]) from None
    _check_json_data(data)
    return data


def dump_yaml(data: Any) -> str:
    return str(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100))


# -- views ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Space:
    id: str
    name: str
    parent: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """A device of the site: a bacnet-uc node or an external device."""

    name: str
    protocol: ProtocolName
    #: The manifest entry (``system.nodes[]`` or ``external_devices[]`` item).
    spec: dict[str, Any]
    space: str | None = None

    @property
    def managed(self) -> bool:
        return self.protocol is ProtocolName.BACNET_UC


@dataclass(frozen=True, slots=True)
class Policy:
    agent_write_priority: int = 12
    default_lease_s: int = 300
    max_lease_s: int = 3600
    #: fnmatch patterns over full point ids.
    deny: tuple[str, ...] = ()
    max_writes_per_minute: int = 30
    max_devices_per_stage: int = 5
    approval_ttl_s: int = 1800

    def denies(self, point: PointRef | str) -> bool:
        text = str(point)
        return any(fnmatch.fnmatchcase(text, pattern) for pattern in self.deny)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_write_priority": self.agent_write_priority,
            "default_lease_s": self.default_lease_s,
            "max_lease_s": self.max_lease_s,
            "deny": list(self.deny),
            "max_writes_per_minute": self.max_writes_per_minute,
            "max_devices_per_stage": self.max_devices_per_stage,
            "approval_ttl_s": self.approval_ttl_s,
        }


@dataclass(frozen=True, slots=True)
class Bridge:
    """A gateway bridge with defaults applied and full point ids."""

    source: PointRef
    dest: PointRef
    max_age_s: int
    scale: float
    offset: float
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": str(self.source), "to": str(self.dest), "max_age_s": self.max_age_s,
            "scale": self.scale, "offset": self.offset, "priority": self.priority,
        }


class SiteManifest:
    """A validated site document. Construct with ``from_yaml``, ``from_dict``
    or ``load``; the constructor assumes the document is already valid."""

    def __init__(self, doc: dict[str, Any], base_dir: Path | None = None) -> None:
        self._doc = _canonical(doc)
        #: Directory that app paths (``wasm``, ``source``) are relative to.
        self.base_dir = base_dir

    # -- construction -----------------------------------------------------------------
    @classmethod
    def from_dict(cls, doc: Any, base_dir: Path | None = None) -> SiteManifest:
        _check_json_data(doc)
        validate_site(doc)
        return cls(copy.deepcopy(doc), base_dir)

    @classmethod
    def from_yaml(cls, text: str, base_dir: Path | None = None, source: str = "site.yaml") -> SiteManifest:
        return cls.from_dict(parse_yaml(text, source), base_dir)

    @classmethod
    def load(cls, path: Path) -> SiteManifest:
        try:
            size = path.stat().st_size
            if size > MAX_MANIFEST_BYTES:
                raise ValidationFailed(f"{path.name} is larger than {MAX_MANIFEST_BYTES} bytes")
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValidationFailed(f"{path.name} is not UTF-8 text", [str(e)]) from None
        return cls.from_yaml(text, path.parent, path.name)

    # -- serialisation ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Deep copy of the document, top-level sections in canonical order."""
        return copy.deepcopy(self._doc)

    def to_yaml(self) -> str:
        return dump_yaml(self._doc)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SiteManifest) and self._doc == other._doc

    __hash__ = None  # type: ignore[assignment]

    # -- metadata and spaces --------------------------------------------------------------
    @property
    def name(self) -> str:
        return str(self._doc["metadata"]["name"])

    @property
    def description(self) -> str:
        return str(self._doc["metadata"].get("description", ""))

    @property
    def spaces(self) -> list[Space]:
        return [Space(s["id"], s["name"], s.get("parent")) for s in self._doc.get("spaces") or []]

    def space_tree(self) -> list[dict[str, Any]]:
        """Spaces nested by parent: ``[{"id", "name", "children": [...]}]``,
        siblings in manifest order."""
        nodes: dict[str, dict[str, Any]] = {
            s.id: {"id": s.id, "name": s.name, "children": []} for s in self.spaces
        }
        roots: list[dict[str, Any]] = []
        for s in self.spaces:
            if s.parent is None:
                roots.append(nodes[s.id])
            else:
                nodes[s.parent]["children"].append(nodes[s.id])
        return roots

    def space_path(self, space: str) -> list[str]:
        """Ids from the root down to ``space``."""
        parents = {s.id: s.parent for s in self.spaces}
        path: list[str] = []
        current: str | None = space
        while current is not None and current in parents:
            path.append(current)
            current = parents[current]
        return list(reversed(path))

    def spaces_within(self, space: str) -> set[str]:
        """``space`` and every space below it."""
        children: dict[str, list[str]] = {}
        for s in self.spaces:
            if s.parent is not None:
                children.setdefault(s.parent, []).append(s.id)
        out: set[str] = set()
        todo = [space]
        while todo:
            current = todo.pop()
            if current not in out:
                out.add(current)
                todo.extend(children.get(current, []))
        return out

    @property
    def placement(self) -> dict[str, str]:
        return dict(self._doc.get("placement") or {})

    # -- devices -----------------------------------------------------------------------
    @property
    def system(self) -> dict[str, Any] | None:
        system = self._doc.get("system")
        return copy.deepcopy(system) if system is not None else None

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        """BACnet-uc nodes (``system.nodes``) by name, in manifest order."""
        return {n["name"]: copy.deepcopy(n) for n in self._system.get("nodes") or []}

    @property
    def externals(self) -> dict[str, dict[str, Any]]:
        return {d["name"]: copy.deepcopy(d) for d in self._doc.get("external_devices") or []}

    @property
    def devices(self) -> dict[str, DeviceSpec]:
        """Every device by name: bacnet-uc nodes first, then external devices."""
        placement = self._doc.get("placement") or {}
        out: dict[str, DeviceSpec] = {}
        for name, node in self.nodes.items():
            out[name] = DeviceSpec(name, ProtocolName.BACNET_UC, node, placement.get(name))
        for name, ext in self.externals.items():
            out[name] = DeviceSpec(name, ProtocolName(ext["protocol"]), ext, placement.get(name))
        return out

    def instances(self) -> dict[str, int | None]:
        """Device name -> BACnet device instance (None for devices without one)."""
        out: dict[str, int | None] = {
            n["name"]: int(n["device"]["instance"]) for n in self._system.get("nodes") or []
        }
        for ext in self._doc.get("external_devices") or []:
            instance = ext.get("device_instance")
            out[ext["name"]] = int(instance) if instance is not None else None
        return out

    def device_instance(self, device: str) -> int | None:
        """BACnet device instance of a bacnet-uc node or bacnet-ip device."""
        return self.instances().get(device)

    def point_ref(self, text: str) -> PointRef:
        """Parse a site-relative or full point id of this site."""
        return PointRef.parse(text, default_site=self.name)

    # -- policy, tags, safety, bridges ------------------------------------------------------
    @property
    def policy(self) -> Policy:
        raw = {**POLICY_DEFAULTS, **(self._doc.get("policy") or {})}
        return Policy(
            agent_write_priority=int(raw["agent_write_priority"]),
            default_lease_s=int(raw["default_lease_s"]),
            max_lease_s=int(raw["max_lease_s"]),
            deny=tuple(self._full_pattern(p) for p in raw["deny"]),
            max_writes_per_minute=int(raw["max_writes_per_minute"]),
            max_devices_per_stage=int(raw["max_devices_per_stage"]),
            approval_ttl_s=int(raw["approval_ttl_s"]),
        )

    def _full_pattern(self, pattern: str) -> str:
        """Site-relative deny patterns (fewer than two '/') get the site prefix."""
        return pattern if pattern.count("/") >= 2 else f"{self.name}/{pattern}"

    @property
    def tags(self) -> dict[str, list[str]]:
        """Full point id -> tags."""
        return {str(self.point_ref(k)): list(v) for k, v in (self._doc.get("tags") or {}).items()}

    @property
    def safety(self) -> dict[str, SafetyClass]:
        """Full point id -> safety class (points not listed are normal)."""
        return {str(self.point_ref(k)): SafetyClass(v) for k, v in (self._doc.get("safety") or {}).items()}

    @property
    def bridges(self) -> list[Bridge]:
        priority = self.policy.agent_write_priority
        out = []
        for b in self._doc.get("bridges") or []:
            values = {**BRIDGE_DEFAULTS, "priority": priority, **b}
            out.append(Bridge(
                source=self.point_ref(b["from"]), dest=self.point_ref(b["to"]),
                max_age_s=int(values["max_age_s"]), scale=float(values["scale"]),
                offset=float(values["offset"]), priority=int(values["priority"]),
            ))
        return out

    # -- system: links, apps, tests, node documents ------------------------------------------
    @property
    def _system(self) -> dict[str, Any]:
        return self._doc.get("system") or {}

    @property
    def links(self) -> list[Link]:
        return [Link.from_entry(i, e, self.name) for i, e in enumerate(self._system.get("links") or [])]

    @property
    def tests(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._system.get("tests") or [])

    def _node(self, node: str) -> dict[str, Any]:
        entry: dict[str, Any]
        for entry in self._system.get("nodes") or []:
            if entry["name"] == node:
                return entry
        raise KeyError(f"no bacnet-uc node {node!r}")

    def device_json(self, node: str) -> dict[str, Any]:
        """Desired ``/lfs/cfg/device.json`` of a node (manifest sections only)."""
        return nodedocs.device_doc(self._node(node))

    def io_json(self, node: str) -> dict[str, Any]:
        """Desired ``/lfs/cfg/io.json`` of a node."""
        return nodedocs.io_doc(self._node(node))

    def apps(self, node: str) -> list[DesiredApp]:
        """Desired apps on a node: ``system.apps`` entries in manifest order, then
        the generated uc-link instances for the links ending on it."""
        entry = self._node(node)
        out = [
            nodedocs.user_app(app, i, self._system, self._doc)
            for i, app in enumerate(self._system.get("apps") or [])
            if app["node"] == node
        ]
        out.extend(nodedocs.link_apps(node, int(entry["device"]["instance"]), self.links, self.instances()))
        return out

    def iter_apps(self) -> Iterator[DesiredApp]:
        for node in self.nodes:
            yield from self.apps(node)


def _canonical(doc: dict[str, Any]) -> dict[str, Any]:
    out = _ordered(doc, TOP_LEVEL_ORDER)
    if isinstance(out.get("system"), dict):
        out["system"] = _ordered(out["system"], SYSTEM_ORDER)
    return out


def _ordered(doc: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    out = {k: doc[k] for k in order if k in doc}
    out.update((k, v) for k, v in doc.items() if k not in out)
    return out


def load_site(path: Path | str) -> SiteManifest:
    return SiteManifest.load(Path(path))
