# SPDX-License-Identifier: Apache-2.0
"""System manifests (``schemas/system.schema.json``): loading, placeholder
resolution, schema validation and semantic checks.

A manifest describes the desired state of a distributed BACnet application:
nodes (board, management transport, BACnet identity, IO mapping),
WebAssembly applications placed on nodes, point-to-point ``links`` and
acceptance ``tests``.

Processing order of :func:`load_system`:

1. parse YAML (YAML 1.2 booleans: only ``true``/``false``) or JSON,
2. resolve ``{{ dotted.path }}`` placeholders against the manifest itself
   (``nodes.<name>`` selects the node with that name, a numeric segment a
   list index); a string that is exactly one placeholder takes the type of
   the referenced value,
3. validate against the JSON schema (``$ref`` to device/io schemas resolved
   through a :class:`referencing.Registry`),
4. semantic checks (unique names and device instances, references to
   existing nodes, valid object references, IO channels against a catalog,
   BACnet object collisions per node).

All problems are collected; :class:`ManifestError` carries every error with
a JSON-pointer-like path. Warnings do not fail loading and are kept in
:attr:`System.warnings`.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from bacnet_uc_harness import paths
from bacnet_uc_harness.bacnet import enums
from bacnet_uc_harness.errors import HarnessError

Severity = Literal["error", "warning"]

# Object types the firmware can create (uc_obj_type_supported) and their numbers.
OBJ_AI, OBJ_AO, OBJ_AV, OBJ_BI, OBJ_BO, OBJ_BV = 0, 1, 2, 3, 4, 5
OBJ_MSI, OBJ_MSO, OBJ_MSV = 13, 14, 19
SUPPORTED_TYPES = frozenset({OBJ_AI, OBJ_AO, OBJ_AV, OBJ_BI, OBJ_BO, OBJ_BV, OBJ_MSI, OBJ_MSO,
                             OBJ_MSV})
#: value objects an application (or uc-link) creates when they do not exist
VALUE_TYPES = frozenset({OBJ_AV, OBJ_BV, OBJ_MSV})
#: object types whose Present_Value can be written
WRITABLE_TYPES = frozenset({OBJ_AO, OBJ_AV, OBJ_BO, OBJ_BV, OBJ_MSO, OBJ_MSV})

#: io.schema.json: channel kind -> allowed object types
CHANNEL_KIND_TYPES: dict[str, frozenset[str]] = {
    "di": frozenset({"binary-input", "multi-state-input"}),
    "do": frozenset({"binary-output", "binary-value"}),
    "ai": frozenset({"analog-input"}),
    "ao": frozenset({"analog-output", "analog-value"}),
}

SIM_BOARDS = frozenset({"native_sim", "native_sim/native/64"})
DEFAULT_BACNET_PORT = 47808
DEFAULT_SMP_PORT = 1337
#: CONFIG_UC_APPS_MAX default in firmware/Kconfig (apps.schema.json allows 8)
FIRMWARE_APPS_MAX_DEFAULT = 4
APPS_SCHEMA_MAX = 8
LINKS_PER_APP = 8
STEP_KINDS = ("force", "release", "write", "wait", "expect")

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_FULL_PLACEHOLDER = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}\s*$")


# --- issues ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One validation finding. ``path`` is JSON-pointer-like (``/nodes/0/name``)."""

    path: str
    message: str
    severity: Severity = "error"

    def __str__(self) -> str:
        return f"{self.path or '/'}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path or "/", "message": self.message, "severity": self.severity}


class ManifestError(HarnessError):
    """The manifest is invalid. ``issues`` holds every error (and the warnings)."""

    def __init__(self, issues: list[Issue], source: str | None = None) -> None:
        self.issues = list(issues)
        self.source = source
        errors = self.errors
        head = f"{len(errors)} error(s) in manifest" + (f" {source}" if source else "")
        lines = [head + ":"] + [f"  {i}" for i in errors]
        super().__init__("\n".join(lines))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def messages(self) -> list[str]:
        return [str(i) for i in self.errors]


def _ptr(*parts: object) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


# --- YAML loading -------------------------------------------------------------------------


class _Yaml12Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans (``on``/``off``/``yes``/``no`` stay strings)."""


_Yaml12Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def parse_text(text: str, fmt: Literal["yaml", "json"] = "yaml") -> Any:
    """Parse manifest text (YAML 1.2 booleans, or JSON)."""
    try:
        if fmt == "json":
            return json.loads(text)
        return yaml.load(text, Loader=_Yaml12Loader)  # noqa: S506 - SafeLoader subclass
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ManifestError([Issue("", f"cannot parse {fmt.upper()}: {exc}")]) from exc


def read_document(path: Path | str) -> Any:
    """Read a YAML or JSON file (by suffix; YAML otherwise)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError([Issue("", f"cannot read {p}: {exc}")], str(p)) from exc
    return parse_text(text, "json" if p.suffix.lower() == ".json" else "yaml")


def dump_yaml(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


# --- schemas ---------------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _schemas(schema_dir: str) -> tuple[dict[str, dict[str, Any]], Registry]:
    docs: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource]] = []
    for file in sorted(Path(schema_dir).glob("*.schema.json")):
        doc = json.loads(file.read_text(encoding="utf-8"))
        name = file.name.removesuffix(".schema.json")
        docs[name] = doc
        res = Resource.from_contents(doc, default_specification=DRAFT202012)
        if "$id" in doc:
            resources.append((doc["$id"], res))
        resources.append((file.name, res))
    return docs, Registry().with_resources(resources)


def load_schema(name: str) -> dict[str, Any]:
    """Schema ``schemas/<name>.schema.json`` (``device``, ``io``, ``apps``, ``system``)."""
    docs, _ = _schemas(str(paths.schemas_dir()))
    try:
        return docs[name]
    except KeyError:
        raise HarnessError(f"unknown schema {name!r} (known: {', '.join(sorted(docs))})") from None


def schema_names() -> list[str]:
    docs, _ = _schemas(str(paths.schemas_dir()))
    return sorted(docs)


def _validator(name: str) -> Draft202012Validator:
    docs, registry = _schemas(str(paths.schemas_dir()))
    if name not in docs:
        raise HarnessError(f"unknown schema {name!r}")
    return Draft202012Validator(docs[name], registry=registry, format_checker=FormatChecker())


def _pick_context(err: ValidationError) -> ValidationError:
    """For a failed oneOf/anyOf, the error of the branch that was probably meant:
    the only branch without a ``const``/``enum`` mismatch (e.g. the transport
    whose ``kind`` matched), else jsonschema's best match."""
    branches: dict[object, list[ValidationError]] = {}
    for sub in err.context or ():
        key = sub.relative_schema_path[0] if sub.relative_schema_path else None
        branches.setdefault(key, []).append(sub)
    plausible = [errs for errs in branches.values()
                 if not any(e.validator in ("const", "enum") for e in errs)]
    if len(plausible) == 1:
        return _pick_context(plausible[0][0]) if plausible[0][0].context else plausible[0][0]
    return best_match(err.context) or err


def _error_issue(err: ValidationError, prefix: tuple[object, ...] = ()) -> Issue:
    if err.context:
        err = _pick_context(err)
    parts = (*prefix, *err.absolute_path)
    return Issue(_ptr(*parts) if parts else "", err.message)


def validate_document(name: str, doc: Any, prefix: tuple[object, ...] = ()) -> list[Issue]:
    """Validate ``doc`` against ``schemas/<name>.schema.json``; returns the errors."""
    validator = _validator(name)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    return [_error_issue(e, prefix) for e in errors]


def check_document(name: str, doc: Any) -> None:
    """Raise :class:`ManifestError` when ``doc`` violates ``schemas/<name>.schema.json``."""
    issues = validate_document(name, doc)
    if issues:
        raise ManifestError(issues, f"({name}.json)")


# --- placeholders ---------------------------------------------------------------------------


class _Unresolved(Exception):
    pass


def lookup_path(doc: Any, dotted: str) -> Any:
    """Value at a dotted path. A list is indexed by a number or by the ``name``
    of its elements (``nodes.sensor.device.instance``). Raises ``KeyError``."""
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, Mapping):
            if part not in cur:
                raise KeyError(f"no key {part!r}")
            cur = cur[part]
        elif isinstance(cur, list):
            if part.isdigit():
                idx = int(part)
                if idx >= len(cur):
                    raise KeyError(f"index {idx} out of range")
                cur = cur[idx]
            else:
                for item in cur:
                    if isinstance(item, Mapping) and item.get("name") == part:
                        cur = item
                        break
                else:
                    raise KeyError(f"no element named {part!r}")
        else:
            raise KeyError(f"cannot descend into {type(cur).__name__} at {part!r}")
    return cur


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def resolve_placeholders(doc: Any) -> tuple[Any, list[Issue]]:
    """Resolve ``{{ path }}`` placeholders in all strings of ``doc``.

    Returns the resolved copy and issues for unknown paths or cycles (the
    placeholder is left in place then).
    """
    issues: list[Issue] = []
    root = copy.deepcopy(doc)

    def resolve_target(dotted: str, stack: tuple[str, ...]) -> Any:
        if dotted in stack:
            raise _Unresolved("placeholder cycle: " + " -> ".join((*stack, dotted)))
        try:
            target = lookup_path(root, dotted)
        except KeyError as exc:
            raise _Unresolved(f"unknown placeholder {{{{ {dotted} }}}}: {exc.args[0]}") from None
        return walk(copy.deepcopy(target), (*stack, dotted))

    def walk_str(text: str, stack: tuple[str, ...]) -> Any:
        full = _FULL_PLACEHOLDER.match(text)
        if full:
            return resolve_target(full.group(1), stack)
        return _PLACEHOLDER.sub(lambda m: _stringify(resolve_target(m.group(1), stack)), text)

    def walk(value: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(value, str):
            return walk_str(value, stack) if "{{" in value else value
        if isinstance(value, list):
            return [walk(v, stack) for v in value]
        if isinstance(value, dict):
            return {k: walk(v, stack) for k, v in value.items()}
        return value

    def top(value: Any, where: tuple[object, ...]) -> Any:
        if isinstance(value, str):
            if "{{" not in value:
                return value
            try:
                return walk_str(value, ())
            except _Unresolved as exc:
                issues.append(Issue(_ptr(*where), str(exc)))
                return value
        if isinstance(value, list):
            return [top(v, (*where, i)) for i, v in enumerate(value)]
        if isinstance(value, dict):
            return {k: top(v, (*where, k)) for k, v in value.items()}
        return value

    resolved = top(root, ())
    return resolved, issues


# --- IO catalogs ----------------------------------------------------------------------------

Catalog = dict[str, str | None]  # channel name -> kind (di/do/ai/ao) or None if unknown

_DTS_CHILD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\{([^{}]*)\}", re.S)
_DTS_KIND = re.compile(r'kind\s*=\s*"(di|do|ai|ao)"')


def parse_catalog_dtsi(text: str) -> Catalog:
    """Channels of the ``uc,io-channels`` node of a board IO ``.dtsi``."""
    pos = text.find('"uc,io-channels"')
    if pos < 0:
        return {}
    out: Catalog = {}
    for m in _DTS_CHILD.finditer(text, pos):
        kind = _DTS_KIND.search(m.group(2))
        if kind:
            out[m.group(1)] = kind.group(1)
    return out


def builtin_catalog(board: str) -> Catalog | None:
    """IO catalog of a board from ``firmware/boards/io/<board>.dtsi``, ``None``
    if there is no such file."""
    key = paths.board_key(board)
    if key == "native_sim":
        key = "native_sim_native_64"
    try:
        file = paths.board_io_dir() / f"{key}.dtsi"
        return parse_catalog_dtsi(file.read_text(encoding="utf-8"))
    except (OSError, HarnessError):
        return None


def normalize_catalog(cat: Any) -> Catalog:
    """Accept an ``io_catalog()`` response, a ``{name: kind}`` map or channel names."""
    if isinstance(cat, Mapping) and "channels" in cat:
        return {str(c["name"]): c.get("kind") for c in cat["channels"] if "name" in c}
    if isinstance(cat, Mapping):
        return {str(k): (str(v) if isinstance(v, str) else None) for k, v in cat.items()}
    if isinstance(cat, Iterable) and not isinstance(cat, str | bytes):
        return {str(c): None for c in cat}
    raise HarnessError(f"invalid IO catalog: {cat!r}")


# --- application kinds (objects created, permissions) -----------------------------------


def _param(params: Mapping[str, Any], key: str) -> str | None:
    v = params.get(key)
    return None if v is None else _stringify(v)


def _param_int(params: Mapping[str, Any], key: str, default: int | None) -> int | None:
    v = _param(params, key)
    if v is None or v.strip() == "":
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def is_remote_device(value: Any, own_instance: int | None) -> bool:
    """True if a ``*_device`` parameter names another device (not ``local``)."""
    if value is None:
        return False
    text = _stringify(value).strip().lower()
    if text in ("", "local"):
        return False
    try:
        dev = int(float(text))
    except ValueError:
        return True
    return not (dev == 0xFFFFFFFF or (own_instance is not None and dev == own_instance))


@dataclass(frozen=True)
class ObjectClaim:
    """A BACnet object an application or link creates on its node."""

    obj_type: int
    instance: int
    role: str

    @property
    def ref(self) -> str:
        return enums.format_object_ref(self.obj_type, self.instance)


@dataclass(frozen=True)
class AppKind:
    """Knowledge about a stock application from ``wasm/examples``."""

    name: str
    source: str  # relative to wasm/examples
    objects: Callable[[Mapping[str, Any]], list[ObjectClaim]]
    perms: Callable[[Mapping[str, Any], int | None], list[str]]


def _thermostat_objects(p: Mapping[str, Any]) -> list[ObjectClaim]:
    return [ObjectClaim(OBJ_AV, _param_int(p, "sp_instance", 1) or 0, "setpoint")]


def _thermostat_perms(p: Mapping[str, Any], dev: int | None) -> list[str]:
    perms = ["bacnet.local"]
    if is_remote_device(p.get("sensor_device"), dev) or is_remote_device(p.get("out_device"), dev):
        perms.append("bacnet.remote")
    perms.append("kv")
    return perms


def _alarm_objects(p: Mapping[str, Any]) -> list[ObjectClaim]:
    out = [ObjectClaim(OBJ_BV, _param_int(p, "bv_instance", 10) or 0, "alarm")]
    count = _param_int(p, "count_instance", None)
    if count is not None:
        out.append(ObjectClaim(OBJ_AV, count, "trip counter"))
    return out


def _alarm_perms(p: Mapping[str, Any], dev: int | None) -> list[str]:
    perms = ["bacnet.local"]
    if is_remote_device(p.get("src_device"), dev):
        perms.append("bacnet.remote")
    perms.append("kv")
    return perms


def _blinky_objects(p: Mapping[str, Any]) -> list[ObjectClaim]:
    if _param(p, "channel"):
        return []
    obj_type = _param_int(p, "type", OBJ_BV)
    if obj_type in VALUE_TYPES:
        return [ObjectClaim(obj_type, _param_int(p, "instance", 1) or 0, "blinky")]
    return []


def _blinky_perms(p: Mapping[str, Any], dev: int | None) -> list[str]:
    return ["io"] if _param(p, "channel") else ["bacnet.local"]


def parse_link_line(text: str) -> dict[str, Any]:
    """Parse one uc-link ``l<i>`` parameter (wasm/examples/uc-link/README.md)."""
    f = text.split(" ")
    if len(f) != 10:
        raise ValueError(f"uc-link line needs 10 fields, got {len(f)}: {text!r}")
    return {
        "src_device": int(f[0]), "src_type": int(f[1]), "src_instance": int(f[2]),
        "dst_type": int(f[3]), "dst_instance": int(f[4]), "mode": f[5],
        "period_ms": int(f[6]), "priority": int(f[7]), "scale": float(f[8]),
        "offset": float(f[9]),
    }


def _link_objects(p: Mapping[str, Any]) -> list[ObjectClaim]:
    out = []
    count = _param_int(p, "count", 0) or 0
    for i in range(count):
        line = _param(p, f"l{i}")
        if not line:
            continue
        try:
            link = parse_link_line(line)
        except ValueError:
            continue
        if link["dst_type"] in VALUE_TYPES:
            out.append(ObjectClaim(link["dst_type"], link["dst_instance"], f"link-{i}"))
    return out


APP_KINDS: dict[str, AppKind] = {
    "thermostat": AppKind("thermostat", "thermostat/thermostat.c", _thermostat_objects,
                          _thermostat_perms),
    "alarm": AppKind("alarm", "alarm/alarm.c", _alarm_objects, _alarm_perms),
    "blinky": AppKind("blinky", "blinky/blinky.c", _blinky_objects, _blinky_perms),
    "uc-link": AppKind("uc-link", "uc-link/uc_link.c", _link_objects,
                       lambda p, dev: ["bacnet.local", "bacnet.remote"]),
}


def detect_app_kind(name: str, source: str | Path | None, wasm: str | Path | None) -> str | None:
    """Stock application kind from the source/wasm file stem or the app name."""
    for cand in (source, wasm):
        if cand:
            stem = Path(str(cand)).name.split(".")[0].replace("_", "-")
            if stem in APP_KINDS:
                return stem
    n = name.replace("_", "-")
    return n if n in APP_KINDS else None


# --- the system model -----------------------------------------------------------------------


@dataclass
class Transport:
    kind: Literal["udp", "serial", "sim"]
    host: str | None = None
    port: int = DEFAULT_SMP_PORT
    device: str | None = None
    baud: int = 115200
    ipv4: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Transport:
        return cls(kind=d["kind"], host=d.get("host"), port=int(d.get("port", DEFAULT_SMP_PORT)),
                   device=d.get("device"), baud=int(d.get("baud", 115200)), ipv4=d.get("ipv4"))

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "udp":
            return {"kind": "udp", "host": self.host, "port": self.port}
        if self.kind == "serial":
            return {"kind": "serial", "device": self.device, "baud": self.baud}
        out: dict[str, Any] = {"kind": "sim"}
        if self.ipv4:
            out["ipv4"] = self.ipv4
        return out


@dataclass
class NodeSpec:
    name: str
    board: str
    transport: Transport
    device: dict[str, Any]
    bacnet_address: str | None = None
    network: dict[str, Any] | None = None
    bacnet: dict[str, Any] | None = None
    io: list[dict[str, Any]] = field(default_factory=list)

    @property
    def instance(self) -> int:
        return int(self.device["instance"])

    @property
    def bacnet_port(self) -> int:
        return int((self.bacnet or {}).get("udp_port", DEFAULT_BACNET_PORT))

    @property
    def is_sim(self) -> bool:
        return self.transport.kind == "sim"

    def static_ipv4(self) -> str | None:
        net = self.network or {}
        if net.get("dhcp", True) is False and net.get("ipv4"):
            return str(net["ipv4"])
        return None

    def io_objects(self) -> list[tuple[int, int, dict[str, Any]]]:
        return [(enums.object_type_number(p["type"]), int(p["instance"]), p) for p in self.io]


@dataclass
class AppSpec:
    name: str
    node: str
    source: Path | None = None
    wasm: Path | None = None
    aot: bool = False
    autostart: bool = True
    period_ms: int = 1000
    heap_kb: int = 8
    stack_kb: int = 4
    perms: list[str] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    kind: str | None = None
    generated: bool = False  # uc-link instances derived from links

    def object_claims(self) -> list[ObjectClaim]:
        k = APP_KINDS.get(self.kind or "")
        return k.objects(self.params) if k else []


@dataclass
class LinkSpec:
    index: int
    from_ref: str
    to_ref: str
    src_node: str
    src_type: int
    src_instance: int
    dst_node: str
    dst_type: int
    dst_instance: int
    mode: Literal["cov", "poll"] = "cov"
    period_ms: int = 1000
    priority: int = 0
    scale: float = 1.0
    offset: float = 0.0


@dataclass
class TestSpec:
    __test__ = False

    name: str
    steps: list[dict[str, Any]]


@dataclass
class System:
    name: str
    description: str
    nodes: list[NodeSpec]
    apps: list[AppSpec]
    links: list[LinkSpec]
    tests: list[TestSpec]
    doc: dict[str, Any]
    path: Path | None = None
    base_dir: Path | None = None
    warnings: list[Issue] = field(default_factory=list)

    def node(self, name: str) -> NodeSpec:
        for n in self.nodes:
            if n.name == name:
                return n
        raise HarnessError(f"system {self.name!r} has no node {name!r}")

    def has_node(self, name: str) -> bool:
        return any(n.name == name for n in self.nodes)

    def node_names(self) -> list[str]:
        return [n.name for n in self.nodes]

    def apps_on(self, node: str) -> list[AppSpec]:
        return [a for a in self.apps if a.node == node]

    def links_to(self, node: str) -> list[LinkSpec]:
        return [lk for lk in self.links if lk.dst_node == node]

    def sim_nodes(self) -> list[NodeSpec]:
        return [n for n in self.nodes if n.is_sim]

    def test(self, name: str) -> TestSpec:
        for t in self.tests:
            if t.name == name:
                return t
        raise HarnessError(f"system {self.name!r} has no test {name!r}")

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path) if self.path else None,
            "nodes": [{"name": n.name, "board": n.board, "transport": n.transport.to_dict(),
                       "device_instance": n.instance, "io_points": len(n.io)}
                      for n in self.nodes],
            "apps": [{"name": a.name, "node": a.node, "kind": a.kind,
                      "source": str(a.source) if a.source else None,
                      "wasm": str(a.wasm) if a.wasm else None, "aot": a.aot} for a in self.apps],
            "links": [{"from": lk.from_ref, "to": lk.to_ref, "mode": lk.mode} for lk in self.links],
            "tests": [t.name for t in self.tests],
            "warnings": [str(w) for w in self.warnings],
        }


# --- building + semantic checks ---------------------------------------------------------------


def _split_point(ref: str) -> tuple[str, str]:
    node, _, obj = ref.partition("/")
    return node, obj


def _resolve_file(value: str | None, base_dir: Path | None) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    if not p.is_absolute() and base_dir is not None:
        p = (base_dir / p).resolve()
    return p


def _build(doc: dict[str, Any], base_dir: Path | None) -> System:
    nodes = []
    for n in doc["nodes"]:
        nodes.append(NodeSpec(
            name=n["name"], board=n["board"], transport=Transport.from_dict(n["transport"]),
            device=dict(n["device"]), bacnet_address=n.get("bacnet_address"),
            network=dict(n["network"]) if "network" in n else None,
            bacnet=dict(n["bacnet"]) if "bacnet" in n else None,
            io=[dict(p) for p in n.get("io", [])],
        ))
    apps = []
    for a in doc.get("apps", []):
        source = _resolve_file(a.get("source"), base_dir)
        wasm = _resolve_file(a.get("wasm"), base_dir)
        apps.append(AppSpec(
            name=a["name"], node=a["node"], source=source, wasm=wasm,
            aot=bool(a.get("aot", False)), autostart=bool(a.get("autostart", True)),
            period_ms=int(a.get("period_ms", 1000)), heap_kb=int(a.get("heap_kb", 8)),
            stack_kb=int(a.get("stack_kb", 4)),
            perms=list(a["perms"]) if "perms" in a else None,
            params=dict(a.get("params", {})),
            kind=detect_app_kind(a["name"], a.get("source"), a.get("wasm")),
        ))
    links = []
    for i, lk in enumerate(doc.get("links", [])):
        src_node, src_obj = _split_point(lk["from"])
        dst_node, dst_obj = _split_point(lk["to"])
        try:
            st, si = enums.parse_object_ref(src_obj)
            dt, di = enums.parse_object_ref(dst_obj)
        except ValueError:
            st = si = dt = di = -1
        links.append(LinkSpec(
            index=i, from_ref=lk["from"], to_ref=lk["to"], src_node=src_node, src_type=st,
            src_instance=si, dst_node=dst_node, dst_type=dt, dst_instance=di,
            mode=lk.get("mode", "cov"), period_ms=int(lk.get("period_ms", 1000)),
            priority=int(lk.get("priority", 0)), scale=float(lk.get("scale", 1.0)),
            offset=float(lk.get("offset", 0.0)),
        ))
    tests = [TestSpec(name=t["name"], steps=[dict(s) for s in t["steps"]])
             for t in doc.get("tests", [])]
    meta = doc["metadata"]
    return System(name=meta["name"], description=meta.get("description", ""), nodes=nodes,
                  apps=apps, links=links, tests=tests, doc=doc, base_dir=base_dir)


class _Checker:
    def __init__(self, system: System, catalogs: Mapping[str, Any] | None,
                 builtin_catalogs: bool, check_files: bool) -> None:
        self.s = system
        self.issues: list[Issue] = []
        self.check_files = check_files
        self.catalogs: dict[str, tuple[Catalog, Severity]] = {}
        for n in system.nodes:
            cat = None
            if catalogs:
                cat = catalogs.get(n.name, catalogs.get(n.board))
            if cat is not None:
                self.catalogs[n.name] = (normalize_catalog(cat), "error")
            elif builtin_catalogs:
                bc = builtin_catalog(n.board)
                if bc:
                    self.catalogs[n.name] = (bc, "warning")

    def err(self, path: str, msg: str) -> None:
        self.issues.append(Issue(path, msg, "error"))

    def warn(self, path: str, msg: str) -> None:
        self.issues.append(Issue(path, msg, "warning"))

    def run(self) -> list[Issue]:
        self.nodes()
        self.apps()
        self.links()
        self.objects()
        self.tests()
        return self.issues

    # -- nodes
    def nodes(self) -> None:
        seen: dict[str, int] = {}
        instances: dict[int, str] = {}
        for i, n in enumerate(self.s.nodes):
            if n.name in seen:
                self.err(_ptr("nodes", i, "name"),
                         f"duplicate node name {n.name!r} (also /nodes/{seen[n.name]})")
            seen.setdefault(n.name, i)
            if n.instance in instances:
                self.err(_ptr("nodes", i, "device", "instance"),
                         f"device instance {n.instance} already used by node "
                         f"{instances[n.instance]!r}")
            instances.setdefault(n.instance, n.name)
            if n.is_sim and n.board not in SIM_BOARDS:
                self.err(_ptr("nodes", i, "transport", "kind"),
                         f"transport kind 'sim' needs a native_sim board, not {n.board!r}")
            if n.bacnet_address:
                host, _, port = n.bacnet_address.partition(":")
                if not host or (port and not port.isdigit()):
                    self.err(_ptr("nodes", i, "bacnet_address"),
                             f"invalid bacnet_address {n.bacnet_address!r} (host[:port])")
                elif port and int(port) != n.bacnet_port:
                    self.warn(_ptr("nodes", i, "bacnet_address"),
                              f"port {port} differs from bacnet.udp_port {n.bacnet_port} (fine "
                              "behind NAT/port forwarding, else set bacnet.udp_port)")
            self.io_points(i, n)

    def io_points(self, i: int, n: NodeSpec) -> None:
        cat = self.catalogs.get(n.name)
        channels: dict[str, int] = {}
        if len(n.io) > 32:
            self.err(_ptr("nodes", i, "io"), f"{len(n.io)} IO points, io.json allows 32")
        for j, p in enumerate(n.io):
            where = _ptr("nodes", i, "io", j, "channel")
            ch = p["channel"]
            if ch in channels:
                self.warn(where, f"channel {ch!r} is bound twice (also io/{channels[ch]})")
            channels.setdefault(ch, j)
            if cat is None:
                continue
            catalog, sev = cat
            if ch not in catalog:
                self.issues.append(Issue(where, f"channel {ch!r} not in the IO catalog of "
                                         f"{n.board} ({', '.join(sorted(catalog))})", sev))
                continue
            kind = catalog[ch]
            if kind and p["type"] not in CHANNEL_KIND_TYPES.get(kind, frozenset()):
                self.issues.append(Issue(
                    _ptr("nodes", i, "io", j, "type"),
                    f"{p['type']} cannot be bound to {kind} channel {ch!r} (allowed: "
                    f"{', '.join(sorted(CHANNEL_KIND_TYPES[kind]))})", sev))

    # -- apps
    def apps(self) -> None:
        per_node: dict[str, dict[str, int]] = {}
        for i, a in enumerate(self.s.apps):
            if not self.s.has_node(a.node):
                self.err(_ptr("apps", i, "node"), f"unknown node {a.node!r}")
                continue
            names = per_node.setdefault(a.node, {})
            if a.name in names:
                self.err(_ptr("apps", i, "name"),
                         f"duplicate app name {a.name!r} on node {a.node!r}")
            names.setdefault(a.name, i)
            if a.name == "link" or re.fullmatch(r"link-\d+", a.name):
                if self.s.links_to(a.node):
                    self.err(_ptr("apps", i, "name"),
                             f"app name {a.name!r} is reserved for the uc-link instances "
                             f"generated from links to node {a.node!r}")
            if self.check_files:
                for key, file in (("source", a.source), ("wasm", a.wasm)):
                    if file is not None and not file.is_file():
                        self.err(_ptr("apps", i, key), f"file not found: {file}")
            if a.perms is not None and len(set(a.perms)) != len(a.perms):
                self.warn(_ptr("apps", i, "perms"), "duplicate permissions")
            for key, value in a.params.items():
                text = _stringify(value)
                if len(text) > 95:
                    self.err(_ptr("apps", i, "params", key),
                             f"value longer than 95 characters ({len(text)})")
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,23}", key):
                    self.err(_ptr("apps", i, "params", key),
                             "parameter keys are 1..23 characters of [A-Za-z0-9_.-]")
            if len(a.params) > 16:
                self.err(_ptr("apps", i, "params"), f"{len(a.params)} parameters, max. 16")
        for node in self.s.nodes:
            count = len(self.s.apps_on(node.name))
            links = len(self.s.links_to(node.name))
            count += -(-links // LINKS_PER_APP)
            where = _ptr("nodes", self.s.nodes.index(node))
            if count > APPS_SCHEMA_MAX:
                self.err(where, f"{count} applications (incl. uc-link instances) on node "
                         f"{node.name!r}, apps.json allows {APPS_SCHEMA_MAX}")
            elif count > FIRMWARE_APPS_MAX_DEFAULT:
                self.warn(where, f"{count} applications on node {node.name!r}: the firmware "
                          f"default CONFIG_UC_APPS_MAX is {FIRMWARE_APPS_MAX_DEFAULT}")

    # -- links
    def links(self) -> None:
        for lk in self.s.links:
            base = ("links", lk.index)
            for key, node, ref in (("from", lk.src_node, lk.from_ref),
                                   ("to", lk.dst_node, lk.to_ref)):
                if not self.s.has_node(node):
                    self.err(_ptr(*base, key), f"unknown node {node!r} in {ref!r}")
                _, obj = _split_point(ref)
                try:
                    enums.parse_object_ref(obj)
                except ValueError as exc:
                    self.err(_ptr(*base, key), str(exc))
            if lk.dst_type >= 0 and lk.dst_type not in WRITABLE_TYPES:
                self.err(_ptr(*base, "to"),
                         f"destination {enums.object_type_name(lk.dst_type)} is not writable "
                         "(use analog-/binary-/multi-state- output or value objects)")
            if (lk.src_node, lk.src_type, lk.src_instance) == (lk.dst_node, lk.dst_type,
                                                              lk.dst_instance):
                self.err(_ptr(*base), "link copies a point onto itself")

    # -- BACnet object collisions per node
    def objects(self) -> None:
        for i, n in enumerate(self.s.nodes):
            creators: dict[tuple[int, int], str] = {}
            io_objs: set[tuple[int, int]] = set()
            app_objs: dict[tuple[int, int], str] = {}
            for j, p in enumerate(n.io):
                try:
                    key = (enums.object_type_number(p["type"]), int(p["instance"]))
                except ValueError:
                    continue
                who = f"io point {p['channel']!r}"
                if key in creators:
                    self.err(_ptr("nodes", i, "io", j, "instance"),
                             f"{enums.format_object_ref(*key)} already defined by {creators[key]}")
                    continue
                creators[key] = who
                io_objs.add(key)
            for ai, a in enumerate(self.s.apps):
                if a.node != n.name:
                    continue
                for claim in a.object_claims():
                    key = (claim.obj_type, claim.instance)
                    who = f"app {a.name!r} ({claim.role})"
                    if key in creators:
                        self.err(_ptr("apps", ai, "params"),
                                 f"{claim.ref} created by {who} collides with {creators[key]} "
                                 f"on node {n.name!r}")
                        continue
                    creators[key] = who
                    app_objs[key] = who
            dests: dict[tuple[int, int], LinkSpec] = {}
            for lk in self.s.links_to(n.name):
                key = (lk.dst_type, lk.dst_instance)
                where = _ptr("links", lk.index, "to")
                if lk.dst_type < 0:
                    continue
                if key in dests and dests[key].priority == lk.priority:
                    self.err(where, f"{lk.to_ref} is also written by link "
                             f"/links/{dests[key].index} at the same priority")
                dests.setdefault(key, lk)
                if key in app_objs:
                    self.warn(where, f"{lk.to_ref} is created by {app_objs[key]}; the uc-link "
                              "instance may create it first depending on the start order")
                elif key not in io_objs and lk.dst_type not in VALUE_TYPES:
                    self.warn(where, f"{lk.to_ref} is neither an IO point nor created by an "
                              "app on this node; uc-link only creates analog/binary/multi-state "
                              "value objects")
            # link sources on the node itself must exist there as well
            for lk in self.s.links:
                if lk.src_node != n.name or lk.src_type < 0:
                    continue
                key = (lk.src_type, lk.src_instance)
                if key not in creators and key not in dests:
                    self.warn(_ptr("links", lk.index, "from"),
                              f"{lk.from_ref} is not an IO point or app object of node "
                              f"{n.name!r} (fine if created otherwise)")

    # -- tests
    def tests(self) -> None:
        names: set[str] = set()
        for ti, t in enumerate(self.s.tests):
            if t.name in names:
                self.err(_ptr("tests", ti, "name"), f"duplicate test name {t.name!r}")
            names.add(t.name)
            for si, step in enumerate(t.steps):
                for kind, body in step.items():
                    where = _ptr("tests", ti, "steps", si, kind)
                    if kind not in STEP_KINDS:
                        self.err(where, f"unknown test step {kind!r} (one of "
                                 f"{', '.join(STEP_KINDS)})")
                    elif kind in ("force", "release"):
                        self._test_channel(where, body["node"], body["channel"])
                    elif kind in ("write", "expect"):
                        node, obj = _split_point(body["point"])
                        if not self.s.has_node(node):
                            self.err(where + "/point", f"unknown node {node!r}")
                        try:
                            enums.parse_object_ref(obj)
                        except ValueError as exc:
                            self.err(where + "/point", str(exc))
                        prop = body.get("property", "present-value")
                        try:
                            enums.property_number(prop)
                        except ValueError as exc:
                            self.err(where + "/property", str(exc))

    def _test_channel(self, where: str, node: str, channel: str) -> None:
        if not self.s.has_node(node):
            self.err(where + "/node", f"unknown node {node!r}")
            return
        cat = self.catalogs.get(node)
        if cat is not None and channel not in cat[0]:
            self.issues.append(Issue(where + "/channel",
                                     f"channel {channel!r} not in the IO catalog of node "
                                     f"{node!r}", cat[1]))


def check_system(
    doc: Any,
    *,
    base_dir: Path | str | None = None,
    catalogs: Mapping[str, Any] | None = None,
    builtin_catalogs: bool = True,
    check_files: bool | None = None,
) -> tuple[System | None, list[Issue]]:
    """Resolve, validate and check a parsed manifest.

    Args:
        doc: the parsed manifest.
        base_dir: directory relative ``source``/``wasm`` paths refer to.
        catalogs: IO catalogs by node name or board (``io_catalog()`` responses,
            ``{channel: kind}`` maps or channel name lists); unknown channels
            are errors.
        builtin_catalogs: check nodes without an explicit catalog against
            ``firmware/boards/io/<board>.dtsi`` (findings are warnings).
        check_files: require ``source``/``wasm`` files to exist (default:
            when ``base_dir`` is given).

    Returns:
        ``(system, issues)``; ``system`` is ``None`` when there are errors.
    """
    base = Path(base_dir).resolve() if base_dir is not None else None
    if not isinstance(doc, Mapping):
        return None, [Issue("", "manifest must be a mapping")]
    resolved, issues = resolve_placeholders(dict(doc))
    issues += validate_document("system", resolved)
    if any(i.severity == "error" for i in issues):
        return None, issues
    system = _build(resolved, base)
    if check_files is None:
        check_files = base is not None
    issues += _Checker(system, catalogs, builtin_catalogs, check_files).run()
    system.warnings = [i for i in issues if i.severity == "warning"]
    if any(i.severity == "error" for i in issues):
        return None, issues
    return system, issues


def load_system_dict(doc: Any, *, base_dir: Path | str | None = None,
                     catalogs: Mapping[str, Any] | None = None, builtin_catalogs: bool = True,
                     check_files: bool | None = None, source: str | None = None) -> System:
    """Like :func:`load_system` for an already parsed manifest."""
    system, issues = check_system(doc, base_dir=base_dir, catalogs=catalogs,
                                  builtin_catalogs=builtin_catalogs, check_files=check_files)
    if system is None:
        raise ManifestError(issues, source)
    return system


def load_system(path: Path | str, *, catalogs: Mapping[str, Any] | None = None,
                builtin_catalogs: bool = True, check_files: bool = True) -> System:
    """Load and validate a system manifest (YAML or JSON).

    Raises:
        ManifestError: with every error found.
    """
    p = Path(path).expanduser().resolve()
    doc = read_document(p)
    system = load_system_dict(doc, base_dir=p.parent, catalogs=catalogs,
                              builtin_catalogs=builtin_catalogs, check_files=check_files,
                              source=str(p))
    system.path = p
    return system


def validation_report(path_or_doc: Path | str | Mapping[str, Any], *,
                      catalogs: Mapping[str, Any] | None = None,
                      builtin_catalogs: bool = True) -> dict[str, Any]:
    """JSON-friendly validation result: ``{"ok", "errors", "warnings", "system"}``."""
    try:
        if isinstance(path_or_doc, Mapping):
            system, issues = check_system(path_or_doc, catalogs=catalogs,
                                          builtin_catalogs=builtin_catalogs)
        else:
            p = Path(path_or_doc).expanduser().resolve()
            system, issues = check_system(read_document(p), base_dir=p.parent,
                                          catalogs=catalogs, builtin_catalogs=builtin_catalogs)
            if system is not None:
                system.path = p
    except ManifestError as exc:
        system, issues = None, exc.issues
    return {
        "ok": system is not None,
        "errors": [i.to_dict() for i in issues if i.severity == "error"],
        "warnings": [i.to_dict() for i in issues if i.severity == "warning"],
        "system": system.summary() if system is not None else None,
    }
