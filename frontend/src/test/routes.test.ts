import { describe, expect, it } from "vitest";
import { parseHash, routeToHash } from "../shell/routes";

describe("hash routes", () => {
  it("defaults to fleet", () => {
    expect(parseHash("")).toEqual({ name: "fleet" });
    expect(parseHash("#/")).toEqual({ name: "fleet" });
    expect(parseHash("#/unknown")).toEqual({ name: "fleet" });
  });

  it("parses server routes with tabs", () => {
    expect(parseHash("#/server/srv1/gpus")).toEqual({
      name: "server",
      serverId: "srv1",
      tab: "gpus",
    });
    expect(parseHash("#/server/srv1")).toEqual({
      name: "server",
      serverId: "srv1",
      tab: "overview",
    });
    expect(parseHash("#/server/srv1/bogus")).toEqual({
      name: "server",
      serverId: "srv1",
      tab: "overview",
    });
  });

  it("parses settings and preview", () => {
    expect(parseHash("#/settings")).toEqual({ name: "settings" });
    expect(parseHash("#/preview")).toEqual({ name: "preview" });
  });

  it("round-trips through routeToHash", () => {
    const route = { name: "server", serverId: "srv with space", tab: "processes" } as const;
    expect(parseHash(routeToHash(route))).toEqual(route);
    expect(parseHash(routeToHash({ name: "settings" }))).toEqual({ name: "settings" });
    expect(parseHash(routeToHash({ name: "fleet" }))).toEqual({ name: "fleet" });
    expect(parseHash(routeToHash({ name: "preview" }))).toEqual({ name: "preview" });
  });
});
