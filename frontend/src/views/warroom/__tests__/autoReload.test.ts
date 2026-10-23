import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetVersionCheck } from "@/composables/useVersionCheck";
import { RESUME_KEY, saveResume, startAutoReload, takeResume } from "../autoReload";

describe("war room auto reload", () => {
  let build = "A";
  let ready = false;
  beforeEach(() => {
    vi.useFakeTimers();
    _resetVersionCheck();
    sessionStorage.clear();
    build = "A"; ready = false;
    vi.stubGlobal("__APP_VERSION__", "9.9.9");          // vitest 沒有 vite 的 define
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.startsWith("/version.json")) return new Response(JSON.stringify({ version: "9.9.9", build }));
      if (url.startsWith("/?")) return ready ? new Response('<div id="app"></div>') : new Response("bad gateway", { status: 502 });
      if (url.startsWith("/api/v1/auth/realms")) return new Response("[]", { status: ready ? 200 : 502 });
      return new Response("", { status: 404 });
    }));
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("reloads only after the new build is out AND the site answers, keeping the position", async () => {
    const reload = vi.fn();
    const stop = startAutoReload(() => ({ key: "services", page: 2, paused: true }), reload);
    await vi.advanceTimersByTimeAsync(180_000);
    expect(reload).not.toHaveBeenCalled();               // 還是同一個 build
    build = "B";                                         // 部署了新版，但後端還在重啟
    await vi.advanceTimersByTimeAsync(180_000);
    expect(reload).not.toHaveBeenCalled();
    ready = true;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(takeResume()).toEqual({ key: "services", page: 2, paused: true });
    stop();
  });

  it("resume is taken once and ignored when stale", () => {
    saveResume({ key: "capacity", page: 3, paused: false });
    expect(takeResume()).toEqual({ key: "capacity", page: 3, paused: false });
    expect(takeResume()).toBeNull();
    sessionStorage.setItem(RESUME_KEY, JSON.stringify({ key: "capacity", page: 1, paused: false, at: Date.now() - 11 * 60_000 }));
    expect(takeResume()).toBeNull();
  });
});
