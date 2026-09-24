import type { ReactNode } from "react";
import { plural } from "../lib/format";
import { devicesIn, PROTOCOL_LABELS, spacePath } from "../lib/site";
import { useHub } from "../state/hub";
import { useUi, type WorkTab } from "../state/ui";
import { Chip } from "../ui/common";
import { TabList, TabPanel, type TabSpec } from "../ui/Tabs";
import { ChangesPane } from "./ChangesPane";
import { DevicesPane } from "./DevicesPane";
import { PointsPane } from "./PointsPane";
import { TestsPane } from "./TestsPane";

function Header() {
  const hub = useHub();
  const ui = useUi();
  const site = hub.site.data;
  if (!site) {
    return (
      <div className="whead">
        <h2>{hub.health.data?.site ?? "Site"}</h2>
      </div>
    );
  }
  const root = site.name.toUpperCase();
  const scope = ui.scope;
  let crumbs: string[] = [root];
  let title = site.description || site.name;
  let meta: ReactNode = null;

  if (scope.kind === "space") {
    const path = spacePath(site, scope.id);
    crumbs = [root, ...path.map((s) => s.name)];
    title = path.at(-1)?.name ?? scope.id;
    const devs = devicesIn(site, scope.id, true);
    const offline = devs.filter((d) => !d.online).length;
    meta = (
      <>
        <Chip>{plural(devs.length, "device")}</Chip>
        {offline > 0 ? (
          <Chip tone="warn" dot>
            {offline} offline
          </Chip>
        ) : devs.length > 0 ? (
          <Chip tone="ok" dot>
            All online
          </Chip>
        ) : null}
      </>
    );
  } else if (scope.kind === "device") {
    const d = site.devices.find((x) => x.name === scope.name);
    crumbs = [root, ...spacePath(site, d?.space ?? null).map((s) => s.name), scope.name];
    title = scope.name;
    if (d) {
      meta = (
        <>
          <Chip tone={d.online ? "ok" : ""} dot>
            {d.online ? "Online" : "Offline"}
          </Chip>
          <Chip>{PROTOCOL_LABELS[d.protocol]}</Chip>
          {d.model && <Chip>{d.model}</Chip>}
          {d.instance !== null && <Chip className="mono">BACnet {d.instance}</Chip>}
          <Chip className="mono">{d.address}</Chip>
          {d.firmware && <Chip>fw {d.firmware}</Chip>}
        </>
      );
    }
  } else {
    const s = site.summary;
    meta = (
      <>
        <Chip>{plural(s.devices, "device")}</Chip>
        <Chip>{plural(s.points, "point")}</Chip>
        {s.unassigned > 0 && <Chip tone="warn">{s.unassigned} not placed</Chip>}
      </>
    );
  }
  return (
    <div className="whead">
      <div className="crumb">{crumbs.join(" / ")}</div>
      <h2>{title}</h2>
      <div className="meta">{meta}</div>
    </div>
  );
}

/** Centre pane: header for the selected scope and the Points / Changes / Tests / Devices tabs. */
export function Workspace() {
  const hub = useHub();
  const ui = useUi();
  const changes = hub.plan.data?.plan?.changes.length ?? 0;
  const tabs: TabSpec<WorkTab>[] = [
    { id: "points", label: "Points" },
    {
      id: "changes",
      label: (
        <>
          Changes <span className={`badge${changes ? "" : " zero"}`}>{changes}</span>
        </>
      ),
      name: `Changes, ${changes} pending`,
    },
    { id: "tests", label: "Tests" },
    { id: "devices", label: ui.scope.kind === "device" ? "Device" : "Devices" },
  ];
  return (
    <main className="work" aria-label="Workspace">
      <Header />
      <TabList tabs={tabs} value={ui.workTab} onChange={ui.setWorkTab} label="Workspace" prefix="ws" className="tabs" />
      <TabPanel prefix="ws" id={ui.workTab} className="tabpane">
        {ui.workTab === "points" && <PointsPane scope={ui.scope} query={ui.query} />}
        {ui.workTab === "changes" && <ChangesPane />}
        {ui.workTab === "tests" && <TestsPane />}
        {ui.workTab === "devices" && <DevicesPane scope={ui.scope} />}
      </TabPanel>
    </main>
  );
}
