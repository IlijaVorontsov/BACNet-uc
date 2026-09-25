"""SEC-01 release artifact guard (release; static, in the build job).

Catalogue SEC-01: no release .config sets any CONFIG_HIL* symbol. strings zephyr.elf | grep -c
'KEYLOG ' is 0. Each release .config differs from the plain app build only by its D24
allowlist (BACnet plain: app-pool workaround; BACnet MCUboot: plus overlay-mcuboot.conf, and
the OTA candidate a UC_FW_VERSION suffix; MQTT plain and mTLS: site symbols). Variant images
are reported separately and add exactly one documented symbol.

One test per ``--release-build DIR``; each is compared with the ``--plain-build DIR`` of the
same app and board (the app built from the same sources without the site configuration; a
``west build --cmake-only`` is enough, as only its .config is read). The MCUboot artifact
(It2) is not in the allowlist yet.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hilrig.release import DutImage, check_release

pytestmark = pytest.mark.release


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "release_build" in metafunc.fixturenames:
        builds = metafunc.config.getoption("--release-build")
        metafunc.parametrize(
            "release_build", builds or [None], ids=[Path(b).name for b in builds] or ["none"]
        )


def test_sec01_release_artifact_guard(
    release_build: str | None, pytestconfig: pytest.Config, rig_report: Callable[[str, dict[str, Any]], Path]
) -> None:
    if release_build is None:
        pytest.skip("SEC-01 checks release builds: pass --release-build DIR and --plain-build DIR")
    release = DutImage.from_build(Path(release_build))
    plains = [DutImage.from_build(Path(p)) for p in pytestconfig.getoption("--plain-build")]
    plain = next((p for p in plains if (p.app, p.board) == (release.app, release.board)), None)
    assert plain is not None, f"no --plain-build of {release.app} for {release.board} to compare with"
    report = check_release(release, plain)
    kind = f"{report.kind} variant {report.variant}" if report.variant else report.kind
    rig_report(f"SEC-01-{Path(release_build).name}", {"kind": kind, **report.__dict__})
    print(f"SEC-01 {release_build}: {kind}; allowlisted differences {sorted(report.allowed)}")
    assert report.hil_symbols == [], f"release image sets {report.hil_symbols}"
    assert report.keylog_strings is not None, "no zephyr.elf in the release build (not linked)"
    assert report.keylog_strings == 0, f"{report.keylog_strings} 'KEYLOG ' strings in zephyr.elf"
    assert report.unexpected == {}, f"differences outside the D24 allowlist: {report.unexpected}"
