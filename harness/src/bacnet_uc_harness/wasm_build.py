# SPDX-License-Identifier: Apache-2.0
"""Build BACnet-uc WebAssembly applications and inspect modules.

- :func:`build_c` compiles C sources with ``wasm/sdk/uc-cc`` (the SDK
  compiler wrapper) or, when the SDK wrapper is missing, with clang and the
  flags documented in ``wasm/sdk/include/bacnet_uc.h``.
- :func:`parse_wasm` is a small pure-Python reader of the WebAssembly
  binary format (types, imports, exports); :func:`check_module` verifies a
  module against the application ABI: every import comes from
  ``bacnet_uc`` (names declared with ``UC_IMPORT`` in ``bacnet_uc.h``) or
  ``env`` (libc-builtin functions declared in ``uc_libc.h``), and
  ``uc_app_api_version`` is exported.
- :func:`aot_compile` runs ``wasm/sdk/uc-aot`` or ``wamrc`` for a board.
- :func:`sdk_info` summarises the host API for humans and agents.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from bacnet_uc_harness import paths
from bacnet_uc_harness.errors import HarnessError

IMPORT_MODULE = "bacnet_uc"
LIBC_MODULE = "env"
REQUIRED_EXPORT = "uc_app_api_version"
WASM_MAGIC = b"\x00asm"
AOT_MAGIC = b"\x00aot"

#: Build flags from the bacnet_uc.h header comment (fallback without uc-cc).
HEADER_CFLAGS = ("--target=wasm32", "-nostdlib")
HEADER_LDFLAGS = (
    "-Wl,--no-entry", "-Wl,--allow-undefined", "-Wl,--strip-debug",
    "-Wl,--export=__heap_base", "-Wl,--export=__data_end",
    "-Wl,-z,stack-size=4096", "-Wl,--initial-memory=65536",
)

#: board -> (wamrc --target, --cpu, --target-abi); must match CONFIG_WAMR_BUILD_TARGET
AOT_TARGETS: dict[str, tuple[str, str, str | None]] = {
    "nucleo_f767zi": ("thumbv7em", "cortex-m7", "eabihf"),
    "frdm_mcxn947/mcxn947/cpu0": ("thumbv8m.main", "cortex-m33", "eabihf"),
    "native_sim/native/64": ("x86_64", "x86-64", None),
}
BOARD_ALIASES = {
    "nucleo": "nucleo_f767zi",
    "f767": "nucleo_f767zi",
    "frdm_mcxn947": "frdm_mcxn947/mcxn947/cpu0",
    "mcxn947": "frdm_mcxn947/mcxn947/cpu0",
    "frdm_mcxn947_mcxn947_cpu0": "frdm_mcxn947/mcxn947/cpu0",
    "native_sim": "native_sim/native/64",
    "native_sim_native_64": "native_sim/native/64",
}

VALTYPES = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64", 0x7B: "v128", 0x70: "funcref",
            0x6F: "externref"}
KIND_NAMES = {0: "func", 1: "table", 2: "memory", 3: "global", 4: "tag"}


class WasmError(HarnessError):
    """Malformed module or ABI violation."""

    def __init__(self, message: str, errors: Sequence[str] = (), output: str = "") -> None:
        self.errors = list(errors)
        self.output = output
        super().__init__(message)


class WasmBuildError(WasmError):
    """Compilation, linking or the post-link check failed. ``output`` holds the
    compiler output."""


# --- binary format --------------------------------------------------------------------------


@dataclass
class WasmImport:
    module: str
    name: str
    kind: str  # func/table/memory/global/tag
    signature: str | None = None  # "(i32, i32) -> (i32)" for functions

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"


@dataclass
class WasmExport:
    name: str
    kind: str
    index: int


@dataclass
class WasmModuleInfo:
    size: int
    imports: list[WasmImport] = field(default_factory=list)
    exports: list[WasmExport] = field(default_factory=list)
    sections: list[int] = field(default_factory=list)

    def function_exports(self) -> list[str]:
        return [e.name for e in self.exports if e.kind == "func"]


class _Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise WasmError("unexpected end of module")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def uleb(self) -> int:
        result = shift = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise WasmError("LEB128 value too long")

    def bytes(self, n: int) -> bytes:
        if self.pos + n > self.end:
            raise WasmError("unexpected end of module")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def name(self) -> str:
        raw = self.bytes(self.uleb())
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise WasmError("name is not UTF-8") from None

    def limits(self) -> None:
        flags = self.byte()
        self.uleb()
        if flags & 1:
            self.uleb()


def _valtype(b: int) -> str:
    return VALTYPES.get(b, f"0x{b:02x}")


def parse_wasm(data: bytes) -> WasmModuleInfo:
    """Read the type, import and export sections of a WebAssembly binary.

    Raises:
        WasmError: not a (version 1) WebAssembly module or truncated.
    """
    if len(data) < 8 or data[:4] != WASM_MAGIC:
        raise WasmError("not a WebAssembly module (bad magic)")
    if data[4:8] != b"\x01\x00\x00\x00":
        raise WasmError(f"unsupported WebAssembly version {data[4:8].hex()}")
    info = WasmModuleInfo(size=len(data))
    types: list[str] = []
    r = _Reader(data, 8)
    while r.pos < r.end:
        sid = r.byte()
        size = r.uleb()
        start = r.pos
        if start + size > len(data):
            raise WasmError(f"section {sid} exceeds the module")
        info.sections.append(sid)
        s = _Reader(data, start, start + size)
        if sid == 1:  # type
            for _ in range(s.uleb()):
                form = s.byte()
                if form != 0x60:
                    types.append("?")
                    break
                params = [_valtype(s.byte()) for _ in range(s.uleb())]
                results = [_valtype(s.byte()) for _ in range(s.uleb())]
                types.append(f"({', '.join(params)}) -> ({', '.join(results)})")
        elif sid == 2:  # import
            for _ in range(s.uleb()):
                module = s.name()
                name = s.name()
                kind = s.byte()
                sig = None
                if kind == 0:
                    idx = s.uleb()
                    sig = types[idx] if idx < len(types) else None
                elif kind == 1:
                    s.byte()
                    s.limits()
                elif kind == 2:
                    s.limits()
                elif kind == 3:
                    s.byte()
                    s.byte()
                elif kind == 4:
                    s.byte()
                    s.uleb()
                else:
                    raise WasmError(f"unknown import kind {kind}")
                info.imports.append(WasmImport(module, name, KIND_NAMES[kind], sig))
        elif sid == 7:  # export
            for _ in range(s.uleb()):
                name = s.name()
                kind = s.byte()
                idx = s.uleb()
                info.exports.append(WasmExport(name, KIND_NAMES.get(kind, str(kind)), idx))
        r.pos = start + size
    return info


# --- ABI knowledge from the SDK headers ------------------------------------------------------

_UC_IMPORT = re.compile(r"(?:(/\*\*(?:(?!\*/).)*\*/)\s*)?^UC_IMPORT\((\w+)\)[ \t]*\n([^;]*);",
                        re.S | re.M)
_PERM_SECTION = re.compile(r"Host imports:\s*(.*?)\s*(?:\(permission \"([a-z.]+)\"\))?\s*\*/")


def _clean_comment(text: str | None) -> str:
    if not text:
        return ""
    body = text.strip()[3:-2]
    lines = [re.sub(r"^\s*\*\s?", "", ln).rstrip() for ln in body.splitlines()]
    return " ".join(ln.strip() for ln in lines if ln.strip())


@dataclass(frozen=True)
class HostFunction:
    name: str
    prototype: str
    doc: str
    section: str
    permission: str | None


@lru_cache(maxsize=4)
def _host_functions(header_text: str) -> tuple[HostFunction, ...]:
    out = []
    sections = [(m.start(), m.group(1), m.group(2)) for m in _PERM_SECTION.finditer(header_text)]
    for m in _UC_IMPORT.finditer(header_text):
        pos = m.start(2)
        section, perm = "", None
        for start, title, p in sections:
            if start < pos:
                section, perm = title, p
        proto = " ".join(m.group(3).split())
        out.append(HostFunction(m.group(2), proto, _clean_comment(m.group(1)), section, perm))
    return tuple(out)


def _read_header(path: Path | None = None) -> str:
    p = path or paths.bacnet_uc_header()
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"cannot read {p}: {exc}") from exc


def host_functions(header: Path | None = None) -> list[HostFunction]:
    """The ``UC_IMPORT`` functions of ``bacnet_uc.h`` in declaration order."""
    return list(_host_functions(_read_header(header)))


def known_imports(header: Path | None = None) -> set[str]:
    """Names of the host functions (``UC_IMPORT(name)`` in ``bacnet_uc.h``)."""
    return {f.name for f in host_functions(header)}


_LIBC_DECL = re.compile(r"^\s*(?!static\b|typedef\b|#)[A-Za-z_][\w\s\*]*?\b([a-z_][a-z0-9_]*)\s*\(",
                        re.M)


def known_env_imports(libc_header: Path | None = None) -> set[str]:
    """libc-builtin functions an app may import from ``env`` (the declarations
    of the ``__wasm__`` branch of ``uc_libc.h``)."""
    p = libc_header or paths.wasm_include_dir() / "uc_libc.h"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return set()
    start = text.find("#else /* __wasm__ */")
    end = text.find("#endif /* __wasm__ */", start)
    if start < 0 or end < 0:
        return set()
    return {m.group(1) for m in _LIBC_DECL.finditer(text[start:end])}


def perms_for_imports(imports: Iterable[str], remote: bool = True,
                      header: Path | None = None) -> list[str]:
    """Permissions the imported host functions need. ``imports`` are
    ``module.name`` or bare names. Functions of the remote section need
    ``bacnet.remote`` when ``remote`` else ``bacnet.local`` (the host accepts
    ``bacnet.local`` for requests to the local device)."""
    by_name = {f.name: f.permission for f in host_functions(header)}
    perms: set[str] = set()
    for imp in imports:
        module, _, name = imp.rpartition(".")
        if module not in ("", IMPORT_MODULE):
            continue
        perm = by_name.get(name)
        if name.endswith("_unsubscribe"):
            continue  # cancelling needs no permission
        if perm == "bacnet.remote":
            perms.add("bacnet.remote" if remote else "bacnet.local")
        elif perm:
            perms.add(perm)
    order = ("bacnet.local", "bacnet.remote", "io", "kv")
    return [p for p in order if p in perms]


@dataclass
class ModuleCheck:
    ok: bool
    errors: list[str]
    warnings: list[str]
    imports: list[str]
    exports: list[str]
    perms: list[str]
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "imports": self.imports, "exports": self.exports, "perms": self.perms,
                "size": self.size}


def check_module(data: bytes | Path | str) -> ModuleCheck:
    """Check a module against the application ABI (see module docstring)."""
    raw = Path(data).read_bytes() if isinstance(data, Path | str) else bytes(data)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        info = parse_wasm(raw)
    except WasmError as exc:
        return ModuleCheck(False, [str(exc)], [], [], [], [], len(raw))
    host = known_imports()
    libc = known_env_imports()
    for imp in info.imports:
        where = f"import {imp.qualname}"
        if imp.kind != "func":
            errors.append(f"{where}: {imp.kind} imports are not provided by the host")
        elif imp.module == IMPORT_MODULE:
            if imp.name not in host:
                errors.append(f"{where}: unknown host function (not declared in bacnet_uc.h)")
        elif imp.module == LIBC_MODULE:
            if libc and imp.name not in libc:
                errors.append(f"{where}: not a libc-builtin function of uc_libc.h")
        else:
            errors.append(f"{where}: import module must be {IMPORT_MODULE!r} or {LIBC_MODULE!r}")
    exports = info.function_exports()
    if REQUIRED_EXPORT not in exports:
        errors.append(f"{REQUIRED_EXPORT} is not exported (use UC_APP_DECLARE())")
    if "uc_app_init" not in exports and "uc_app_tick" not in exports:
        warnings.append("neither uc_app_init nor uc_app_tick is exported: the app does nothing")
    names = [i.qualname for i in info.imports]
    return ModuleCheck(not errors, errors, warnings, names, exports,
                       perms_for_imports(names), len(raw))


# --- building ------------------------------------------------------------------------------


@dataclass
class BuildResult:
    path: Path
    size: int
    imports: list[str]
    exports: list[str]
    warnings: list[str]
    sha256: str = ""
    perms: list[str] = field(default_factory=list)
    tool: str = ""
    command: list[str] = field(default_factory=list)
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "size": self.size, "sha256": self.sha256,
                "imports": self.imports, "exports": self.exports, "perms": self.perms,
                "warnings": self.warnings, "tool": self.tool, "command": self.command}


def _opt_level(opt: str) -> str:
    level = opt.strip().removeprefix("-O") or "2"
    if level not in ("0", "1", "2", "3", "s", "z"):
        raise HarnessError(f"invalid optimisation level {opt!r}")
    return level


def _define_args(defines: Mapping[str, Any] | Iterable[str] | None) -> list[str]:
    if not defines:
        return []
    if isinstance(defines, Mapping):
        return [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in defines.items()]
    return [d if d.startswith("-D") else f"-D{d}" for d in defines]


def _uc_cc() -> Path | None:
    try:
        tool = paths.wasm_sdk_dir() / "uc-cc"
    except HarnessError:
        return None
    return tool if tool.is_file() else None


def build_flags() -> dict[str, Any]:
    """Compile/link flags used for applications (from ``uc-cc --print-flags``
    when the SDK wrapper is present, else from the header comment)."""
    tool = _uc_cc()
    if tool is not None:
        res = subprocess.run([sys.executable, str(tool), "--print-flags"], capture_output=True,
                             text=True, check=False, timeout=30)
        if res.returncode == 0:
            flags: dict[str, Any] = {"tool": str(tool)}
            for line in res.stdout.splitlines():
                key, _, value = line.partition("=")
                flags[key.strip().lower()] = value.strip()
            return flags
    inc = paths.wasm_include_dir()
    return {"tool": "clang", "cflags": " ".join([*HEADER_CFLAGS, "-O2", f"-I{inc}"]),
            "ldflags": " ".join(HEADER_LDFLAGS)}


def build_c(
    sources: Sequence[Path | str],
    out: Path | str,
    *,
    defines: Mapping[str, Any] | Iterable[str] | None = None,
    opt: str = "-O2",
    includes: Sequence[Path | str] = (),
    heap_kb: int = 8,
    extra_flags: Sequence[str] = (),
    clang: str | None = None,
    timeout: float = 300.0,
) -> BuildResult:
    """Compile C sources into a checked WebAssembly application.

    Uses ``wasm/sdk/uc-cc`` when present (it links against the exact
    libc-builtin symbol list and runs the SDK checker), else clang with the
    flags of the ``bacnet_uc.h`` header comment. The result is then parsed
    and checked with :func:`check_module` in both cases.

    Raises:
        WasmBuildError: compiler/linker error or ABI violation (``errors`` and
            ``output`` carry the details).
    """
    srcs = [Path(s).expanduser().resolve() for s in sources]
    if not srcs:
        raise WasmBuildError("no source files")
    missing = [str(s) for s in srcs if not s.is_file()]
    if missing:
        raise WasmBuildError(f"source not found: {', '.join(missing)}")
    target = Path(out).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    level = _opt_level(opt)
    inc_args = [f"-I{Path(i).expanduser().resolve()}" for i in includes]
    tool_path = _uc_cc()
    env = dict(os.environ)
    if clang:
        env["UC_CLANG"] = clang
    if tool_path is not None:
        cmd = [sys.executable, str(tool_path), "-q", "-o", str(target), "-O", level,
               "--heap-kb", str(heap_kb), *_define_args(defines), *inc_args, *extra_flags,
               *map(str, srcs)]
        tool = "uc-cc"
    else:
        cc = clang or env.get("UC_CLANG", "clang")
        cmd = [cc, *HEADER_CFLAGS, f"-O{level}", f"-I{paths.wasm_include_dir()}", *inc_args,
               *_define_args(defines), *extra_flags, *HEADER_LDFLAGS, "-o", str(target),
               *map(str, srcs)]
        tool = "clang"
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout,
                             env=env)
    except FileNotFoundError as exc:
        raise WasmBuildError(f"{cmd[0]} not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WasmBuildError(f"build timed out after {timeout:.0f} s") from exc
    output = (res.stdout + res.stderr).strip()
    if res.returncode != 0 or not target.is_file():
        errs = [ln for ln in output.splitlines() if "error" in ln] or [output[-2000:]]
        raise WasmBuildError(f"{tool} failed (exit {res.returncode})", errs, output)
    data = target.read_bytes()
    chk = check_module(data)
    if not chk.ok:
        target.unlink(missing_ok=True)
        raise WasmBuildError("module violates the BACnet-uc application ABI", chk.errors, output)
    warnings = [ln for ln in output.splitlines() if "warning" in ln.lower()] + chk.warnings
    return BuildResult(path=target, size=len(data), imports=chk.imports, exports=chk.exports,
                       warnings=warnings, sha256=hashlib.sha256(data).hexdigest(),
                       perms=chk.perms, tool=tool, command=cmd, output=output)


def build_source_text(source: str, out: Path | str, *, name: str = "app", **kwargs: Any
                      ) -> BuildResult:
    """Compile inline C source text (written to a temporary file)."""
    with tempfile.TemporaryDirectory(prefix="bacnet-uc-src-") as tmp:
        src = Path(tmp) / f"{name}.c"
        src.write_text(source, encoding="utf-8")
        return build_c([src], out, **kwargs)


# --- AOT --------------------------------------------------------------------------------------


def normalize_board(board: str) -> str:
    return BOARD_ALIASES.get(board, board)


@dataclass
class AotResult:
    path: Path
    size: int
    board: str
    target: str
    sha256: str
    tool: str
    command: list[str]
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "size": self.size, "board": self.board,
                "target": self.target, "sha256": self.sha256, "tool": self.tool,
                "command": self.command}


def find_wamrc(explicit: str | None = None) -> str | None:
    for cand in (explicit, os.environ.get("WAMRC"), "/opt/wamrc/wamrc"):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return shutil.which("wamrc")


def wamrc_command(wasm: Path, out: Path, board: str, opt_level: int = 2,
                  wamrc: str = "wamrc") -> list[str]:
    """Plain wamrc command line for a board (used without ``uc-aot``)."""
    board = normalize_board(board)
    if board not in AOT_TARGETS:
        raise HarnessError(f"no AOT target for board {board!r} (known: {', '.join(AOT_TARGETS)})")
    target, cpu, abi = AOT_TARGETS[board]
    cmd = [wamrc, f"--target={target}", f"--cpu={cpu}"]
    if abi:
        cmd.append(f"--target-abi={abi}")
    cmd += [f"--opt-level={opt_level}", "--bounds-checks=1", "--disable-simd",
            "--disable-ref-types", "-o", str(out), str(wasm)]
    return cmd


def aot_compile(wasm: Path | str, board: str, out: Path | str | None = None, *,
                opt_level: int = 2, wamrc: str | None = None, timeout: float = 300.0
                ) -> AotResult:
    """Compile a module ahead of time for a board (``.aot``).

    Raises:
        WasmBuildError: wamrc missing or failed.
    """
    board = normalize_board(board)
    if board not in AOT_TARGETS:
        raise WasmBuildError(f"no AOT target for board {board!r} (known: "
                             f"{', '.join(AOT_TARGETS)})")
    src = Path(wasm).expanduser().resolve()
    if not src.is_file():
        raise WasmBuildError(f"module not found: {src}")
    target_path = (Path(out).expanduser().resolve() if out
                   else src.with_suffix(f".{paths.board_key(board)}.aot"))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    uc_aot = None
    try:
        cand = paths.wasm_sdk_dir() / "uc-aot"
        uc_aot = cand if cand.is_file() else None
    except HarnessError:
        pass
    if uc_aot is not None:
        cmd = [sys.executable, str(uc_aot), "--board", board, "-O", str(opt_level), "-o",
               str(target_path), str(src)]
        if wamrc:
            cmd[3:3] = ["--wamrc", wamrc]
        tool = "uc-aot"
    else:
        exe = find_wamrc(wamrc)
        if exe is None:
            raise WasmBuildError("wamrc not found (set WAMRC or install WAMR 2.4.5 wamrc)")
        cmd = wamrc_command(src, target_path, board, opt_level, exe)
        tool = "wamrc"
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise WasmBuildError(f"{tool} failed: {exc}") from exc
    output = (res.stdout + res.stderr).strip()
    if res.returncode != 0 or not target_path.is_file():
        raise WasmBuildError(f"{tool} failed (exit {res.returncode})", [output[-2000:]], output)
    data = target_path.read_bytes()
    if data[:4] != AOT_MAGIC:
        raise WasmBuildError(f"{target_path} is not an AOT file")
    return AotResult(path=target_path, size=len(data), board=board,
                     target=AOT_TARGETS[board][0], sha256=hashlib.sha256(data).hexdigest(),
                     tool=tool, command=cmd, output=output)


# --- SDK summary ----------------------------------------------------------------------------

_DEFINE = re.compile(r"#define\s+(UC_[A-Z0-9_]+)\s+\(?(-?(?:0x)?[0-9A-Fa-f]+)u?\)?"
                     r"(?:\s*/\*\*<\s*(.*?)\s*\*/)?")


def sdk_info() -> dict[str, Any]:
    """Summary of the application API (from ``bacnet_uc.h``) and the build setup."""
    text = _read_header()
    defines: dict[str, tuple[int, str]] = {}
    for m in _DEFINE.finditer(text):
        try:
            defines[m.group(1)] = (int(m.group(2), 0), m.group(3) or "")
        except ValueError:
            continue
    exports_doc = ""
    m = re.search(r"/\*\s*\n\s*\*\s*int32_t uc_app_init.*?\*/", text, re.S)
    if m:
        exports_doc = "\n".join(re.sub(r"^\s*\*\s?", "", ln) for ln in
                                m.group(0).strip("/*\n ").splitlines()).strip()
    major = defines.get("UC_API_VERSION_MAJOR", (0, ""))[0]
    minor = defines.get("UC_API_VERSION_MINOR", (0, ""))[0]
    try:
        header_path = str(paths.bacnet_uc_header())
    except HarnessError:
        header_path = ""
    return {
        "api_version": f"{major}.{minor}",
        "header": header_path,
        "import_module": IMPORT_MODULE,
        "host_functions": [
            {"name": f.name, "prototype": f.prototype, "doc": f.doc, "section": f.section,
             "permission": f.permission} for f in host_functions()
        ],
        "app_exports": exports_doc,
        "required_export": REQUIRED_EXPORT,
        "error_codes": {k: {"value": v, "doc": d} for k, (v, d) in defines.items()
                        if k.startswith("UC_ERR_") or k == "UC_OK"},
        "object_types": {k.removeprefix("UC_OBJ_"): v for k, (v, _) in defines.items()
                         if k.startswith("UC_OBJ_")},
        "properties": {k.removeprefix("UC_PROP_"): v for k, (v, _) in defines.items()
                       if k.startswith("UC_PROP_")},
        "libc_builtin": sorted(known_env_imports()),
        "permissions": ["bacnet.local", "bacnet.remote", "io", "kv"],
        "build": build_flags(),
        "aot_targets": {b: {"target": t, "cpu": c, "abi": a} for b, (t, c, a)
                        in AOT_TARGETS.items()},
        "notes": [
            "Every app must call UC_APP_DECLARE() (exports uc_app_api_version).",
            "Numeric property values cross the ABI as double; binary PVs are 0.0/1.0.",
            "Functions return >= 0 on success or a negative UC_ERR_* code.",
            "Parameters come from apps.json params (strings); read them with "
            "uc_param_get/uc_param_get_number (uc_param_num helper).",
            "IO channel values: di/do 0|1, ai millivolts, ao percent 0..100.",
        ],
    }
