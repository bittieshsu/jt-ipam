import { describe, expect, it } from "vitest";
import { humanToolName } from "@/utils/toolLabel";

describe("humanToolName", () => {
  it("動詞＋名詞組合成看得懂的說明", () => {
    expect(humanToolName("get_ip_detail")).toBe("查詢IP詳細資料");
    expect(humanToolName("list_devices")).toBe("列出裝置");
    expect(humanToolName("get_subnet_usage")).toBe("查詢子網路使用率");
    expect(humanToolName("list_anomalies")).toBe("列出異常");
  });

  it("不認得的字保留原樣，不是空白 —— 寧可顯示代號也不要什麼都不說", () => {
    expect(humanToolName("get_widget_frobnicator")).toContain("widget");
    expect(humanToolName("mystery")).toBe("mystery");
  });

  it("空值不會壞掉", () => {
    expect(humanToolName(null)).toBe("");
    expect(humanToolName(undefined)).toBe("");
  });
});
