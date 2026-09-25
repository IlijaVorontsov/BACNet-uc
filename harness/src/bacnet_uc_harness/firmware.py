# SPDX-License-Identifier: Apache-2.0
"""Firmware build and flash wrappers around ``west``.

``west`` runs in the west workspace top directory (``paths.workspace_top()``)
with the application ``firmware/``. The environment is inherited;
``ZEPHYR_SDK_INSTALL_DIR`` is passed on when set, and the directory of the
running Python interpreter is put first in ``PATH`` so that a ``west``
installed in the same virtual environment is found.

Every run writes its complete output to a log file (``<build_dir>.log`` next
to the build directory by default) and returns the last lines in the result.
"""

from __future__ import annotations

import os
import shlex
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bacnet_uc_harness import paths
from bacnet_uc_harness.errors import HarnessError

BOARDS = ("nucleo_f767zi", "frdm_mcxn947/mcxn947/cpu0", "native_sim/native/64")
TAIL_LINES = 40


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    command: list[str]
    cwd: str
    duration_s: float
    log_path: str | None
    output_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "returncode": self.returncode, "command": shlex.join(self.command),
                "cwd": self.cwd, "duration_s": round(self.duration_s, 1),
                "log_path": self.log_path, "output_tail": self.output_tail}


@dataclass
class FirmwareBuildResult(CommandResult):
    board: str = ""
    build_dir: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out.update({"board": self.board, "build_dir": self.build_dir,
                    "artifacts": self.artifacts, "info": self.info})
        return out


def west_command() -> list[str]:
    """``west`` from PATH, next to the interpreter, or ``python -m west``."""
    exe = shutil.which("west")
    if exe:
        return [exe]
    local = Path(sys.executable).parent / "west"
    if local.is_file():
        return [str(local)]
    return [sys.executable, "-m", "west"]


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    bindir = str(Path(sys.executable).parent)
    if bindir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    if extra:
        env.update(extra)
    return env


def default_build_dir(board: str) -> Path:
    """``<home>/.bacnet-uc/build/fw-<board>``."""
    return paths.state_dir() / "build" / f"fw-{paths.board_key(board)}"


def _run(cmd: list[str], cwd: Path, log_path: Path | None, timeout: float | None,
         env: dict[str, str]) -> CommandResult:
    start = time.monotonic()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
        output = proc.stdout or ""
        rc = proc.returncode
    except FileNotFoundError as exc:
        output, rc = f"{cmd[0]}: {exc}\n", 127
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        output = (out.decode(errors="replace") if isinstance(out, bytes) else out)
        output += f"\ntimeout after {timeout} s\n"
        rc = -1
    duration = time.monotonic() - start
    if log_path is not None:
        log_path.write_text(f"$ {shlex.join(cmd)}\n(cwd {cwd})\n{output}", encoding="utf-8")
    tail = "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])
    return CommandResult(ok=rc == 0, returncode=rc, command=cmd, cwd=str(cwd),
                         duration_s=duration, log_path=str(log_path) if log_path else None,
                         output_tail=tail)


def west_build(
    board: str,
    build_dir: Path | str | None = None,
    pristine: bool = False,
    extra_args: Sequence[str] = (),
    sysbuild: bool = False,
    snippets: Sequence[str] = (),
    *,
    app_dir: Path | str | None = None,
    cmake_args: Sequence[str] = (),
    extra_conf: Sequence[str] = (),
    timeout: float | None = 3600,
    log_path: Path | str | None = None,
) -> FirmwareBuildResult:
    """``west build -b <board> firmware -d <build_dir>``.

    Args:
        extra_args: additional ``west build`` arguments (before ``--``).
        sysbuild: build with MCUboot (``--sysbuild``).
        snippets: Zephyr snippets (``-S``), e.g. ``uc-ramfs``.
        cmake_args: arguments after ``--`` (``-DCONFIG_...=y``).
        extra_conf: ``EXTRA_CONF_FILE`` overlays (e.g. ``overlay-syslog.conf``,
            relative to ``firmware/``).
    """
    bdir = Path(build_dir).expanduser().resolve() if build_dir else default_build_dir(board)
    app = Path(app_dir).expanduser().resolve() if app_dir else paths.firmware_dir()
    cmd = [*west_command(), "build", "-b", board, str(app), "-d", str(bdir)]
    if pristine:
        cmd += ["-p", "always"]
    if sysbuild:
        cmd.append("--sysbuild")
    for snip in snippets:
        cmd += ["-S", snip]
    cmd += list(extra_args)
    tail_args = list(cmake_args)
    if extra_conf:
        tail_args.append("-DEXTRA_CONF_FILE=" + ";".join(extra_conf))
    if tail_args:
        cmd += ["--", *tail_args]
    log = Path(log_path) if log_path else bdir.with_name(bdir.name + ".log")
    res = _run(cmd, paths.workspace_top(), log, timeout, build_env())
    result = FirmwareBuildResult(**vars(res), board=board, build_dir=str(bdir))
    if res.ok:
        try:
            info = firmware_info(bdir)
            result.info = info
            result.artifacts = dict(info.get("artifacts", {}))
        except HarnessError as exc:
            result.info = {"error": str(exc)}
    return result


def west_flash(build_dir: Path | str, runner: str | None = None,
               extra_args: Sequence[str] = (), *, timeout: float | None = 600,
               log_path: Path | str | None = None) -> CommandResult:
    """``west flash -d <build_dir> [-r <runner>]`` (e.g. ``openocd``, ``jlink``,
    ``linkserver``, ``pyocd``)."""
    bdir = Path(build_dir).expanduser().resolve()
    if not bdir.is_dir():
        raise HarnessError(f"build directory {bdir} does not exist")
    cmd = [*west_command(), "flash", "-d", str(bdir)]
    if runner:
        cmd += ["-r", runner]
    cmd += list(extra_args)
    log = Path(log_path) if log_path else bdir.with_name(bdir.name + ".flash.log")
    return _run(cmd, paths.workspace_top(), log, timeout, build_env())


# --- build inspection ------------------------------------------------------------------------


def _cmake_cache(build_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = (build_dir / "CMakeCache.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line or line[0] in "#/" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.split(":", 1)[0]] = value
    return out


def elf_sizes(elf: Path) -> dict[str, int]:
    """``text``/``data``/``bss`` of an ELF (allocated sections, like ``size``)
    plus ``flash`` = text + data and ``ram`` = data + bss."""
    data = elf.read_bytes()
    if data[:4] != b"\x7fELF":
        raise HarnessError(f"{elf} is not an ELF file")
    is64 = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    if is64:
        shoff, = struct.unpack_from(end + "Q", data, 0x28)
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x3A)
    else:
        shoff, = struct.unpack_from(end + "I", data, 0x20)
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x2E)
    text = dat = bss = 0
    for i in range(shnum):
        off = shoff + i * shentsize
        if is64:
            _, sh_type, sh_flags, _, _, sh_size = struct.unpack_from(end + "IIQQQQ", data, off)
        else:
            _, sh_type, sh_flags, _, _, sh_size = struct.unpack_from(end + "IIIIII", data, off)
        if not sh_flags & 0x2:  # SHF_ALLOC
            continue
        if sh_type == 8:  # SHT_NOBITS
            bss += sh_size
        elif sh_flags & 0x1:  # SHF_WRITE
            dat += sh_size
        else:
            text += sh_size
    return {"text": text, "data": dat, "bss": bss, "flash": text + dat, "ram": dat + bss}


def _image_dir(build_dir: Path) -> Path:
    if (build_dir / "zephyr" / "zephyr.elf").is_file():
        return build_dir / "zephyr"
    # sysbuild: <build>/<app>/zephyr
    for cand in sorted(build_dir.glob("*/zephyr/zephyr.elf")):
        if cand.parts[-3] != "mcuboot":
            return cand.parent
    raise HarnessError(f"no zephyr.elf below {build_dir} (not built yet?)")


def firmware_info(build_dir: Path | str) -> dict[str, Any]:
    """Board, image files and sizes of a (sysbuild or plain) build directory."""
    bdir = Path(build_dir).expanduser().resolve()
    if not bdir.is_dir():
        raise HarnessError(f"build directory {bdir} does not exist")
    img = _image_dir(bdir)
    cache = _cmake_cache(img.parent)
    board = cache.get("CACHED_BOARD") or cache.get("BOARD") or ""
    artifacts: dict[str, str] = {}
    for name in ("zephyr.elf", "zephyr.exe", "zephyr.bin", "zephyr.hex",
                 "zephyr.signed.bin", "zephyr.signed.hex"):
        p = img / name
        if p.is_file():
            artifacts[name] = str(p)
    info: dict[str, Any] = {"board": board, "build_dir": str(bdir), "image_dir": str(img),
                            "artifacts": artifacts,
                            "sysbuild": (bdir / "domains.yaml").is_file()}
    elf = img / "zephyr.elf"
    try:
        info["sizes"] = elf_sizes(elf)
    except (OSError, struct.error, HarnessError) as exc:
        info["sizes_error"] = str(exc)
    for name in ("zephyr.bin", "zephyr.signed.bin"):
        if name in artifacts:
            info[name.replace(".", "_") + "_size"] = Path(artifacts[name]).stat().st_size
    ver = img / "include" / "generated" / "zephyr" / "app_version.h"
    if ver.is_file():
        for line in ver.read_text(encoding="utf-8").splitlines():
            if line.startswith("#define APP_VERSION_EXTENDED_STRING"):
                info["app_version"] = line.split('"')[1]
    conf = img / ".config"
    if conf.is_file():
        for line in conf.read_text(encoding="utf-8").splitlines():
            if line.startswith("CONFIG_UC_FW_VERSION="):
                info["fw_version"] = line.split("=", 1)[1].strip('"')
    info["updatable"] = "zephyr.signed.bin" in artifacts
    return info


def update_image(build_dir: Path | str) -> Path:
    """The MCUboot-signed image of a sysbuild build (for ``img_upload``)."""
    info = firmware_info(build_dir)
    path = info["artifacts"].get("zephyr.signed.bin")
    if not path:
        raise HarnessError(f"{build_dir} has no zephyr.signed.bin: build with sysbuild=True "
                           "(MCUboot) to update over SMP")
    return Path(path)


def native_executable(build_dir: Path | str) -> Path:
    """``zephyr.exe`` of a native_sim build."""
    info = firmware_info(build_dir)
    exe = info["artifacts"].get("zephyr.exe")
    if not exe:
        raise HarnessError(f"{build_dir} is not a native_sim build (no zephyr.exe)")
    return Path(exe)


def image_hash(signed_bin: Path) -> bytes:
    """SHA-256 image hash from the MCUboot TLV area of a signed image (the value
    ``img_state`` reports and ``img_test`` expects)."""
    data = signed_bin.read_bytes()
    if len(data) < 32 or struct.unpack_from("<I", data, 0)[0] != 0x96F3B83D:
        raise HarnessError(f"{signed_bin} is not an MCUboot image")
    hdr_size, = struct.unpack_from("<H", data, 8)
    prot_size, = struct.unpack_from("<H", data, 10)
    img_size, = struct.unpack_from("<I", data, 12)
    off = hdr_size + img_size
    magic, _total = struct.unpack_from("<HH", data, off)
    if magic == 0x6908:  # protected TLV area first
        off += prot_size
        magic, _total = struct.unpack_from("<HH", data, off)
    if magic != 0x6907:
        raise HarnessError("MCUboot TLV area not found")
    end = off + _total
    off += 4
    while off + 4 <= end:
        tlv_type, tlv_len = struct.unpack_from("<HH", data, off)
        if tlv_type == 0x10:  # IMAGE_TLV_SHA256
            return data[off + 4:off + 4 + tlv_len]
        off += 4 + tlv_len
    raise HarnessError("SHA-256 TLV not found in the MCUboot image")
