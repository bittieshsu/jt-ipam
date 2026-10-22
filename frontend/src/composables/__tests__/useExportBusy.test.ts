import { describe, expect, it, vi } from "vitest";
import { useExportBusy } from "../useExportBusy";

describe("useExportBusy（使用者 2026-10-08：選了檔案格式、正在處理時按鈕要反灰並轉圈）", () => {
  it("處理期間 exporting 為 true，而且先讓畫面畫出轉圈才開始做（同步的產檔也看得到）", async () => {
    const { exporting, run } = useExportBusy();
    const seen: boolean[] = [];
    const p = run(() => { seen.push(exporting.value); });
    expect(exporting.value).toBe(true);
    expect(seen).toEqual([]);            // 還沒開始做：先讓出一個畫面
    await p;
    expect(seen).toEqual([true]);
    expect(exporting.value).toBe(false);
  });

  it("非同步的產檔要等它做完才恢復", async () => {
    const { exporting, run } = useExportBusy();
    let release: (() => void) | undefined;
    const p = run(() => new Promise<void>((r) => { release = r; }));
    // 等到產檔真的開始（先讓出一個畫面）；固定等 30ms 在機器忙的時候不夠（10-22 發版時與後端全套測試同跑失敗過）
    await vi.waitFor(() => expect(release).toBeTypeOf("function"), { timeout: 2000 });
    expect(exporting.value).toBe(true);
    release!();
    await p;
    expect(exporting.value).toBe(false);
  });

  it("出錯也會恢復，錯誤照樣往上拋", async () => {
    const { exporting, run } = useExportBusy();
    await expect(run(() => { throw new Error("boom"); })).rejects.toThrow("boom");
    expect(exporting.value).toBe(false);
  });

  it("處理中再選一次不會重複產檔", async () => {
    const { run } = useExportBusy();
    let n = 0;
    await Promise.all([run(() => { n += 1; }), run(() => { n += 1; })]);
    expect(n).toBe(1);
  });
});
