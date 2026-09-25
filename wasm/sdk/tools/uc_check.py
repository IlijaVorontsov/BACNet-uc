# SPDX-License-Identifier: Apache-2.0
"""Check a WebAssembly module against the BACnet-uc application ABI.

Errors are conditions under which the firmware refuses or cannot run the
module; warnings are legal but costly or unusual choices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import uc_abi
import wasmfile
from wasmfile import KIND_FUNC, KIND_GLOBAL, KIND_MEMORY, KIND_NAMES, KIND_TABLE, Module


@dataclass
class Report:
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _sig(t: wasmfile.FuncType) -> uc_abi.Signature:
    return t.params, t.results


def _fmt(sig: uc_abi.Signature) -> str:
    return f"({', '.join(sig[0])}) -> ({', '.join(sig[1])})"


def check_module(mod: Module, path: str = "", *, allow_grow: bool = False,
                 heap_bytes: int = 8192) -> Report:
    rep = Report(path)
    libc = uc_abi.libc_signatures()
    perms: set[str] = set()
    perm_alternatives: set[str] = set()
    used_bacnet: list[str] = []
    used_libc: list[str] = []

    for msg in mod.decode_errors:
        rep.errors.append(f"code: {msg}")

    # -- imports --------------------------------------------------------------
    for imp in mod.imports:
        where = f"import {imp.module}.{imp.name}"
        if imp.kind != KIND_FUNC:
            kind = KIND_NAMES.get(imp.kind, str(imp.kind))
            hint = ""
            if imp.kind == KIND_MEMORY:
                hint = " (link without --import-memory)"
            elif imp.kind == KIND_TABLE:
                hint = " (link without --import-table)"
            elif imp.kind == KIND_GLOBAL:
                hint = " (no host globals are provided)"
            rep.errors.append(f"{where}: {kind} imports are not provided by the host{hint}")
            continue
        got = _sig(mod.types[imp.type_index])
        if imp.module == uc_abi.IMPORT_MODULE:
            want = uc_abi.BACNET_UC_IMPORTS.get(imp.name)
            if want is None:
                rep.errors.append(f"{where}: unknown host function (bacnet_uc.h API "
                                  f"{uc_abi.API_VERSION_MAJOR}.{uc_abi.API_VERSION_MINOR})")
            elif got != want:
                rep.errors.append(f"{where}: signature {_fmt(got)}, host provides {_fmt(want)}")
            else:
                used_bacnet.append(imp.name)
                alts = uc_abi.IMPORT_PERMS[imp.name]
                if len(alts) == 1:
                    perms.add(alts[0])
                elif alts:
                    perm_alternatives.update(alts)
        elif imp.module == uc_abi.LIBC_MODULE:
            want = libc.get(imp.name)
            if want is None:
                rep.errors.append(f"{where}: not provided by WAMR libc-builtin "
                                  "(see wasm/sdk/include/uc_libc.h)")
            elif got != want:
                rep.errors.append(f"{where}: signature {_fmt(got)}, libc-builtin provides "
                                  f"{_fmt(want)}")
            else:
                used_libc.append(imp.name)
        else:
            rep.errors.append(f"{where}: unknown import module (only \"bacnet_uc\" and "
                              "\"env\" are resolved)")

    # -- exports --------------------------------------------------------------
    exported_funcs: list[str] = []
    for exp in mod.exports:
        if exp.kind != KIND_FUNC:
            continue
        exported_funcs.append(exp.name)
        want = uc_abi.APP_EXPORTS.get(exp.name)
        if want is not None:
            got = _sig(mod.function_type(exp.index))
            if got != want:
                rep.errors.append(f"export {exp.name}: signature {_fmt(got)}, host expects "
                                  f"{_fmt(want)}")
    for name in uc_abi.REQUIRED_EXPORTS:
        if name not in exported_funcs:
            rep.errors.append(f"export {name} missing (use UC_APP_DECLARE())")
    callbacks = [n for n in exported_funcs if n in uc_abi.APP_EXPORTS]
    others = [n for n in exported_funcs if n not in uc_abi.APP_EXPORTS]
    if others:
        rep.warnings.append(f"exports not used by the host: {', '.join(others)}")
    if "uc_app_init" not in exported_funcs and "uc_app_tick" not in exported_funcs:
        rep.warnings.append("neither uc_app_init nor uc_app_tick is exported")
    if mod.start is not None:
        rep.warnings.append("module has a start function (runs before uc_app_api_version)")

    # -- features ------------------------------------------------------------
    for feat in sorted(mod.features):
        sev, why = uc_abi.FEATURES.get(feat, ("error", "unknown feature"))
        if sev == "error":
            rep.errors.append(f"feature {feat}: {why}")
        elif sev == "config" and feat != "bulk-memory":
            rep.warnings.append(f"feature {feat}: {why}")

    # -- memory --------------------------------------------------------------
    if not mod.memories:
        rep.errors.append("module defines no linear memory")
    lay = wasmfile.wamr_memory_layout(mod)
    if mod.grows_memory:
        msg = ("uses memory.grow/memory.size: WAMR cannot shrink the linear memory "
               f"({lay['declared_bytes']} bytes declared stay allocated)")
        (rep.warnings if allow_grow else rep.errors).append(msg)
    else:
        for name in uc_abi.MEMORY_EXPORTS:
            if mod.exported_const_global(name) is None:
                rep.warnings.append(f"{name} not exported: WAMR cannot shrink the linear "
                                    "memory (link with --export=" + name + ")")
        if lay["heap_base"] is not None and not lay["shrinkable"]:
            rep.warnings.append("linear memory is not shrinkable (no aux stack pointer global?)")
    linear = wasmfile.wamr_linear_memory(mod, heap_bytes)
    page = uc_abi.WAMR_OS_PAGE
    bounds = None if linear is None else (linear + page - 1) // page * page
    if linear is not None and bounds != linear:
        rep.warnings.append(
            f"linear memory {linear} B is not a multiple of {page} B: WAMR bounds-checks "
            f"{bounds} B and the firmware allocates {bounds} B from the pool, {bounds - linear} B "
            "more than the module uses (build with uc-cc page alignment and use "
            "heap_kb % 4 == 0)")

    # -- info -----------------------------------------------------------------
    stack_top = lay["stack_top"]
    data_end = lay["data_end"]
    rep.info = {
        "file_bytes": mod.size,
        "code_bytes": mod.code_bytes,
        "data_bytes": mod.data_bytes,
        "functions": len(mod.func_types),
        "imports_bacnet_uc": sorted(used_bacnet),
        "imports_env": sorted(used_libc),
        "exports": callbacks,
        "features": sorted(mod.features),
        "permissions": sorted(perms),
        "permissions_any_of": sorted(perm_alternatives - perms),
        "memory_declared_bytes": lay["declared_bytes"],
        "memory_max_bytes": lay["max_bytes"],
        "heap_base": lay["heap_base"],
        "data_end": data_end,
        "aux_stack_top": stack_top,
        "memory_shrinkable": bool(lay["shrinkable"]),
        "memory_base_bytes": lay["base_bytes"],
        "app_heap_bytes": heap_bytes,
        "linear_memory_bytes": linear,
        "linear_memory_bounds_bytes": bounds,
        "custom_sections": mod.custom_sections,
    }
    return rep


def check_file(path: str, **kw: Any) -> Report:
    try:
        mod = wasmfile.parse_file(path)
    except (OSError, wasmfile.WasmError) as exc:
        rep = Report(path)
        rep.errors.append(str(exc))
        return rep
    return check_module(mod, path, **kw)


def summary_line(rep: Report) -> str:
    i = rep.info
    if not i:
        return rep.path
    lin = i["linear_memory_bytes"]
    mem = (f"linear memory {i['memory_base_bytes']} B + heap {i['app_heap_bytes']} B = {lin} B"
           if lin is not None else f"linear memory {i['memory_declared_bytes']} B (not shrinkable)")
    return (f"{rep.path}: {i['file_bytes']} B (code {i['code_bytes']} B, data "
            f"{i['data_bytes']} B), {mem}")
