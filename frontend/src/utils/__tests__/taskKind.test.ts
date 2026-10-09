import { describe, expect, it } from "vitest";
import { taskKindKey, taskKindLabel } from "../taskKind";
import zh from "../../i18n/zh-TW.json";

const dict = (zh as any).tasks.kinds as Record<string, string>;
const t = (k: string) => dict[k.replace("tasks.kinds.", "")];
const te = (k: string) => k.startsWith("tasks.kinds.") && k.replace("tasks.kinds.", "") in dict;

describe("作業類型的顯示名稱", () => {
  it("點號換成底線當 i18n 鍵（vue-i18n 會把點號當成巢狀路徑）", () => {
    expect(taskKindKey("checkpoint_gaia.sync")).toBe("tasks.kinds.checkpoint_gaia_sync");
  });

  it("兩個 Check Point 作業分得出來：管理伺服器 vs 閘道", () => {
    expect(taskKindLabel("checkpoint.sync", t, te)).toContain("管理伺服器");
    expect(taskKindLabel("checkpoint_gaia.sync", t, te)).toContain("閘道");
  });

  it("沒有翻譯的類型照原樣顯示，不會空白", () => {
    expect(taskKindLabel("brand_new.sync", t, te)).toBe("brand_new.sync");
  });
});
