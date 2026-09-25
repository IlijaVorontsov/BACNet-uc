"""The host shell scripts (hil/host/build.sh, hil/host/isolated.sh, hil/tools/survey.sh) with
stand-ins for west, dockerd and git history, so they run without a workspace or a daemon."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from rig_skips import needs_root, needs_tools

HIL = Path(__file__).resolve().parents[2]  # hil/
BUILD_SH, ISOLATED_SH, SURVEY_SH = (
    HIL / "host" / "build.sh",
    HIL / "host" / "isolated.sh",
    HIL / "tools" / "survey.sh",
)


def script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# ld's memory table: a used size that is a whole number of KiB is printed in KB
FAKE_WEST = r"""#!/bin/sh
case $1 in
manifest) echo "${FAKE_MANIFEST:-/nonexistent/west.yml}" ;;
build)
	echo "west $*"
	while [ $# -gt 0 ]; do [ "$1" = -d ] && dir=$2; shift; done
	mkdir -p "$dir/zephyr" && : >"$dir/zephyr/.config"
	echo 'Memory region         Used Size  Region Size  %age Used'
	echo '           FLASH:       92076 B         2 MB      4.39%'
	echo '             RAM:          96 KB       384 KB     25.00%'
	echo '            DTCM:           1 MB       128 KB      0.00%' ;;
esac
"""


def build_sh(tmp_path: Path, *args: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Run build.sh with the stand-in west; returns OUT and the finished process."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script(bin_dir / "west", FAKE_WEST)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "ZEPHYR_SDK_INSTALL_DIR": "/nonexistent"}
    env["FAKE_MANIFEST"] = str(tmp_path / "ws" / "west.yml")
    out = tmp_path / "out"
    proc = subprocess.run(
        ["bash", str(BUILD_SH), "-o", str(out), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return out, proc


def test_build_sh_reports_sizes_that_ld_prints_in_kib(tmp_path: Path) -> None:
    """Regression: sizes() read only "<n> B" rows, so a used size that is a whole number of KiB
    (ld prints "96 KB") went missing from sizes.tsv."""
    out, proc = build_sh(tmp_path, "stim")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    row = next(r.split("\t") for r in (out / "sizes.tsv").read_text().splitlines() if r.startswith("stim\t"))
    assert row[2:6] == ["ok", "92076", str(96 * 1024), f"dtcm={1 << 20}"], row


@pytest.mark.parametrize("own_pool", [False, True])
def test_build_sh_applies_the_app_pool_workaround_only_where_needed(tmp_path: Path, own_pool: bool) -> None:
    """Regression: BACnet e62a095 puts the WAMR pool into DTCM itself (112 KiB, FW-04 done), and
    build.sh still forced the 96 KiB workaround, so bac-rel was not the product's release image."""
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "west.yml").write_text("manifest: {}\n")
    fw = tmp_path / "fw"
    (fw / "zephyr").mkdir(parents=True)
    (fw / "firmware" / "boards").mkdir(parents=True)
    (fw / "west.yml").write_text("manifest: {}\n")
    (fw / "zephyr" / "module.yml").write_text("name: bacnet-uc\n")
    chosen = "\tchosen { uc,app-pool = &dtcm; };\n" if own_pool else ""
    (fw / "firmware" / "boards" / "nucleo_f767zi.overlay").write_text(f"/ {{\n{chosen}}};\n")
    out, proc = build_sh(tmp_path, "-f", str(fw), "bac-rel")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    command = next(
        line for line in (out / "bac-rel.log").read_text().splitlines() if line.startswith("west ")
    )
    assert ("f767-app-pool.overlay" in command) is not own_pool, command
    assert ("CONFIG_UC_APP_POOL_SIZE=98304" in command) is not own_pool, command


@needs_root
@needs_tools("unshare", "ip")
def test_isolated_sh_stops_its_dockerd_when_interrupted(tmp_path: Path) -> None:
    """Regression: a SIGTERM to the run (or a dockerd that never answered) ended the inner shell
    without stopping the private dockerd, which then ran on in an unreachable namespace."""
    marker = f"hil-fake-dockerd-{uuid.uuid4().hex}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script(bin_dir / "dockerd", f"#!/bin/bash\nexec -a {marker} sleep 300\n")
    script(bin_dir / "docker", "#!/bin/sh\nexit 0\n")  # 'docker info' answers at once
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", DOCKER_LOG=str(tmp_path / "dockerd.log"))
    command = ["sh", "-c", "kill -TERM $PPID; sleep 1"]  # the inner shell gets SIGTERM mid-run
    try:
        proc = subprocess.run(["bash", str(ISOLATED_SH), "-d", *command], env=env, timeout=60, check=False)
        left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False).stdout
        assert proc.returncode == 143 and not left.strip(), (proc.returncode, left)
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)
    ok = subprocess.run(["bash", str(ISOLATED_SH), "sh", "-c", "exit 3"], env=env, timeout=60, check=False)
    assert ok.returncode == 3  # without -d: the command's own status


@needs_tools("git")
def test_survey_sh_reports_a_long_drift_with_status_1(tmp_path: Path) -> None:
    """Regression: 'git diff | head -n 200' under pipefail ended the script with git's SIGPIPE
    status 141 as soon as the drift diff was longer than 200 lines, before the MQTT section."""
    repo = tmp_path / "repo"
    # The throwaway repo must not inherit the user's git config (commit signing helpers,
    # hooks, templates): those can fail where the host's services are out of reach.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "bacnet", str(repo)], env=env, check=True)
    (repo / "modules").mkdir()
    (repo / "west.yml").write_text("manifest: {}\n")
    (repo / "modules" / "glue.c").write_text("".join(f"int a{i};\n" for i in range(400)))
    subprocess.run([*git, "add", "."], env=env, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "tip"], env=env, check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "hil"], env=env, check=True)
    (repo / "modules" / "glue.c").write_text("".join(f"int b{i};\n" for i in range(400)))
    subprocess.run([*git, "commit", "-q", "-am", "stale copy"], env=env, check=True)
    argv = [
        "bash",
        str(SURVEY_SH),
        "--remote",
        "none",
        "--hil",
        "hil",
        "--bacnet",
        "bacnet",
        "--mqtt",
        "bacnet",
    ]
    proc = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 1, proc.stdout[-500:] + proc.stderr
    assert "DRIFT:" in proc.stdout and "== For information: MQTT" in proc.stdout
    assert proc.stdout.count("\n  | ") == 200  # the diff is cut at 200 lines


def test_scripts_are_shellcheck_clean() -> None:
    """The scripts above stay shellcheck-clean (the linter runs where it is installed)."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    proc = subprocess.run(
        [shellcheck, str(BUILD_SH), str(ISOLATED_SH), str(SURVEY_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
