import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// The Node-backed jsdom environment ships no Web Storage implementation
// (Node only enables it with --localstorage-file). UI-preference persistence
// (sgc.theme / sgc.locale / sgc.sections) must stay observable in tests, so a
// minimal in-memory Storage is installed when the platform provides none.
if (typeof globalThis.localStorage === "undefined") {
  const backing = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return backing.size;
    },
    getItem: (key) => (backing.has(key) ? (backing.get(key) as string) : null),
    setItem: (key, value) => {
      backing.set(key, String(value));
    },
    removeItem: (key) => {
      backing.delete(key);
    },
    clear: () => {
      backing.clear();
    },
    key: (index) => [...backing.keys()][index] ?? null,
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: storage,
    configurable: true,
  });
}

afterEach(() => {
  cleanup();
});
