import { AgentPanel } from "../agent/AgentPanel";
import { Workspace } from "../panes/Workspace";
import { SiteTree } from "./SiteTree";
import { TopBar } from "./TopBar";

export function DesktopApp() {
  return (
    <div className="app desk">
      <TopBar />
      <div className="body3">
        <SiteTree />
        <Workspace />
        <AgentPanel />
      </div>
    </div>
  );
}
