# SPDX-License-Identifier: Apache-2.0
"""WebAssembly build, binary parser, ABI check and AOT wrapper."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bacnet_uc_harness import wasm_build as wb
from bacnet_uc_harness.errors import HarnessError

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "wasm" / "examples"
needs_clang = pytest.mark.skipif(shutil.which("clang") is None or shutil.which("wasm-ld") is None,
                                 reason="clang with wasm32 + wasm-ld required")


# --- a tiny module assembler for parser tests -------------------------------------------------


def uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def name(s: str) -> bytes:
    return uleb(len(s)) + s.encode()


def section(sid: int, body: bytes) -> bytes:
    return bytes([sid]) + uleb(len(body)) + body


def vec(items: list[bytes]) -> bytes:
    return uleb(len(items)) + b"".join(items)


def module(imports: list[tuple[str, str]], exports: list[str], *, extra_imports: bytes = b"",
           n_extra: int = 0) -> bytes:
    types = section(1, vec([b"\x60" + vec([b"\x7f", b"\x7f"]) + vec([b"\x7f"]),
                            b"\x60" + vec([]) + vec([b"\x7f"])]))
    imps = [name(mod) + name(n) + b"\x00" + uleb(0) for mod, n in imports]
    imp = section(2, uleb(len(imps) + n_extra) + b"".join(imps) + extra_imports)
    funcs = section(3, vec([uleb(1)]))
    exps = section(7, vec([name(e) + b"\x00" + uleb(len(imports)) for e in exports]))
    code = section(10, vec([uleb(4) + b"\x00\x41\x00\x0b"]))
    custom = section(0, name("producers") + b"xyz")
    return b"\x00asm\x01\x00\x00\x00" + custom + types + imp + funcs + exps + code


def test_parse_wasm_imports_exports() -> None:
    data = module([("bacnet_uc", "uc_log"), ("env", "snprintf")], ["uc_app_api_version"])
    info = wb.parse_wasm(data)
    assert [i.qualname for i in info.imports] == ["bacnet_uc.uc_log", "env.snprintf"]
    assert info.imports[0].signature == "(i32, i32) -> (i32)"
    assert info.function_exports() == ["uc_app_api_version"]
    assert info.sections == [0, 1, 2, 3, 7, 10]
    assert info.size == len(data)


def test_parse_wasm_other_import_kinds() -> None:
    mem = name("env") + name("memory") + b"\x02" + b"\x01" + uleb(1) + uleb(2)
    glob = name("env") + name("g") + b"\x03" + b"\x7f\x00"
    table = name("env") + name("t") + b"\x01" + b"\x70" + b"\x00" + uleb(1)
    data = module([], ["uc_app_api_version"], extra_imports=mem + glob + table, n_extra=3)
    info = wb.parse_wasm(data)
    assert [(i.name, i.kind) for i in info.imports] == [("memory", "memory"), ("g", "global"),
                                                        ("t", "table")]
    chk = wb.check_module(data)
    assert not chk.ok
    assert sum("imports are not provided" in e for e in chk.errors) == 3


@pytest.mark.parametrize(("data", "fragment"), [
    (b"\x7fELF\x01\x01\x01\x00", "bad magic"),
    (b"\x00asm\x02\x00\x00\x00", "unsupported WebAssembly version"),
    (b"\x00asm\x01\x00\x00\x00\x07\x10\x01", "exceeds the module"),
    (b"\x00asm\x01\x00\x00\x00\x07\x02\x01\x05", "unexpected end"),
])
def test_parse_wasm_errors(data: bytes, fragment: str) -> None:
    with pytest.raises(wb.WasmError, match=fragment):
        wb.parse_wasm(data)


def test_check_module_abi_errors() -> None:
    data = module([("bacnet_uc", "uc_launch_missiles"), ("env", "sin"), ("wasi", "fd_write")],
                  ["main"])
    chk = wb.check_module(data)
    assert not chk.ok
    text = "\n".join(chk.errors)
    assert "bacnet_uc.uc_launch_missiles: unknown host function" in text
    assert "env.sin: not a libc-builtin function" in text
    assert "wasi.fd_write: import module must be" in text
    assert "uc_app_api_version is not exported" in text
    assert chk.warnings


def test_check_module_ok_and_perms() -> None:
    data = module([("bacnet_uc", "uc_remote_read"), ("bacnet_uc", "uc_io_find"),
                   ("bacnet_uc", "uc_cov_unsubscribe"), ("env", "memcpy")],
                  ["uc_app_api_version", "uc_app_tick"])
    chk = wb.check_module(data)
    assert chk.ok, chk.errors
    assert chk.perms == ["bacnet.remote", "io"]
    assert chk.to_dict()["size"] == len(data)


def test_header_knowledge() -> None:
    names = wb.known_imports()
    assert {"uc_log", "uc_remote_read", "uc_kv_set", "uc_io_write"} <= names
    assert "name" not in names
    funcs = {f.name: f for f in wb.host_functions()}
    assert funcs["uc_obj_create"].permission == "bacnet.local"
    assert funcs["uc_kv_get"].permission == "kv"
    assert funcs["uc_log"].permission is None
    assert funcs["uc_uptime_ms"].doc == "Milliseconds since boot."
    assert funcs["uc_log"].prototype.startswith("void uc_log(int32_t level")
    libc = wb.known_env_imports()
    assert {"snprintf", "memcpy", "strtol", "malloc"} <= libc
    assert "uc_trap" not in libc


def test_perms_for_imports() -> None:
    imps = ["bacnet_uc.uc_remote_write", "bacnet_uc.uc_prop_read", "env.printf", "uc_kv_get"]
    assert wb.perms_for_imports(imps) == ["bacnet.local", "bacnet.remote", "kv"]
    assert wb.perms_for_imports(["bacnet_uc.uc_remote_read"], remote=False) == ["bacnet.local"]
    assert wb.perms_for_imports(["bacnet_uc.uc_log"]) == []


def test_sdk_info() -> None:
    info = wb.sdk_info()
    assert info["api_version"] == "1.0"
    assert info["error_codes"]["UC_ERR_PERM"]["value"] == -3
    assert info["object_types"]["ANALOG_VALUE"] == 2
    assert info["properties"]["PRESENT_VALUE"] == 85
    assert len(info["host_functions"]) == len(wb.known_imports())
    assert "uc_app_tick" in info["app_exports"]
    assert info["aot_targets"]["nucleo_f767zi"]["cpu"] == "cortex-m7"
    assert "cflags" in info["build"]


def test_wamrc_command_targets(tmp_path: Path) -> None:
    w, o = tmp_path / "a.wasm", tmp_path / "a.aot"
    cmd = wb.wamrc_command(w, o, "nucleo_f767zi", wamrc="wamrc")
    assert cmd[:4] == ["wamrc", "--target=thumbv7em", "--cpu=cortex-m7", "--target-abi=eabihf"]
    cmd = wb.wamrc_command(w, o, "frdm_mcxn947", wamrc="wamrc")
    assert cmd[1:4] == ["--target=thumbv8m.main", "--cpu=cortex-m33", "--target-abi=eabihf"]
    cmd = wb.wamrc_command(w, o, "native_sim/native/64", wamrc="wamrc")
    assert cmd[1] == "--target=x86_64" and not any(c.startswith("--target-abi") for c in cmd)
    assert cmd[-3:] == ["-o", str(o), str(w)]
    with pytest.raises(HarnessError):
        wb.wamrc_command(w, o, "esp32")


def test_build_errors(tmp_path: Path) -> None:
    with pytest.raises(wb.WasmBuildError, match="no source"):
        wb.build_c([], tmp_path / "x.wasm")
    with pytest.raises(wb.WasmBuildError, match="not found"):
        wb.build_c([tmp_path / "missing.c"], tmp_path / "x.wasm")
    with pytest.raises(HarnessError, match="optimisation"):
        wb.build_c([EXAMPLES / "blinky" / "blinky.c"], tmp_path / "x.wasm", opt="-O9")


@needs_clang
def test_build_example_with_sdk(tmp_path: Path) -> None:
    res = wb.build_c([EXAMPLES / "uc-link" / "uc_link.c"], tmp_path / "link.wasm", opt="-Oz")
    assert res.tool == "uc-cc"
    assert res.path.is_file() and res.size == res.path.stat().st_size
    assert "uc_app_api_version" in res.exports
    assert "bacnet_uc.uc_remote_read" in res.imports
    assert res.perms == ["bacnet.local", "bacnet.remote"]
    assert len(res.sha256) == 64
    assert res.to_dict()["tool"] == "uc-cc"


@needs_clang
def test_build_inline_source_and_errors(tmp_path: Path) -> None:
    src = """
#include <bacnet_uc.h>
UC_APP_DECLARE()
UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now)
{
	(void)uc_pv_write(UC_OBJ_ANALOG_VALUE, UC_VALUE, (double)now, UC_PRIORITY_NONE);
}
"""
    res = wb.build_source_text(src, tmp_path / "hello.wasm", defines={"UC_VALUE": 3})
    assert res.imports == ["bacnet_uc.uc_prop_write"]
    assert res.perms == ["bacnet.local"]
    with pytest.raises(wb.WasmBuildError) as exc:
        wb.build_source_text("int broken(void) { return ; }", tmp_path / "bad.wasm")
    assert exc.value.output and exc.value.errors
    # an undefined function is a link error with the SDK
    with pytest.raises(wb.WasmBuildError):
        wb.build_source_text('#include <bacnet_uc.h>\nUC_APP_DECLARE()\nextern double sin(double);'
                             '\nUC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t n)'
                             '{ uc_set_tick_period((uint32_t)sin((double)n)); }',
                             tmp_path / "sin.wasm")


@needs_clang
def test_build_fallback_to_clang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "_uc_cc", lambda: None)
    res = wb.build_c([EXAMPLES / "blinky" / "blinky.c"], tmp_path / "blinky.wasm")
    assert res.tool == "clang"
    assert "-Wl,--allow-undefined" in res.command
    assert "uc_app_api_version" in res.exports
    # the header's flags allow any undefined symbol: the ABI check must catch it
    src = tmp_path / "bad.c"
    src.write_text("extern int launch(void);\n"
                   "__attribute__((export_name(\"f\"))) int f(void) { return launch(); }\n")
    with pytest.raises(wb.WasmBuildError) as exc:
        wb.build_c([src], tmp_path / "bad.wasm")
    assert any("env.launch" in e for e in exc.value.errors)
    assert not (tmp_path / "bad.wasm").exists()


@needs_clang
@pytest.mark.skipif(wb.find_wamrc() is None, reason="wamrc not installed")
def test_aot_compile(tmp_path: Path) -> None:
    res = wb.build_c([EXAMPLES / "blinky" / "blinky.c"], tmp_path / "blinky.wasm")
    aot = wb.aot_compile(res.path, "frdm_mcxn947/mcxn947/cpu0", tmp_path / "blinky.aot")
    assert aot.path.read_bytes()[:4] == wb.AOT_MAGIC
    assert aot.target == "thumbv8m.main"
    with pytest.raises(wb.WasmBuildError):
        wb.aot_compile(res.path, "esp32")
