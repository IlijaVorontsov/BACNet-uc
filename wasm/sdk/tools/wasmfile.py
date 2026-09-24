# SPDX-License-Identifier: Apache-2.0
"""Minimal WebAssembly binary reader for the BACnet-uc SDK tools.

Reads the sections a module checker needs (types, imports, functions,
tables, memories, globals, exports, data, code) and decodes every
instruction of every function body to find the WebAssembly features the
module uses and whether it contains memory.grow/memory.size (which stop
WAMR from shrinking the linear memory). Standard library only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

WASM_MAGIC = b"\0asm"
WASM_VERSION = 1
AOT_MAGIC = b"\0aot"

VALTYPES = {
    0x7F: "i32",
    0x7E: "i64",
    0x7D: "f32",
    0x7C: "f64",
    0x7B: "v128",
    0x70: "funcref",
    0x6F: "externref",
}

KIND_FUNC, KIND_TABLE, KIND_MEMORY, KIND_GLOBAL, KIND_TAG = range(5)
KIND_NAMES = {0: "func", 1: "table", 2: "memory", 3: "global", 4: "tag"}


class WasmError(Exception):
    """Malformed or unsupported module."""


@dataclass
class FuncType:
    params: tuple[str, ...]
    results: tuple[str, ...]

    def __str__(self) -> str:
        return f"({', '.join(self.params)}) -> ({', '.join(self.results)})"


@dataclass
class Limits:
    minimum: int
    maximum: int | None
    shared: bool = False
    memory64: bool = False


@dataclass
class Import:
    module: str
    name: str
    kind: int
    type_index: int = -1  # functions
    limits: Limits | None = None  # memories, tables


@dataclass
class Export:
    name: str
    kind: int
    index: int


@dataclass
class Global:
    valtype: str
    mutable: bool
    init_op: int  # opcode of the (first) init instruction
    init_value: int | float | None


@dataclass
class Module:
    size: int = 0
    types: list[FuncType] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    func_types: list[int] = field(default_factory=list)  # defined functions
    tables: list[Limits] = field(default_factory=list)
    memories: list[Limits] = field(default_factory=list)
    globals: list[Global] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    start: int | None = None
    data_bytes: int = 0
    data_segments: int = 0
    code_bytes: int = 0
    custom_sections: list[str] = field(default_factory=list)
    section_sizes: dict[str, int] = field(default_factory=dict)
    features: set[str] = field(default_factory=set)
    opcode_counts: dict[str, int] = field(default_factory=dict)
    decode_errors: list[str] = field(default_factory=list)

    # -- helpers -----------------------------------------------------------
    def imported(self, kind: int) -> list[Import]:
        return [i for i in self.imports if i.kind == kind]

    def function_type(self, func_index: int) -> FuncType:
        funcs = self.imported(KIND_FUNC)
        if func_index < len(funcs):
            return self.types[funcs[func_index].type_index]
        return self.types[self.func_types[func_index - len(funcs)]]

    def export(self, name: str) -> Export | None:
        for e in self.exports:
            if e.name == name:
                return e
        return None

    def global_by_index(self, index: int) -> Global | None:
        n_imp = len(self.imported(KIND_GLOBAL))
        if index < n_imp:
            return None
        index -= n_imp
        return self.globals[index] if index < len(self.globals) else None

    def exported_const_global(self, name: str) -> int | None:
        """Value of an exported immutable i32 global with an i32.const init."""
        e = self.export(name)
        if e is None or e.kind != KIND_GLOBAL:
            return None
        g = self.global_by_index(e.index)
        if g is None or g.mutable or g.valtype != "i32" or g.init_op != 0x41:
            return None
        return int(g.init_value) & 0xFFFFFFFF

    @property
    def grows_memory(self) -> bool:
        return bool(self.opcode_counts.get("memory.grow") or self.opcode_counts.get("memory.size"))


# ---------------------------------------------------------------------------
# Byte reader
# ---------------------------------------------------------------------------
class Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def eof(self) -> bool:
        return self.pos >= self.end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise WasmError(f"unexpected end of data at offset {self.pos:#x}")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def bytes(self, n: int) -> bytes:
        if self.pos + n > self.end:
            raise WasmError(f"unexpected end of data at offset {self.pos:#x}")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def u32(self) -> int:
        result = shift = 0
        for _ in range(5):
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
        raise WasmError(f"LEB128 u32 too long at offset {self.pos:#x}")

    def sleb(self, bits: int) -> int:
        result = shift = 0
        for _ in range((bits + 6) // 7):
            b = self.byte()
            result |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                if b & 0x40:
                    result -= 1 << shift
                return result
        raise WasmError(f"LEB128 s{bits} too long at offset {self.pos:#x}")

    def name(self) -> str:
        n = self.u32()
        try:
            return self.bytes(n).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WasmError("name is not UTF-8") from exc

    def valtype(self) -> str:
        b = self.byte()
        if b not in VALTYPES:
            raise WasmError(f"unknown value type {b:#x} at offset {self.pos - 1:#x}")
        return VALTYPES[b]

    def limits(self) -> Limits:
        flags = self.byte()
        if flags & ~0x07:
            raise WasmError(f"unknown limits flags {flags:#x}")
        memory64 = bool(flags & 0x04)
        read = (lambda: self.sleb(64) & 0xFFFFFFFFFFFFFFFF) if memory64 else self.u32
        minimum = read()
        maximum = read() if flags & 0x01 else None
        return Limits(minimum, maximum, bool(flags & 0x02), memory64)


# ---------------------------------------------------------------------------
# Instruction decoding
# ---------------------------------------------------------------------------
# Immediate kinds of single-byte opcodes. Opcodes missing here are unknown.
_NONE, _BLOCK, _U32, _U32X2, _BR_TABLE, _CALL_IND, _MEMARG, _MEMIDX, _S32, _S64, _F32, _F64, \
    _SELECT_T, _REFTYPE = range(14)

_OPS: dict[int, tuple[str, int, str | None]] = {
    0x00: ("unreachable", _NONE, None),
    0x01: ("nop", _NONE, None),
    0x02: ("block", _BLOCK, None),
    0x03: ("loop", _BLOCK, None),
    0x04: ("if", _BLOCK, None),
    0x05: ("else", _NONE, None),
    0x06: ("try", _BLOCK, "exception-handling"),
    0x07: ("catch", _U32, "exception-handling"),
    0x08: ("throw", _U32, "exception-handling"),
    0x09: ("rethrow", _U32, "exception-handling"),
    0x0B: ("end", _NONE, None),
    0x0C: ("br", _U32, None),
    0x0D: ("br_if", _U32, None),
    0x0E: ("br_table", _BR_TABLE, None),
    0x0F: ("return", _NONE, None),
    0x10: ("call", _U32, None),
    0x11: ("call_indirect", _CALL_IND, None),
    0x12: ("return_call", _U32, "tail-call"),
    0x13: ("return_call_indirect", _CALL_IND, "tail-call"),
    0x18: ("delegate", _U32, "exception-handling"),
    0x19: ("catch_all", _NONE, "exception-handling"),
    0x1A: ("drop", _NONE, None),
    0x1B: ("select", _NONE, None),
    0x1C: ("select_t", _SELECT_T, "reference-types"),
    0x20: ("local.get", _U32, None),
    0x21: ("local.set", _U32, None),
    0x22: ("local.tee", _U32, None),
    0x23: ("global.get", _U32, None),
    0x24: ("global.set", _U32, None),
    0x25: ("table.get", _U32, "reference-types"),
    0x26: ("table.set", _U32, "reference-types"),
    0x3F: ("memory.size", _MEMIDX, None),
    0x40: ("memory.grow", _MEMIDX, None),
    0x41: ("i32.const", _S32, None),
    0x42: ("i64.const", _S64, None),
    0x43: ("f32.const", _F32, None),
    0x44: ("f64.const", _F64, None),
    0xD0: ("ref.null", _REFTYPE, "reference-types"),
    0xD1: ("ref.is_null", _NONE, "reference-types"),
    0xD2: ("ref.func", _U32, "reference-types"),
}
for _op in range(0x28, 0x3F):  # loads and stores
    _OPS[_op] = (f"mem_{_op:#x}", _MEMARG, None)
for _op in range(0x45, 0xC0):  # MVP numeric instructions
    _OPS[_op] = (f"num_{_op:#x}", _NONE, None)
for _op in range(0xC0, 0xC5):
    _OPS[_op] = (f"extend_{_op:#x}", _NONE, "sign-ext")

# 0xFC prefix: sub-opcode -> (name, immediates, feature)
_FC_OPS: dict[int, tuple[str, str, str | None]] = {
    **{i: (f"trunc_sat_{i}", "", "nontrapping-fptoint") for i in range(8)},
    8: ("memory.init", "ub", "bulk-memory"),
    9: ("data.drop", "u", "bulk-memory"),
    10: ("memory.copy", "bb", "bulk-memory"),
    11: ("memory.fill", "b", "bulk-memory"),
    12: ("table.init", "uu", "bulk-memory"),
    13: ("elem.drop", "u", "bulk-memory"),
    14: ("table.copy", "uu", "bulk-memory"),
    15: ("table.grow", "u", "reference-types"),
    16: ("table.size", "u", "reference-types"),
    17: ("table.fill", "u", "reference-types"),
}

_PREFIX_FEATURES = {0xFB: "gc", 0xFD: "simd128", 0xFE: "threads"}


def _count(mod: Module, name: str) -> None:
    mod.opcode_counts[name] = mod.opcode_counts.get(name, 0) + 1


def _blocktype(r: Reader, mod: Module) -> None:
    b = r.data[r.pos] if r.pos < r.end else 0
    if b == 0x40 or b in VALTYPES:
        r.byte()
        if b == 0x7B:
            mod.features.add("simd128")
        return
    idx = r.sleb(33)
    if idx < 0 or idx >= len(mod.types):
        raise WasmError(f"bad block type index {idx}")
    t = mod.types[idx]
    if len(t.results) > 1 or t.params:
        mod.features.add("multivalue")


def _memarg(r: Reader, mod: Module) -> None:
    align = r.u32()
    if align & 0x40:
        mod.features.add("multi-memory")
        r.u32()
    r.u32()


def decode_body(r: Reader, mod: Module) -> None:
    """Decode one function body (locals + instructions) up to r.end."""
    for _ in range(r.u32()):
        r.u32()
        vt = r.valtype()
        if vt == "v128":
            mod.features.add("simd128")
        elif vt in ("funcref", "externref"):
            mod.features.add("reference-types")
    while not r.eof():
        start = r.pos
        op = r.byte()
        if op in _PREFIX_FEATURES:
            mod.features.add(_PREFIX_FEATURES[op])
            # Instructions of this prefix are not decoded; skip the body.
            r.pos = r.end
            return
        if op == 0xFC:
            sub = r.u32()
            if sub not in _FC_OPS:
                raise WasmError(f"unknown instruction 0xfc {sub} at offset {start:#x}")
            name, imms, feature = _FC_OPS[sub]
            for imm in imms:
                (r.u32 if imm == "u" else r.byte)()
            if feature:
                mod.features.add(feature)
            _count(mod, name)
            continue
        if op not in _OPS:
            raise WasmError(f"unknown instruction {op:#04x} at offset {start:#x}")
        name, kind, feature = _OPS[op]
        if feature:
            mod.features.add(feature)
        if kind == _BLOCK:
            _blocktype(r, mod)
        elif kind == _U32:
            r.u32()
        elif kind == _BR_TABLE:
            for _ in range(r.u32()):
                r.u32()
            r.u32()
        elif kind == _CALL_IND:
            r.u32()
            before = r.pos
            table = r.u32()
            if table != 0 or r.pos - before != 1:
                mod.features.add("reference-types")
        elif kind == _MEMARG:
            _memarg(r, mod)
        elif kind == _MEMIDX:
            if r.u32() != 0:
                mod.features.add("multi-memory")
        elif kind == _S32:
            r.sleb(32)
        elif kind == _S64:
            r.sleb(64)
        elif kind == _F32:
            r.bytes(4)
        elif kind == _F64:
            r.bytes(8)
        elif kind == _SELECT_T:
            for _ in range(r.u32()):
                r.valtype()
        elif kind == _REFTYPE:
            r.byte()
        if name in ("memory.grow", "memory.size", "call_indirect"):
            _count(mod, name)


def _const_expr(r: Reader) -> tuple[int, int | float | None]:
    op = r.byte()
    value: int | float | None = None
    if op == 0x41:
        value = r.sleb(32)
    elif op == 0x42:
        value = r.sleb(64)
    elif op == 0x43:
        value = struct.unpack("<f", r.bytes(4))[0]
    elif op == 0x44:
        value = struct.unpack("<d", r.bytes(8))[0]
    elif op == 0x23:
        value = r.u32()
    elif op == 0xD0:
        r.byte()
    elif op == 0xD2:
        value = r.u32()
    else:
        raise WasmError(f"unsupported constant expression opcode {op:#x}")
    # extended-const (i32.add etc.) is not produced by uc-cc
    end = r.byte()
    if end != 0x0B:
        raise WasmError("constant expression is not a single instruction")
    return op, value


# ---------------------------------------------------------------------------
# Module parser
# ---------------------------------------------------------------------------
_SECTION_NAMES = {
    0: "custom",
    1: "type",
    2: "import",
    3: "function",
    4: "table",
    5: "memory",
    6: "global",
    7: "export",
    8: "start",
    9: "element",
    10: "code",
    11: "data",
    12: "datacount",
    13: "tag",
}


def parse(data: bytes) -> Module:
    if data[:4] == AOT_MAGIC:
        raise WasmError("this is a WAMR AOT file, not a WebAssembly module")
    if data[:4] != WASM_MAGIC:
        raise WasmError("not a WebAssembly module (bad magic)")
    if struct.unpack("<I", data[4:8])[0] != WASM_VERSION:
        raise WasmError("unsupported WebAssembly binary version")
    mod = Module(size=len(data))
    r = Reader(data, 8)
    while not r.eof():
        sid = r.byte()
        size = r.u32()
        body = Reader(data, r.pos, r.pos + size)
        if body.end > len(data):
            raise WasmError(f"section {sid} exceeds the file")
        r.pos = body.end
        sname = _SECTION_NAMES.get(sid, f"unknown-{sid}")
        mod.section_sizes[sname] = mod.section_sizes.get(sname, 0) + size
        if sid == 0:
            mod.custom_sections.append(body.name())
        elif sid == 1:
            for _ in range(body.u32()):
                form = body.byte()
                if form != 0x60:
                    mod.features.add("gc")
                    raise WasmError(f"unsupported type form {form:#x} (GC types)")
                params = tuple(body.valtype() for _ in range(body.u32()))
                results = tuple(body.valtype() for _ in range(body.u32()))
                if len(results) > 1:
                    mod.features.add("multivalue")
                mod.types.append(FuncType(params, results))
        elif sid == 2:
            for _ in range(body.u32()):
                module, name, kind = body.name(), body.name(), body.byte()
                imp = Import(module, name, kind)
                if kind == KIND_FUNC:
                    imp.type_index = body.u32()
                elif kind == KIND_TABLE:
                    body.valtype()
                    imp.limits = body.limits()
                elif kind == KIND_MEMORY:
                    imp.limits = body.limits()
                elif kind == KIND_GLOBAL:
                    body.valtype()
                    body.byte()
                elif kind == KIND_TAG:
                    body.byte()
                    body.u32()
                    mod.features.add("exception-handling")
                else:
                    raise WasmError(f"unknown import kind {kind}")
                mod.imports.append(imp)
        elif sid == 3:
            mod.func_types = [body.u32() for _ in range(body.u32())]
        elif sid == 4:
            for _ in range(body.u32()):
                if body.valtype() != "funcref":
                    mod.features.add("reference-types")
                mod.tables.append(body.limits())
            if len(mod.tables) + len(mod.imported(KIND_TABLE)) > 1:
                mod.features.add("reference-types")
        elif sid == 5:
            mod.memories = [body.limits() for _ in range(body.u32())]
        elif sid == 6:
            for _ in range(body.u32()):
                vt = body.valtype()
                mut = body.byte() == 1
                op, value = _const_expr(body)
                mod.globals.append(Global(vt, mut, op, value))
        elif sid == 7:
            for _ in range(body.u32()):
                mod.exports.append(Export(body.name(), body.byte(), body.u32()))
        elif sid == 8:
            mod.start = body.u32()
        elif sid == 10:
            count = body.u32()
            mod.code_bytes = size
            for i in range(count):
                fsize = body.u32()
                fr = Reader(data, body.pos, body.pos + fsize)
                body.pos = fr.end
                try:
                    decode_body(fr, mod)
                except WasmError as exc:
                    mod.decode_errors.append(f"function {i}: {exc}")
        elif sid == 11:
            for _ in range(body.u32()):
                flags = body.u32()
                if flags == 1:
                    mod.features.add("bulk-memory")  # passive segment
                else:
                    if flags == 2:
                        body.u32()
                    _const_expr(body)
                n = body.u32()
                body.bytes(n)
                mod.data_bytes += n
                mod.data_segments += 1
        elif sid == 12:
            mod.features.add("bulk-memory")
        elif sid == 13:
            mod.features.add("exception-handling")
    if len(mod.memories) + len(mod.imported(KIND_MEMORY)) > 1:
        mod.features.add("multi-memory")
    for mem in mod.memories + [i.limits for i in mod.imported(KIND_MEMORY) if i.limits]:
        if mem.memory64:
            mod.features.add("memory64")
        if mem.shared:
            mod.features.add("threads")
    return mod


def parse_file(path: str) -> Module:
    with open(path, "rb") as f:
        return parse(f.read())


# ---------------------------------------------------------------------------
# WAMR linear memory model
# ---------------------------------------------------------------------------
def _align(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def wamr_memory_layout(mod: Module) -> dict[str, int | bool | None]:
    """Reproduce what WAMR 2.4.5 (wasm_loader.c, WASM_ENABLE_SHRUNK_MEMORY=1)
    does with the module's memory: the linear memory is truncated at
    __heap_base when the module exports __heap_base/__data_end, has an aux
    stack pointer global and no memory.grow/memory.size; the app heap is
    then appended behind it (wasm_runtime.c memory_instantiate)."""
    heap_base = mod.exported_const_global("__heap_base")
    data_end = mod.exported_const_global("__data_end")
    mem = mod.memories[0] if mod.memories else None
    declared = mem.minimum * 65536 if mem else 0
    stack_top = None
    if heap_base is not None:
        for g in mod.globals:
            if (g.mutable and g.valtype == "i32" and g.init_op == 0x41
                    and (int(g.init_value) & 0xFFFFFFFF) <= heap_base):
                stack_top = int(g.init_value) & 0xFFFFFFFF
                break
    shrinkable = (
        mem is not None
        and not mod.grows_memory
        and heap_base is not None
        and data_end is not None
        and _align(data_end, 16) <= heap_base
        and stack_top is not None
        and _align(heap_base, 8) <= declared
    )
    return {
        "declared_bytes": declared,
        "max_bytes": (mem.maximum * 65536) if (mem and mem.maximum is not None) else None,
        "heap_base": heap_base,
        "data_end": data_end,
        "stack_top": stack_top,
        "shrinkable": shrinkable,
        "base_bytes": _align(heap_base, 8) if shrinkable else declared,
    }


def wamr_linear_memory(mod: Module, heap_bytes: int) -> int | None:
    """Linear memory WAMR allocates for an instance with an app heap of
    heap_bytes, or None when the module may grow its memory (then WAMR
    keeps the page structure and the result depends on memory.grow)."""
    if mod.grows_memory or not mod.memories:
        return None
    lay = wamr_memory_layout(mod)
    return int(lay["base_bytes"]) + _align(heap_bytes, 8)
