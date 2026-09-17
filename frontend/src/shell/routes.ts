/**
 * Hash routes: #/fleet (default) · #/server/{id}/{tab} · #/workspace/{section}
 * (+#/workspace/projects/{id}, #/workspace/datasets/{id}) · #/transfers ·
 * #/settings · #/preview. Pure module — imported by the store (routing state)
 * and by shell components.
 */

export const SERVER_TABS = [
  "overview",
  "gpus",
  "processes",
  "system",
  "storage",
  "network",
] as const;

export type ServerTab = (typeof SERVER_TABS)[number];

export const DEFAULT_TAB: ServerTab = "overview";

export const SERVER_TAB_LABELS: Record<ServerTab, string> = {
  overview: "Overview",
  gpus: "GPUs",
  processes: "Processes",
  system: "System",
  storage: "Storage",
  network: "Network",
};

/** Workspace tabs in rail/header order. */
export const WORKSPACE_SECTIONS = ["projects", "datasets", "models", "launches"] as const;

export type WorkspaceSection = (typeof WORKSPACE_SECTIONS)[number];

export const DEFAULT_WORKSPACE_SECTION: WorkspaceSection = "projects";

export type Route =
  | { name: "fleet" }
  | { name: "server"; serverId: string; tab: ServerTab }
  | { name: "workspace"; section: WorkspaceSection; projectId?: string; artifactId?: string }
  | { name: "transfers" }
  | { name: "settings" }
  | { name: "preview" };

function decodeSegment(raw: string | undefined): string | undefined {
  if (raw === undefined || raw === "") return undefined;
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export function parseHash(hash: string): Route {
  const clean = hash.replace(/^#\/?/, "");
  const segments = clean.split("/");
  const head = segments[0] ?? "";
  if (head === "server" && segments[1]) {
    const rawTab = segments[2] ?? "";
    const tab = (SERVER_TABS as readonly string[]).includes(rawTab)
      ? (rawTab as ServerTab)
      : DEFAULT_TAB;
    const serverId = decodeSegment(segments[1]) ?? segments[1];
    return { name: "server", serverId, tab };
  }
  if (head === "workspace") {
    const rawSection = segments[1] ?? "";
    const section = (WORKSPACE_SECTIONS as readonly string[]).includes(rawSection)
      ? (rawSection as WorkspaceSection)
      : DEFAULT_WORKSPACE_SECTION;
    // Per contract only projects/:id and datasets/:id carry an object id.
    const projectId = section === "projects" ? decodeSegment(segments[2]) : undefined;
    const artifactId =
      section === "datasets" || section === "models" ? decodeSegment(segments[2]) : undefined;
    return { name: "workspace", section, projectId, artifactId };
  }
  if (head === "transfers") return { name: "transfers" };
  if (head === "settings") return { name: "settings" };
  if (head === "preview") return { name: "preview" };
  return { name: "fleet" };
}

export function routeToHash(route: Route): string {
  switch (route.name) {
    case "server":
      return `#/server/${encodeURIComponent(route.serverId)}/${route.tab}`;
    case "workspace": {
      const id = route.projectId ?? route.artifactId;
      return id === undefined
        ? `#/workspace/${route.section}`
        : `#/workspace/${route.section}/${encodeURIComponent(id)}`;
    }
    case "transfers":
      return "#/transfers";
    case "settings":
      return "#/settings";
    case "preview":
      return "#/preview";
    case "fleet":
      return "#/fleet";
  }
}

/** FLEET link target used by the rail and the detail back button. */
export const FLEET_HASH = "#/fleet";

/** Workspace entry target used by the rail. */
export const WORKSPACE_HASH = "#/workspace/projects";

/** Transfer Center entry target used by the rail and the 同步到… menu items. */
export const TRANSFERS_HASH = "#/transfers";
