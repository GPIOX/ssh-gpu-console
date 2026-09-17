import { useEffect, type ReactNode } from "react";
import { useConsoleStore } from "./store/consoleStore";
import { ContextHeader } from "./shell/ContextHeader";
import { LeftRail } from "./shell/LeftRail";
import { PreviewPage } from "./shell/PreviewPage";
import { FleetPage } from "./features/fleet/FleetPage";
import { ServerPage } from "./features/server/ServerPage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { WorkspacePage } from "./features/workspace/WorkspacePage";
import { TransfersPage } from "./features/transfers/TransfersPage";
import "./shell/shell.css";

/** App shell: fixed left rail + context header + scrolling dense canvas. */
export function App() {
  const route = useConsoleStore((state) => state.route);
  const connect = useConsoleStore((state) => state.connect);

  useEffect(() => {
    connect(); // idempotent; wires realtime listeners + hash routing + initial loads
  }, [connect]);

  let page: ReactNode;
  switch (route.name) {
    case "server":
      page = <ServerPage serverId={route.serverId} />;
      break;
    case "settings":
      page = <SettingsPage />;
      break;
    case "preview":
      page = <PreviewPage />;
      break;
    case "fleet":
      page = <FleetPage />;
      break;
    case "workspace":
      page = <WorkspacePage />;
      break;
    case "transfers":
      page = <TransfersPage />;
      break;
  }

  return (
    <div className="app-frame">
      <LeftRail />
      <div className="app-main">
        <ContextHeader />
        <main className="app-content">{page}</main>
      </div>
    </div>
  );
}
