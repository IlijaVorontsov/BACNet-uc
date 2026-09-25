import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { diffStats, parseUnifiedDiff } from "./diff";
import { formatDuration, formatExpiry, formatReading, formatValue, initials, unitSymbol } from "./format";
import { Markdown } from "./markdown";
import { buildingSpace, crumbPath, resolveDevice, spacePath, topSpaces } from "./site";
import type { Device, Site } from "../api/types";

describe("parseUnifiedDiff", () => {
  it("classifies lines and numbers them from the hunk header", () => {
    const lines = parseUnifiedDiff("--- a/io.json\n+++ b/io.json\n@@ -3,2 +3,3 @@ x\n keep\n-old\n+new\n+more\n");
    expect(lines.map((l) => l.kind)).toEqual(["meta", "meta", "hunk", "ctx", "del", "add", "add"]);
    expect(lines[3]).toMatchObject({ text: "keep", oldNo: 3, newNo: 3 });
    expect(lines[4]).toMatchObject({ text: "old", oldNo: 4, newNo: null });
    expect(lines[6]).toMatchObject({ text: "more", newNo: 5 });
    expect(diffStats(lines)).toEqual({ added: 2, removed: 1 });
  });

  it("accepts diffs without headers and CRLF", () => {
    const lines = parseUnifiedDiff("-a: 1\r\n+a: 2\r\nplain");
    expect(lines.map((l) => [l.kind, l.text])).toEqual([
      ["del", "a: 1"],
      ["add", "a: 2"],
      ["ctx", "plain"],
    ]);
  });

  it("treats --- inside a hunk as a removed line", () => {
    const lines = parseUnifiedDiff("@@ -1 +1 @@\n--- old yaml separator\n");
    expect(lines[1]).toMatchObject({ kind: "del", text: "-- old yaml separator" });
  });

  it("returns nothing for an empty diff", () => {
    expect(parseUnifiedDiff("")).toEqual([]);
  });
});

describe("format", () => {
  const real = { obj: "analog-input:1", datatype: "real" as const, units: "degrees-celsius" };
  it("formats values by point type", () => {
    expect(formatValue(22.44, real)).toBe("22.4");
    expect(formatValue(22, real)).toBe("22.0");
    expect(formatValue(35.4, { obj: "analog-output:1", datatype: "real", units: "percent" })).toBe("35");
    expect(formatValue(1, { obj: "binary-input:1", datatype: "enum", units: null })).toBe("active");
    expect(formatValue(null, real)).toBe("–");
    expect(formatValue("on")).toBe("on");
    expect(formatReading({ id: "x", value: 612, ts: 1, quality: "good" }, { obj: "co2", datatype: "real", units: "parts-per-million" })).toBe("612 ppm");
    expect(unitSymbol("no-units")).toBe("");
    expect(unitSymbol("furlongs")).toBe("furlongs");
  });

  it("formats durations and expiry", () => {
    expect(formatDuration(200)).toBe("0.2 s");
    expect(formatDuration(21000)).toBe("21 s");
    expect(formatDuration(65000)).toBe("1 min 5 s");
    expect(formatExpiry(1000, 2000)).toBe("expired");
    expect(formatExpiry(1000 + 1800, 1000)).toBe("expires in 30 min");
    expect(initials("dev")).toBe("DE");
    expect(initials("ilija vorontsov")).toBe("IV");
  });
});

describe("Markdown", () => {
  it("renders paragraphs, lists, code and bold without raw HTML", () => {
    const { container } = render(
      <Markdown text={"Held at **100 %** by `r205-ctl`.\n\n- one\n- two\n\n<img src=x onerror=alert(1)>"} />,
    );
    expect(container.querySelectorAll("p")).toHaveLength(2);
    expect(container.querySelector("strong")?.textContent).toBe("100 %");
    expect(container.querySelector("code")?.textContent).toBe("r205-ctl");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});

describe("site helpers", () => {
  const dev = (name: string, instance: number | null, hwid = ""): Device => ({
    name,
    protocol: "bacnet-uc",
    address: "",
    online: true,
    managed: true,
    model: "",
    firmware: "",
    hwid,
    instance,
    space: null,
    last_seen: null,
  });
  const devices = [dev("r204-ctl", 2041, "0x3a7f9"), dev("ahu1-ctl", 100)];

  it("resolves scanned labels and typed ids to devices", () => {
    expect(resolveDevice("r204-ctl", devices)?.name).toBe("r204-ctl");
    expect(resolveDevice(" R204-CTL ", devices)?.name).toBe("r204-ctl");
    expect(resolveDevice("2041", devices)?.name).toBe("r204-ctl");
    expect(resolveDevice("uc:ahu1-ctl", devices)?.name).toBe("ahu1-ctl");
    expect(resolveDevice("https://hub.local/?device=ahu1-ctl", devices)?.name).toBe("ahu1-ctl");
    expect(resolveDevice("https://hub.local/devices/r204-ctl", devices)?.name).toBe("r204-ctl");
    expect(resolveDevice("0x3A7F9", devices)?.name).toBe("r204-ctl");
    expect(resolveDevice("nope", devices)).toBeNull();
    expect(resolveDevice("", devices)).toBeNull();
  });

  it("walks the space tree root first and survives cycles", () => {
    const site = {
      spaces: [
        { id: "f2", name: "Floor 2", parent: null },
        { id: "r204", name: "Room 204", parent: "f2" },
        { id: "a", name: "A", parent: "b" },
        { id: "b", name: "B", parent: "a" },
      ],
    } as Site;
    expect(spacePath(site, "r204").map((s) => s.id)).toEqual(["f2", "r204"]);
    expect(spacePath(site, "a").map((s) => s.id)).toEqual(["b", "a"]);
    expect(spacePath(site, null)).toEqual([]);
  });

  it("shows a site that is one building space as the site itself", () => {
    const building = {
      spaces: [
        { id: "hq", name: "HQ", parent: null },
        { id: "f2", name: "Floor 2", parent: "hq" },
        { id: "r204", name: "Room 204", parent: "f2" },
        { id: "plant", name: "Plant room", parent: "hq" },
      ],
    } as Site;
    expect(buildingSpace(building)?.id).toBe("hq");
    expect(topSpaces(building).map((s) => s.id)).toEqual(["f2", "plant"]);
    expect(crumbPath(building, "r204").map((s) => s.name)).toEqual(["Floor 2", "Room 204"]);

    const campus = { spaces: [...building.spaces, { id: "b2", name: "Annex", parent: null }] } as Site;
    expect(buildingSpace(campus)).toBeNull();
    expect(topSpaces(campus).map((s) => s.id)).toEqual(["hq", "b2"]);
    expect(crumbPath(campus, "r204").map((s) => s.name)).toEqual(["HQ", "Floor 2", "Room 204"]);
    // A single space without children is a room, not a building.
    expect(buildingSpace({ spaces: [{ id: "lab", name: "Lab", parent: null }] } as Site)).toBeNull();
  });
});
