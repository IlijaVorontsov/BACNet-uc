# SPDX-License-Identifier: Apache-2.0
"""Read the symbols a WAMR 2.4.5 AOT file needs from the runtime and check
them against the target's symbol map (core/iwasm/aot/arch/aot_reloc_*.c
and aot_reloc.h). A symbol the map lacks makes wasm_runtime_load() fail on
the target with "resolve symbol <name> failed".

Format (aot_loader.c): "\\0aot", u32 version, then sections of u32 type,
u32 size, body. Integers are read at their natural alignment relative to
the file start. RELOCATION (5): u32 symbol count, u32 offsets[count], u32
string bytes, strings (u16 length + bytes). CUSTOM (100) with sub-type 1
(native symbols, indirect mode): u32 count, strings (u16 length + bytes,
NUL-terminated).
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

AOT_MAGIC = b"\0aot"
SECTION_RELOCATION = 5
SECTION_CUSTOM = 100
CUSTOM_NATIVE_SYMBOL = 1

# target (wamrc --target prefix) -> aot_reloc_<arch>.c
ARCH_FILES = {
    "thumb": "aot_reloc_thumb.c",
    "arm": "aot_reloc_arm.c",
    "x86_64": "aot_reloc_x86_64.c",
    "i386": "aot_reloc_x86_32.c",
    "riscv": "aot_reloc_riscv.c",
    "xtensa": "aot_reloc_xtensa.c",
    "aarch64": "aot_reloc_aarch64.c",
}


class AotError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes, pos: int, end: int):
        self.data, self.pos, self.end = data, pos, end

    def u(self, size: int) -> int:
        self.pos = (self.pos + size - 1) // size * size
        if self.pos + size > self.end:
            raise AotError("truncated AOT file")
        v = int.from_bytes(self.data[self.pos:self.pos + size], "little")
        self.pos += size
        return v

    def string(self) -> str:
        n = self.u(2)
        if self.pos + n > self.end:
            raise AotError("truncated AOT string")
        s = self.data[self.pos:self.pos + n]
        self.pos += n
        return s.rstrip(b"\0").decode("utf-8", "replace")


def sections(data: bytes) -> list[tuple[int, int, int]]:
    """(type, body start, body end) of every section."""
    if data[:4] != AOT_MAGIC:
        raise AotError("not a WAMR AOT file")
    r = _Reader(data, 8, len(data))
    out = []
    while r.pos + 8 <= len(data):
        stype = r.u(4)
        size = r.u(4)
        start = r.pos
        if start + size > len(data):
            raise AotError("AOT section exceeds the file")
        out.append((stype, start, start + size))
        r.pos = start + size
    return out


def needed_symbols(data: bytes) -> set[str]:
    """Runtime symbols referenced by relocations or the native symbol list."""
    names: set[str] = set()
    for stype, start, end in sections(data):
        r = _Reader(data, start, end)
        if stype == SECTION_RELOCATION:
            count = r.u(4)
            offsets = [r.u(4) for _ in range(count)]
            total = r.u(4)
            base = r.pos
            for off in offsets:
                if off + 2 > total:
                    raise AotError("bad AOT symbol offset")
                n = struct.unpack_from("<H", data, base + off)[0]
                raw = data[base + off + 2:base + off + 2 + n]
                names.add(raw.rstrip(b"\0").decode("utf-8", "replace"))
        elif stype == SECTION_CUSTOM:
            if r.u(4) != CUSTOM_NATIVE_SYMBOL:
                continue
            for _ in range(r.u(4)):
                names.add(r.string())
    return {n for n in names if n and not _internal(n)}


def _internal(name: str) -> bool:
    return (name.startswith(".") or name.startswith("aot_func#")
            or name.startswith("aot_func_internal#") or name.startswith("__ignore")
            or name[:4] in ("f32#", "f64#", "i32#", "i64#"))


def target_symbols(wamr_root: Path, target: str) -> set[str] | None:
    """Symbols of the target's map (a superset: conditional groups of
    aot_reloc.h are all included). None when the WAMR sources are missing."""
    arch = next((a for a in ARCH_FILES if target.startswith(a)), None)
    aot_dir = wamr_root / "core" / "iwasm" / "aot"
    if arch is None:
        return None
    files = [aot_dir / "arch" / ARCH_FILES[arch], aot_dir / "aot_reloc.h"]
    if not all(f.is_file() for f in files):
        return None
    names: set[str] = set()
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        names.update(re.findall(r"REG_SYM\(\s*(\w+)\s*\)", text))
        names.update(re.findall(r'\{\s*"(\w+)"\s*,\s*\(void\s*\*\)', text))
    return names


def check(path: str, wamr_root: Path, target: str) -> tuple[set[str], set[str] | None]:
    """(needed symbols, missing ones or None when the map is unavailable)."""
    data = Path(path).read_bytes()
    needed = needed_symbols(data)
    known = target_symbols(wamr_root, target)
    return needed, (None if known is None else needed - known)
