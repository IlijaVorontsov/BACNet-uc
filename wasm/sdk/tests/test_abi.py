# SPDX-License-Identifier: Apache-2.0
"""Consistency tests of the SDK tooling (python3 -m unittest, stdlib only).

- sdk/tools/uc_abi.py against a module compiled from bacnet_uc.h/uc_libc.h
  (sdk/tests/abi_probe.c), the firmware's native symbol table
  (firmware/src/apps/uc_app_host_api.c) and WAMR's libc-builtin table.
- uc-cc rejects what the firmware would refuse.

Environment: UC_CLANG (clang), WAMR_ROOT (WAMR source tree, default
<west top>/modules/lib/wamr). Tests whose inputs are missing are skipped.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
REPO = SDK.parents[1]
sys.path.insert(0, str(SDK / "tools"))

import uc_abi  # noqa: E402
import uc_check  # noqa: E402
import wasmfile  # noqa: E402

UC_CC = SDK / "uc-cc"
WAMR_ROOT = Path(os.environ.get("WAMR_ROOT", REPO.parent / "modules" / "lib" / "wamr"))
FW_NATIVES = REPO / "firmware" / "src" / "apps" / "uc_app_host_api.c"
LIBC_WRAPPER = WAMR_ROOT / "core/iwasm/libraries/libc-builtin/libc_builtin_wrapper.c"


def uc_cc(out: Path, *sources: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(UC_CC), "-q", "-o", str(out), *sources, *extra],
                          capture_output=True, text=True, check=False)


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="uc-abi-")
        out = Path(cls.tmp.name) / "probe.wasm"
        res = uc_cc(out, str(SDK / "tests" / "abi_probe.c"))
        if res.returncode != 0:
            raise AssertionError(f"abi_probe.c does not build:\n{res.stderr}")
        cls.mod = wasmfile.parse_file(str(out))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def imports(self, module: str) -> dict[str, uc_abi.Signature]:
        return {i.name: (self.mod.types[i.type_index].params,
                                      self.mod.types[i.type_index].results)
                for i in self.mod.imports if i.module == module}

    def test_bacnet_uc_imports_match_header(self) -> None:
        got = self.imports(uc_abi.IMPORT_MODULE)
        self.assertEqual(got, uc_abi.BACNET_UC_IMPORTS)
        self.assertEqual(set(uc_abi.IMPORT_PERMS), set(uc_abi.BACNET_UC_IMPORTS))

    def test_libc_imports_match_uc_libc_h(self) -> None:
        got = self.imports(uc_abi.LIBC_MODULE)
        self.assertEqual(got, uc_abi.libc_signatures())

    def test_probe_passes_check(self) -> None:
        rep = uc_check.check_module(self.mod, "probe")
        self.assertEqual(rep.errors, [])
        self.assertTrue(rep.info["memory_shrinkable"])
        # shrunk to __heap_base, which uc-cc pads to a 4 KiB boundary
        self.assertLessEqual(rep.info["memory_base_bytes"], 8192)
        self.assertEqual(rep.info["memory_base_bytes"] % 4096, 0)
        self.assertEqual(rep.info["linear_memory_bytes"], rep.info["linear_memory_bounds_bytes"])


class FirmwareTableTest(unittest.TestCase):
    def test_firmware_natives_match(self) -> None:
        if not FW_NATIVES.is_file():
            self.skipTest(f"{FW_NATIVES} not found")
        text = FW_NATIVES.read_text(encoding="utf-8")
        entries = re.findall(r'\{\s*"(uc_\w+)"\s*,\s*\(void \*\)\w+\s*,\s*"([^"]+)"', text)
        self.assertTrue(entries, "no NativeSymbol entries found")
        fw = {name: uc_abi.wamr_signature(sig) for name, sig in entries}
        self.assertEqual(fw, uc_abi.BACNET_UC_IMPORTS)


class WamrLibcTest(unittest.TestCase):
    def test_libc_builtin_signatures(self) -> None:
        if not LIBC_WRAPPER.is_file():
            self.skipTest(f"{LIBC_WRAPPER} not found")
        text = LIBC_WRAPPER.read_text(encoding="utf-8")
        table = dict(re.findall(r'REG_NATIVE_FUNC\((\w+),\s*"([^"]+)"\)', text))
        table.update(re.findall(r'\{\s*"(\w+)",\s*\w+_wrapper,\s*"([^"]+)"', text))
        for name, sig in uc_abi.LIBC_BUILTIN.items():
            with self.subTest(name=name):
                self.assertIn(name, table)
                self.assertEqual(sig, table[name])


class RejectTest(unittest.TestCase):
    CASES = {
        "undefined": ("#include <bacnet_uc.h>\nUC_APP_DECLARE()\ndouble sin(double);\n"
                      "UC_EXPORT(uc_app_init) int32_t uc_app_init(void)"
                      "{ return (int)sin(uc_param_num(\"x\", 1)); }\n",
                      "undefined symbol: sin"),
        "grow": ("#include <bacnet_uc.h>\nUC_APP_DECLARE()\n"
                 "UC_EXPORT(uc_app_init) int32_t uc_app_init(void)"
                 "{ return __builtin_wasm_memory_grow(0, 1); }\n",
                 "memory.grow/memory.size"),
        "badsig": ("#include <stdint.h>\n"
                   "__attribute__((import_module(\"bacnet_uc\"), import_name(\"uc_log\")))"
                   " void f(int);\n"
                   "__attribute__((export_name(\"uc_app_api_version\"))) uint32_t v(void)"
                   "{ f(1); return 0x10000; }\n",
                   "import bacnet_uc.uc_log: signature (i32) -> ()"),
        "noversion": ("#include <bacnet_uc.h>\n"
                      "UC_EXPORT(uc_app_init) int32_t uc_app_init(void) { return 0; }\n",
                      "export uc_app_api_version missing"),
        "badexport": ("#include <bacnet_uc.h>\nUC_APP_DECLARE()\n"
                      "UC_EXPORT(uc_app_tick) void uc_app_tick(uint32_t t) { (void)t; }\n",
                      "export uc_app_tick: signature (i32) -> ()"),
    }

    def test_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="uc-rej-") as tmp:
            for name, (src, needle) in self.CASES.items():
                with self.subTest(case=name):
                    c = Path(tmp) / f"{name}.c"
                    c.write_text(src, encoding="utf-8")
                    out = Path(tmp) / f"{name}.wasm"
                    res = uc_cc(out, str(c))
                    self.assertNotEqual(res.returncode, 0)
                    self.assertIn(needle, res.stderr)
                    self.assertFalse(out.exists(), "a rejected module must not be left")

    def test_allow_grow(self) -> None:
        src, _ = self.CASES["grow"]
        with tempfile.TemporaryDirectory(prefix="uc-rej-") as tmp:
            c = Path(tmp) / "grow.c"
            c.write_text(src, encoding="utf-8")
            res = uc_cc(Path(tmp) / "grow.wasm", str(c), extra=("--allow-grow",))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("warning", res.stderr)


if __name__ == "__main__":
    unittest.main()
