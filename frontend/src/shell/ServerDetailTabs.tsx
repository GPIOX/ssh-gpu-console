import { SERVER_TABS } from "./routes";
import { useT } from "../i18n";
import { useConsoleStore } from "../store/consoleStore";
import { getHiddenSections, subscribeSections } from "../utils/sections";
import { useSyncExternalStore } from "react";
import { cx } from "../utils/cx";

/** Sub-tab strip in the context header (Overview/GPUs/Processes/System/Storage/Network).
 *  Hidden sections (Settings → Preferences) are filtered out; Overview/GPUs always show. */
export function ServerDetailTabs() {
  const t = useT();
  const route = useConsoleStore((state) => state.route);
  const setTab = useConsoleStore((state) => state.setTab);
  const hidden = useSyncExternalStore(subscribeSections, getHiddenSections, getHiddenSections);
  if (route.name !== "server") return null;
  return (
    <nav className="tabs" aria-label="Server sections">
      {SERVER_TABS.filter((tab) => !hidden.includes(tab)).map((tab) => (
        <button
          key={tab}
          type="button"
          className={cx("tabs__tab", route.tab === tab && "tabs__tab--active")}
          onClick={() => setTab(tab)}
        >
          {t.tabs[tab]}
        </button>
      ))}
    </nav>
  );
}
