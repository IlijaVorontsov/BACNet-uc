# SPDX-License-Identifier: Apache-2.0
"""BACnet-uc WebAssembly ABI tables shared by uc-cc, uc-aot and uc-wasm-info.

Sources of truth:
  - wasm/sdk/include/bacnet_uc.h (API 1.0): BACNET_UC_IMPORTS, APP_EXPORTS.
    `make -C wasm test` cross-checks the import table against a module
    compiled from bacnet_uc.h (sdk/tests/abi_probe.c).
  - WAMR 2.4.5 core/iwasm/libraries/libc-builtin/libc_builtin_wrapper.c:
    LIBC_BUILTIN (the subset declared in wasm/sdk/include/uc_libc.h).
  - modules/wasm-micro-runtime (firmware WAMR configuration): FEATURES.
"""

from __future__ import annotations

I32 = "i32"
I64 = "i64"
F32 = "f32"
F64 = "f64"

Signature = tuple[tuple[str, ...], tuple[str, ...]]

API_VERSION_MAJOR = 1
API_VERSION_MINOR = 0

IMPORT_MODULE = "bacnet_uc"
LIBC_MODULE = "env"

# name -> (params, results), in bacnet_uc.h order. Pointers and sizes are i32.
BACNET_UC_IMPORTS: dict[str, Signature] = {
    "uc_log": ((I32, I32, I32), ()),
    "uc_uptime_ms": ((), (I64,)),
    "uc_set_tick_period": ((I32,), (I32,)),
    "uc_param_get": ((I32, I32, I32, I32), (I32,)),
    "uc_param_get_number": ((I32, I32, I32), (I32,)),
    "uc_obj_create": ((I32, I32, I32, I32), (I32,)),
    "uc_obj_delete": ((I32, I32), (I32,)),
    "uc_prop_read": ((I32, I32, I32, I32, I32), (I32,)),
    "uc_prop_write": ((I32, I32, I32, I32, F64, I32), (I32,)),
    "uc_prop_write_null": ((I32, I32, I32, I32), (I32,)),
    "uc_prop_write_string": ((I32, I32, I32, I32, I32), (I32,)),
    "uc_remote_read": ((I32, I32, I32, I32, I32, I32, I32), (I32,)),
    "uc_remote_write": ((I32, I32, I32, I32, I32, F64, I32, I32), (I32,)),
    "uc_remote_write_null": ((I32, I32, I32, I32, I32, I32), (I32,)),
    "uc_cov_subscribe": ((I32, I32, I32, I32), (I32,)),
    "uc_cov_unsubscribe": ((I32,), (I32,)),
    "uc_io_find": ((I32, I32), (I32,)),
    "uc_io_read": ((I32, I32), (I32,)),
    "uc_io_write": ((I32, F64), (I32,)),
    "uc_kv_get": ((I32, I32, I32, I32), (I32,)),
    "uc_kv_set": ((I32, I32, I32, I32), (I32,)),
}

# Permissions (apps.json "perms") an import may need. One entry of the
# tuple is required; which one depends on the arguments: uc_remote_* and
# uc_cov_subscribe need bacnet.local for the local device and
# bacnet.remote for other devices. An empty tuple: always allowed.
IMPORT_PERMS: dict[str, tuple[str, ...]] = {
    "uc_log": (),
    "uc_uptime_ms": (),
    "uc_set_tick_period": (),
    "uc_param_get": (),
    "uc_param_get_number": (),
    "uc_obj_create": ("bacnet.local",),
    "uc_obj_delete": ("bacnet.local",),
    "uc_prop_read": ("bacnet.local",),
    "uc_prop_write": ("bacnet.local",),
    "uc_prop_write_null": ("bacnet.local",),
    "uc_prop_write_string": ("bacnet.local",),
    "uc_remote_read": ("bacnet.remote", "bacnet.local"),
    "uc_remote_write": ("bacnet.remote", "bacnet.local"),
    "uc_remote_write_null": ("bacnet.remote", "bacnet.local"),
    "uc_cov_subscribe": ("bacnet.remote", "bacnet.local"),
    "uc_cov_unsubscribe": (),
    "uc_io_find": ("io",),
    "uc_io_read": ("io",),
    "uc_io_write": ("io",),
    "uc_kv_get": ("kv",),
    "uc_kv_set": ("kv",),
}

# Functions the host looks up by name. uc_app_api_version is mandatory.
APP_EXPORTS: dict[str, Signature] = {
    "uc_app_api_version": ((), (I32,)),
    "uc_app_init": ((), (I32,)),
    "uc_app_tick": ((I64,), ()),
    "uc_app_on_cov": ((I32, I32, I32, I32, I32, F64), ()),
    "uc_app_on_write": ((I32, I32, I32, I32, F64), ()),
    "uc_app_deinit": ((), ()),
}
REQUIRED_EXPORTS = ("uc_app_api_version",)

# Globals WAMR uses to shrink the linear memory (build_wasm_app.md).
MEMORY_EXPORTS = ("__heap_base", "__data_end")

# WAMR libc-builtin natives available to applications, with the signature
# string WAMR registers (i/I/f/F: i32/i64/f32/f64; '*', '~', '$': i32
# pointer, length, string). sprintf/vsprintf (unbounded), abort/exit
# (signatures differ from ISO C), clock/clock_gettime and the emscripten,
# C++ and spectest helpers are deliberately left out.
LIBC_BUILTIN: dict[str, str] = {
    "printf": "($*)i",
    "vprintf": "($*)i",
    "snprintf": "(*~$*)i",
    "vsnprintf": "(*~$*)i",
    "puts": "($)i",
    "putchar": "(i)i",
    "memcpy": "(**~)i",
    "memmove": "(**~)i",
    "memset": "(*ii)i",
    "memcmp": "(**~)i",
    "memchr": "(*ii)i",
    "strlen": "($)i",
    "strcmp": "($$)i",
    "strncmp": "(**~)i",
    "strncasecmp": "($$i)i",
    "strcpy": "(*$)i",
    "strncpy": "(**~)i",
    "strchr": "($i)i",
    "strstr": "($$)i",
    "strspn": "($$)i",
    "strcspn": "($$)i",
    "strdup": "($)i",
    "malloc": "(i)i",
    "calloc": "(ii)i",
    "realloc": "(ii)i",
    "free": "(*)",
    "atoi": "($)i",
    "strtol": "($*i)i",
    "strtoul": "($*i)i",
    "isalnum": "(i)i",
    "isalpha": "(i)i",
    "isdigit": "(i)i",
    "isgraph": "(i)i",
    "isprint": "(i)i",
    "isspace": "(i)i",
    "isupper": "(i)i",
    "isxdigit": "(i)i",
    "tolower": "(i)i",
    "toupper": "(i)i",
}

_WAMR_TYPES = {"i": I32, "I": I64, "f": F32, "F": F64, "*": I32, "~": I32, "$": I32}


def wamr_signature(sig: str) -> Signature:
    """Convert a WAMR native signature string such as "(*~$*)i"."""
    if not sig.startswith("(") or ")" not in sig:
        raise ValueError(f"bad WAMR signature {sig!r}")
    inner, result = sig[1:].split(")", 1)
    params = tuple(_WAMR_TYPES[c] for c in inner)
    results = tuple(_WAMR_TYPES[c] for c in result)
    return params, results


def libc_signatures() -> dict[str, Signature]:
    return {name: wamr_signature(sig) for name, sig in LIBC_BUILTIN.items()}


# ---------------------------------------------------------------------------
# WebAssembly features
# ---------------------------------------------------------------------------
# The firmware builds WAMR 2.4.5 with the fast interpreter, bulk memory and
# reference types, without SIMD, threads/atomics, exceptions, tail calls,
# GC, memory64 or multi-memory (modules/wasm-micro-runtime/CMakeLists.txt).
#
# uc-cc starts from -mcpu=mvp and enables what the runtime always supports
# (sign-ext, nontrapping-fptoint) plus bulk-memory (memcpy/memset become
# memory.copy/memory.fill instead of imports). mutable-globals (part of
# clang's "generic" CPU) is left off: modules neither import nor export
# mutable globals, and WAMR lists import/export of mutable globals as
# unsupported.
# reference-types is left off: C does not need it and it changes the
# call_indirect encoding, so modules also load on a firmware built with
# CONFIG_WAMR_REF_TYPES=n.
CLANG_FEATURE_FLAGS = (
    "-mcpu=mvp",
    "-msign-ext",
    "-mnontrapping-fptoint",
    "-mbulk-memory",
)

# feature name -> (severity, explanation) for the module checker.
#   "ok":      always supported by the BACnet-uc runtime
#   "config":  needs a firmware option that is on by default
#   "error":   not supported by the BACnet-uc runtime
FEATURES: dict[str, tuple[str, str]] = {
    "sign-ext": ("ok", "sign extension operators"),
    "nontrapping-fptoint": ("ok", "saturating float-to-int conversions"),
    "multivalue": ("ok", "multi-value block types"),
    "bulk-memory": ("config", "needs CONFIG_WAMR_BULK_MEMORY=y (default)"),
    "reference-types": ("config", "needs CONFIG_WAMR_REF_TYPES=y (default)"),
    "simd128": ("error", "SIMD is not built into the BACnet-uc runtime"),
    "threads": ("error", "atomics/shared memory are not supported"),
    "exception-handling": ("error", "exception handling is not supported"),
    "tail-call": ("error", "tail calls are not supported"),
    "gc": ("error", "GC/typed function references are not supported"),
    "multi-memory": ("error", "only one linear memory is supported"),
    "memory64": ("error", "memory64 is not supported"),
}

WASM_PAGE = 65536

# os_getpagesize() of WAMR's Zephyr platform layer without MMU. WAMR rounds
# the bounds-checked linear memory size up to this (see uc-cc).
WAMR_OS_PAGE = 4096
