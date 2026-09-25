"""hil/tools/check_catalog.py: DUT devicetree against the pin tables and the bench file.

The pin-table parser runs on the committed docs/hil/pin-tables.md. The devicetree side is
tested twice: against a fake EDT built from the pin tables (always runs), and against a real
edtlib EDT of a small devicetree (needs Zephyr's python-devicetree, found through
$ZEPHYR_BASE). $HIL_DUT_BUILD, when set, names a real DUT build directory to check end to end
(the build job sets it to the instrumented BACnet build).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hilrig.bench import CATALOGS, Bench

HIL = Path(__file__).resolve().parents[2]
TOOL = HIL / "tools" / "check_catalog.py"
PIN_TABLES = HIL.parent / "docs" / "hil" / "pin-tables.md"
GPIO_ACTIVE_LOW, GPIO_PULL_UP, GPIO_PULL_DOWN = 0x1, 0x10, 0x20


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_catalog", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_catalog"] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


cc = _load_tool()


@pytest.fixture(scope="module")
def table() -> Any:
    return cc.parse_pin_tables(PIN_TABLES.read_text())


# ----------------------------------------------------------------- a fake edtlib EDT


@dataclass
class Prop:
    val: Any


@dataclass
class Entry:
    """edtlib ControllerAndData: the controller node and the specifier cells."""

    controller: Node
    data: dict[str, int]


@dataclass
class Pinctrl:
    conf_nodes: list[Node]


@dataclass
class Node:
    name: str
    path: str
    labels: list[str] = field(default_factory=list)
    props: dict[str, Prop] = field(default_factory=dict)
    children: dict[str, Node] = field(default_factory=dict)
    parent: Node | None = None
    pinctrls: list[Pinctrl] = field(default_factory=list)


@dataclass
class FakeEdt:
    compat2nodes: dict[str, list[Node]]
    nodes: list[Node]


def _flags(ch: Any) -> int:
    pull = {"up": GPIO_PULL_UP, "down": GPIO_PULL_DOWN, None: 0}[ch.pull]
    return (GPIO_ACTIVE_LOW if ch.active_low else 0) | pull


def fake_edt(channels: list[Any], markers: list[Any]) -> FakeEdt:
    """The EDT an STM32 build of these channels would have (hal_stm32 pinctrl names)."""
    ctrl: dict[str, Node] = {}

    def controller(label: str, parent: str | None = None) -> Node:
        if label not in ctrl:
            node = Node(label, f"/soc/{label}", [label], pinctrls=[Pinctrl([])])
            if parent:
                node.parent = Node(parent, f"/soc/{parent}", [parent])
            ctrl[label] = node
        return ctrl[label]

    def gpio(pin: str, flags: int) -> Entry:
        return Entry(controller(f"gpio{pin[1].lower()}"), {"pin": int(pin[2:]), "flags": flags})

    catalog = Node("uc-io", "/uc-io")
    for ch in channels:
        node = Node(ch.name, f"/uc-io/{ch.name}", props={"kind": Prop(ch.kind)})
        if ch.kind in ("di", "do"):
            node.props["gpios"] = Prop([gpio(ch.pin, _flags(ch))])
        elif ch.kind == "ai":
            adc, inp = ch.hw.removeprefix("ADC").split("_IN")
            c = controller(f"adc{adc}")
            c.pinctrls[0].conf_nodes.append(Node(f"adc{adc}_in{inp}_{ch.pin.lower()}", "/pinctrl/x"))
            node.props["io-channels"] = Prop([Entry(c, {"input": int(inp)})])
        elif ch.kind == "ao":
            tim, c_ = ch.hw.removeprefix("TIM").split("_CH")
            c = controller(f"pwm{tim}", parent=f"timers{tim}")
            c.pinctrls[0].conf_nodes.append(Node(f"tim{tim}_ch{c_}_{ch.pin.lower()}", "/pinctrl/x"))
            node.props["pwms"] = Prop([Entry(c, {"channel": int(c_), "period": 1000000, "flags": 0})])
        catalog.children[ch.name] = node
    nodes = [catalog]
    if markers:
        user = Node("zephyr,user", "/zephyr,user")
        user.props["hil-marker-gpios"] = Prop([gpio(m.pin, _flags(m)) for m in markers])
        nodes.append(user)
    return FakeEdt({"uc,io-channels": [catalog]}, nodes)


# ----------------------------------------------------------------- tests


def test_pin_tables_give_the_p1_catalog_extras_and_markers(table: Any) -> None:
    assert [c.name for c in table.catalog] == list(CATALOGS["nucleo_f767zi"])
    assert [c.id for c in table.catalog] == list(range(17))
    by = {c.name: c for c in table.catalog}
    assert (by["di0"].pin, by["di0"].active_low, by["di0"].pull) == ("PC13", False, None)
    assert (by["di1"].pin, by["di1"].active_low, by["di1"].pull) == ("PF15", True, "up")
    assert (by["do2"].kind, by["do2"].pin) == ("do", "PB14")
    assert (by["ai0"].pin, by["ai0"].hw) == ("PA3", "ADC1_IN3")
    assert (by["ai5"].pin, by["ai5"].hw) == ("PF10", "ADC3_IN8")
    assert (by["ao0"].pin, by["ao0"].hw) == ("PE9", "TIM1_CH1")
    assert [(c.id, c.name, c.pin) for c in table.extras] == [
        (17, "di3", "PE10"),
        (18, "di4", "PE12"),
        (19, "do5", "PE14"),
        (20, "do6", "PE15"),
    ]
    assert all(c.pull == "down" for c in table.extras[:2])
    assert all(c.pull is None and not c.active_low for c in table.extras[2:])
    assert [m.pin for m in table.markers] == ["PG0", "PG1", "PG2", "PG3"]


def test_unreadable_pin_tables_are_a_usage_error() -> None:
    with pytest.raises(cc.CheckError, match=r"### 1\.1"):
        cc.parse_pin_tables("# pin tables\n\nno sections here\n")


def test_consistent_build_passes(table: Any) -> None:
    edt = fake_edt(table.catalog + table.extras, table.markers)
    report = cc.check(edt, "nucleo_f767zi", table, None)
    assert report.problems == []
    assert cc.dut_catalog(edt) == table.catalog + table.extras


def test_release_build_notes_missing_extras_and_markers(table: Any) -> None:
    report = cc.check(fake_edt(table.catalog, []), "nucleo_f767zi", table, None)
    assert report.problems == []
    assert any("no hil-io extras" in n for n in report.notes)
    assert any("no hil-marker-gpios" in n for n in report.notes)


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        (lambda c: dataclasses.replace(c, pin="PF14") if c.name == "di1" else c, "di1 (id 1): pin"),
        (lambda c: dataclasses.replace(c, active_low=False) if c.name == "di2" else c, "active_low"),
        (lambda c: dataclasses.replace(c, pull="down") if c.name == "di1" else c, "di1 (id 1): pull"),
        (lambda c: dataclasses.replace(c, hw="ADC1_IN4", pin="PA4") if c.name == "ai0" else c, "hw"),
        (lambda c: dataclasses.replace(c, hw="TIM1_CH4", pin="PE14") if c.name == "ao2" else c, "ao2"),
    ],
)
def test_each_field_mismatch_fails(table: Any, change: Any, expect: str) -> None:
    build = [change(c) for c in table.catalog]
    problems = cc.check(fake_edt(build, table.markers), "nucleo_f767zi", table, None).problems
    assert problems and any(expect in p for p in problems), problems


def test_inserted_channel_renumbers_and_fails(table: Any) -> None:
    """FW-01: a channel inserted in the middle shifts every id after it."""
    inserted = dataclasses.replace(table.catalog[3], name="do9", pin="PG9")
    build = [*table.catalog[:3], inserted, *table.catalog[3:]]
    build = [dataclasses.replace(c, id=i) for i, c in enumerate(build)]
    problems = cc.check(fake_edt(build, []), "nucleo_f767zi", table, None).problems
    assert any("differs from hilrig.bench.CATALOGS" in p for p in problems)
    assert any("do0 (id 3): name 'do0' expected, build has 'do9'" in p for p in problems)


def test_wrong_markers_fail(table: Any) -> None:
    markers = [dataclasses.replace(m, pin="PG4") if m.name == "m3" else m for m in table.markers]
    problems = cc.check(fake_edt(table.catalog, markers), "nucleo_f767zi", table, None).problems
    assert problems == ["pin tables 1.2 (markers): m3 (id 3): pin 'PG3' expected, build has 'PG4'"]


def test_bench_channels_must_exist_in_the_build(table: Any) -> None:
    bench = Bench.load(HIL / "host" / "bench.yml.example")
    assert cc.check(fake_edt(table.catalog, table.markers), "nucleo_f767zi", table, bench).problems == []
    assert bench.stim is not None
    wired = dataclasses.replace(bench.stim, chans=(*bench.stim.chans, "di3"))
    problems = cc.check(
        fake_edt(table.catalog, []), "nucleo_f767zi", table, dataclasses.replace(bench, stim=wired)
    ).problems
    assert problems == ["bench bench1: stim.chans ['di3'] are not in the build's catalog"]
    other = cc.check(fake_edt(table.catalog, []), "frdm_mcxn947/mcxn947/cpu0", table, bench).problems
    assert any("dut.board 'nucleo_f767zi'" in p for p in other)


def test_mqtt_build_without_catalog_checks_markers_only(table: Any) -> None:
    edt = fake_edt([], table.markers)
    edt.compat2nodes = {}
    report = cc.check(edt, "nucleo_f767zi", table, None)
    assert report.problems == [] and any("no uc,io-channels catalog" in n for n in report.notes)


def test_board_targets_map_to_the_rig_board_names() -> None:
    assert cc.canonical_board("nucleo_f767zi/stm32f767xx") == "nucleo_f767zi"  # sysbuild cache
    assert cc.canonical_board("nucleo_f767zi") == "nucleo_f767zi"
    assert cc.canonical_board("frdm_mcxn947/mcxn947/cpu0") == "frdm_mcxn947/mcxn947/cpu0"
    assert cc.canonical_board("native_sim/native/64") == "native_sim/native/64"
    assert cc.canonical_board("qemu_x86") == "qemu_x86"


def test_missing_build_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cc.main([str(tmp_path)]) == 2
    assert "no such file" in capsys.readouterr().err


DTS = """
/dts-v1/;
/ {
	#address-cells = <1>;
	#size-cells = <1>;
	pinctrl: pinctrl {
		compatible = "test,pinctrl";
		adc1_in3_pa3: adc1_in3_pa3 { pinmux = <3>; };
		tim1_ch1_pe9: tim1_ch1_pe9 { pinmux = <9>; };
	};
	gpiof: gpio@40021400 {
		compatible = "test,gpio"; reg = <0x40021400 0x400>; gpio-controller; #gpio-cells = <2>;
	};
	gpiog: gpio@40021800 {
		compatible = "test,gpio"; reg = <0x40021800 0x400>; gpio-controller; #gpio-cells = <2>;
	};
	adc1: adc@40012000 {
		compatible = "test,adc"; reg = <0x40012000 0x100>; #io-channel-cells = <1>;
		pinctrl-0 = <&adc1_in3_pa3>; pinctrl-names = "default";
	};
	timers1: timers@40010000 {
		compatible = "test,timers"; reg = <0x40010000 0x400>;
		pwm1: pwm { compatible = "test,pwm"; #pwm-cells = <3>;
			pinctrl-0 = <&tim1_ch1_pe9>; pinctrl-names = "default"; };
	};
	uc-io {
		compatible = "uc,io-channels";
		di1 { kind = "di"; gpios = <&gpiof 15 0x11>; };
		ai0 { kind = "ai"; io-channels = <&adc1 3>; };
		ao0 { kind = "ao"; pwms = <&pwm1 1 1000000 0>; };
	};
	zephyr,user { hil-marker-gpios = <&gpiog 0 0>, <&gpiog 1 0>; };
};
"""

BINDINGS = {  # file names do not clash with Zephyr's own bindings (edtlib includes by name)
    "hiltest-gpio.yaml": "compatible: test,gpio\ninclude: [base.yaml, gpio-controller.yaml]\n"
    "gpio-cells: [pin, flags]\n",
    "hiltest-adc.yaml": "compatible: test,adc\ninclude: [base.yaml, pinctrl-device.yaml]\n"
    "properties:\n  '#io-channel-cells': {type: int, const: 1}\nio-channel-cells: [input]\n",
    "hiltest-timers.yaml": "compatible: test,timers\ninclude: base.yaml\n",
    "hiltest-pwm.yaml": "compatible: test,pwm\ninclude: [base.yaml, pinctrl-device.yaml]\n"
    "properties:\n  '#pwm-cells': {type: int, const: 3}\npwm-cells: [channel, period, flags]\n",
    "hiltest-pinctrl.yaml": "compatible: test,pinctrl\ninclude: base.yaml\n"
    "child-binding:\n  description: pin\n  properties:\n    pinmux: {type: int}\n",
    "hiltest-uc-io.yaml": "compatible: uc,io-channels\nchild-binding:\n  description: channel\n"
    "  properties:\n    kind: {type: string, required: true}\n    gpios: {type: phandle-array}\n"
    "    io-channels: {type: phandle-array}\n    pwms: {type: phandle-array}\n",
}


def test_real_edtlib_edt_is_read_like_the_fake(tmp_path: Path) -> None:
    """The attribute names used on the EDT exist in Zephyr's edtlib, and the pickle loads."""
    base = os.environ.get("ZEPHYR_BASE")
    if not base:
        pytest.skip("ZEPHYR_BASE is not set: the edtlib round trip needs Zephyr's python-devicetree")
    sys.path.insert(0, str(Path(base) / "scripts" / "dts" / "python-devicetree" / "src"))
    edtlib = pytest.importorskip("devicetree.edtlib", reason=f"no python-devicetree under {base}")
    (tmp_path / "test.dts").write_text(DTS)
    for name, text in BINDINGS.items():
        (tmp_path / name).write_text(f"description: check_catalog test binding\n{text}")
    edt = edtlib.EDT(
        str(tmp_path / "test.dts"),
        [str(tmp_path), str(Path(base) / "dts" / "bindings")],
        infer_binding_for_paths=["/zephyr,user"],
    )
    (tmp_path / "zephyr").mkdir()
    with (tmp_path / "zephyr" / "edt.pickle").open("wb") as f:
        pickle.dump(edt, f)
    loaded, board = cc.load_edt(tmp_path)
    assert board is None  # no CMakeCache.txt
    assert cc.dut_catalog(loaded) == [
        cc.Channel(0, "di1", "di", "PF15", True, "up"),
        cc.Channel(1, "ai0", "ai", "PA3", hw="ADC1_IN3"),
        cc.Channel(2, "ao0", "ao", "PE9", hw="TIM1_CH1"),
    ]
    assert [(m.name, m.pin) for m in cc.dut_markers(loaded)] == [("m0", "PG0"), ("m1", "PG1")]


def test_real_dut_build() -> None:
    """End to end on a real DUT build ($HIL_DUT_BUILD, e.g. the instrumented BACnet build)."""
    build = os.environ.get("HIL_DUT_BUILD")
    if not build:
        pytest.skip("HIL_DUT_BUILD is not set (a DUT build directory with zephyr/edt.pickle)")
    assert cc.main([build, "--bench", str(HIL / "host" / "bench.yml.example")]) == 0
