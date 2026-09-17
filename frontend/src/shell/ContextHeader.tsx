import { ArrowLeft } from "@phosphor-icons/react";
import { useConsoleStore } from "../store/consoleStore";
import { useWorkspaceStore } from "../store/workspaceStore";
import { IconButton, StatusDot, dotStatusFromServerStatus } from "../design";
import { useRelative, useT } from "../i18n";
import { useNow } from "../utils/clock";
import { FLEET_HASH } from "./routes";
import { ServerDetailTabs } from "./ServerDetailTabs";


/** 56px context header: fleet aggregate, or server identity + status + tabs. */
export function ContextHeader() {
  const t = useT();
  const relative = useRelative();
  const route = useConsoleStore((state) => state.route);
  const servers = useConsoleStore((state) => state.servers);
  const fleetSummary = useConsoleStore((state) => state.fleetSummary);
  const statuses = useConsoleStore((state) => state.statuses);
  const navigate = useConsoleStore((state) => state.navigate);
  const now = useNow();
  const projects = useWorkspaceStore((state) => state.projects);

  if (route.name === "server") {
    const record = servers.find((server) => server.server_id === route.serverId);
    const entry = fleetSummary?.servers.find((item) => item.server_id === route.serverId);
    const status = entry?.status ?? statuses[route.serverId] ?? "unknown";
    const freshness = entry?.updated_at ?? null;
    return (
      <header className="ctx">
        <IconButton label={t.nav.back} onClick={() => navigate(FLEET_HASH)}>
          <ArrowLeft size={16} />
        </IconButton>
        <span className="ctx__name">{record?.display_name ?? route.serverId}</span>
        <StatusDot status={dotStatusFromServerStatus(status)} label={status} />
        <span className="ctx__status mono">{t.status[status]}</span>
        <span className="ctx__freshness mono">{relative(freshness, now)}</span>
        <div className="ctx__spacer" />
        <ServerDetailTabs />
      </header>
    );
  }

  const title =
    route.name === "settings"
      ? t.settings.title
      : route.name === "workspace"
        ? t.workspace.title
        : route.name === "transfers"
          ? t.transfers.title
          : route.name === "preview"
            ? "Preview"
            : t.nav.fleet;
  // Project detail shows a quiet breadcrumb: 工作区 · {project}.
  const projectName =
    route.name === "workspace" && route.section === "projects" && route.projectId !== undefined
      ? (projects.find((p) => p.project_id === route.projectId)?.name ?? null)
      : null;
  return (
    <header className="ctx">
      <span className="ctx__title">{title}</span>
      {projectName !== null && <span className="ctx__name">{projectName}</span>}
      {route.name === "fleet" && fleetSummary !== null && (
        <span className="ctx__freshness mono" title={fleetSummary.generated_at}>
          {relative(fleetSummary.generated_at, now)}
        </span>
      )}
    </header>
  );
}
