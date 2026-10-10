import { afterEach, describe, expect, it, vi } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { formatOf, saveBlob, setExportLogger, type ExportEvent } from "@/utils/saveFile";
import { exportTable } from "@/utils/tableExport";

/**
 * 瀏覽器端產生的檔案一律經過 utils/saveFile 存檔，才會留下匯出稽核（2026-10-09 合規核對：
 * 表格匯出、報告、拓樸圖等都沒有任何記錄）。自己 `URL.createObjectURL` 存檔就繞過了 ——
 * 新的存檔功能要嘛改用 saveBlob，要嘛寫進下面的例外並附理由。
 */
const SRC = resolve(__dirname, "../..");
const ALLOWED: Record<string, string> = {
  "utils/saveFile.ts": "存檔的唯一出口本身",
  "api/racks.ts": "機櫃嵌入圖的預覽（顯示用的物件網址，不是存檔）",
  "components/SftpBrowser.vue": "SFTP 下載遠端主機的檔案；伺服器端逐檔稽核（sftp_download）",
  "rdweb/fileSave.ts": "RustDesk 網頁客戶端接收遠端檔案；檔案傳輸由 RustDesk 主控台事件記錄",
};

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "__tests__" || name === "node_modules") continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(?:ts|vue)$/.test(name)) out.push(p);
  }
  return out;
}

describe("存檔只走 saveFile", () => {
  it("沒有其他地方自己 createObjectURL 存檔", () => {
    const users = walk(SRC)
      .filter((p) => /URL\.createObjectURL\(/.test(readFileSync(p, "utf8")))
      .map((p) => relative(SRC, p).split("\\").join("/"));
    expect(users.length).toBeGreaterThan(0);
    const offenders = users.filter((p) => !(p in ALLOWED));
    expect(offenders, "這些檔案自己存檔，不會留下匯出稽核：改用 utils/saveFile 的 saveBlob").toEqual([]);
    const stale = Object.keys(ALLOWED).filter((p) => !users.includes(p));
    expect(stale, "例外清單裡的檔案已經不再 createObjectURL，請移除").toEqual([]);
  });
});

describe("saveBlob 回報匯出", () => {
  const events: ExportEvent[] = [];
  afterEach(() => { events.length = 0; setExportLogger(null); vi.restoreAllMocks(); });

  function stubDownload() {
    (URL as any).createObjectURL = vi.fn(() => "blob:x");
    (URL as any).revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  }

  it("一般存檔回報來源、格式與列數", () => {
    stubDownload();
    setExportLogger((e) => events.push(e));
    saveBlob("topology.svg", new Blob(["<svg/>"]), "image/svg+xml", { source: "topology", rows: 12 });
    expect(events).toEqual([{ source: "topology", format: "svg", rows: 12, filename: "topology.svg" }]);
  });

  it("伺服器已記過的下載不重複回報", () => {
    stubDownload();
    setExportLogger((e) => events.push(e));
    saveBlob("addresses.csv", new Blob(["a"]), "text/csv", { source: "subnet-csv", audited: true });
    expect(events).toEqual([]);
  });

  it("表格匯出一次只回報一筆，帶列數", () => {
    stubDownload();
    setExportLogger((e) => events.push(e));
    exportTable("csv", "devices", [{ key: "name", label: "Name" }], [{ name: "a" }, { name: "b" }]);
    expect(events).toEqual([{ source: "devices", format: "csv", rows: 2, filename: "devices.csv" }]);
  });

  it("回報失敗不影響存檔", () => {
    stubDownload();
    setExportLogger(() => { throw new Error("offline"); });
    expect(() => saveBlob("x.txt", new Blob(["x"]), "text/plain", { source: "x" })).not.toThrow();
  });

  it("副檔名判斷", () => {
    expect(formatOf("a.tar.GZ")).toBe("gz");
    expect(formatOf("noext")).toBe("bin");
  });
});
