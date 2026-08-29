/**
 * 拖曳進來的項目要正確排除資料夾。
 *
 * 客戶回報：拖「一個資料夾 + 一個檔案」進 SFTP，兩個都失敗、伺服器等滿逾時才放棄。
 * 原因是判斷式寫成 `f.size > 0` —— **macOS 交出來的資料夾 File.size 是 256**，
 * 於是資料夾通過篩選被當成檔案上傳，讀內容時才失敗（錯誤訊息裡的「0/256 位元組」）。
 *
 * 正確作法是逐項對照 `webkitGetAsEntry().isFile`，大小完全不能拿來判斷型別。
 */
import { describe, expect, it } from "vitest";
import { pickDroppedFiles } from "../dropFilter";

const f = (name: string, size: number) => ({ name, size }) as File;

describe("拖曳項目的篩選", () => {
  it("排除資料夾，即使它回報的大小不是 0（macOS 是 256）", () => {
    const files = [f("some-folder", 256), f("installer.exe", 5831130)];
    const entries = [{ isFile: false }, { isFile: true }];
    const r = pickDroppedFiles(files, entries);
    expect(r.files.map((x) => x.name)).toEqual(["installer.exe"]);
    expect(r.skippedDirs).toBe(1);
  });

  it("拿不到 entry 時保守放行（後面還有可讀性檢查把關）", () => {
    const files = [f("a.bin", 10)];
    expect(pickDroppedFiles(files, [null]).files).toHaveLength(1);
  });

  it("全是檔案時不誤報略過", () => {
    const files = [f("a", 1), f("b", 2)];
    const r = pickDroppedFiles(files, [{ isFile: true }, { isFile: true }]);
    expect(r.files).toHaveLength(2);
    expect(r.skippedDirs).toBe(0);
  });

  it("空檔案是合法的檔案，不可以被當成資料夾濾掉", () => {
    const r = pickDroppedFiles([f("empty.txt", 0)], [{ isFile: true }]);
    expect(r.files).toHaveLength(1);
  });
});
