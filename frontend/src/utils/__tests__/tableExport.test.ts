import { describe, it, expect } from "vitest";
import { cellText, columnsForExport } from "../tableExport";

describe("tableExport", () => {
  // 「未裝 Agent 的 IP」匯出的子網路／區段／單位一直是空白：欄位 key 是 subnet，
  // 資料欄位卻叫 subnet_cidr。畫面用 render 顯示所以看不出來（2026-09-27 發現）。
  it("欄位可以自帶匯出值（exportValue），不必讓 key 剛好等於資料欄位名", () => {
    const cols = columnsForExport([
      { title: "子網路", key: "subnet", render: () => null, exportValue: (r: any) => r.subnet_cidr },
      { title: "IP", key: "ip" },
      { title: () => "操作", key: "actions" },
    ]);
    expect(cols.map((c) => c.key)).toEqual(["subnet", "ip"]);
    const row = { ip: "198.51.100.7", subnet_cidr: "198.51.100.0/24" };
    expect(cellText(row, cols[0])).toBe("198.51.100.0/24");
    expect(cellText(row, cols[1])).toBe("198.51.100.7");
  });

  it("沒有值就是空字串；陣列用逗號接", () => {
    expect(cellText({}, { key: "x", label: "x" })).toBe("");
    expect(cellText({ x: ["a", "b"] }, { key: "x", label: "x" })).toBe("a, b");
  });
});

describe("關聯欄位匯出名稱而不是 UUID（issue #50：電路的供應商、類型匯出成編碼）", async () => {
  const { h } = await import("vue");
  const { NTag } = await import("naive-ui");
  const ID = "adbedc6c-626a-4a8f-89aa-fdbe69d9fe9b";
  const names: Record<string, string> = { [ID]: "Hinet" };

  it("畫面用 render 換成名稱的欄位，匯出也是名稱", () => {
    const [col] = columnsForExport([{ title: "供應商", key: "provider_id", render: (r: any) => names[r.provider_id] ?? "—" }]);
    expect(cellText({ provider_id: ID }, col)).toBe("Hinet");
  });

  it("render 回傳元件（標籤、連結）也抓得到裡面的文字", () => {
    const [col] = columnsForExport([{ title: "單位", key: "customer_id",
      render: (r: any) => h(NTag, { size: "small" }, () => names[r.customer_id]) }]);
    expect(cellText({ customer_id: ID }, col)).toBe("Hinet");
  });

  it("找不到名稱（畫面顯示 —）就留空，不要匯出 UUID", () => {
    const [col] = columnsForExport([{ title: "類型", key: "type_id", render: () => "—" }]);
    expect(cellText({ type_id: ID }, col)).toBe("");
  });

  it("不是 UUID 的原始值照舊（MAC 欄畫面上多一行廠商，匯出只要 MAC）", () => {
    const [col] = columnsForExport([{ title: "MAC", key: "mac",
      render: (r: any) => h("div", null, [h("div", null, r.mac), h("div", null, "Vendor Inc.")]) }]);
    expect(cellText({ mac: "00:00:5e:00:53:01" }, col)).toBe("00:00:5e:00:53:01");
  });

  it("欄位自己寫的 exportValue 優先", () => {
    const [col] = columnsForExport([{ title: "x", key: "provider_id", render: () => "畫面", exportValue: () => "匯出" }]);
    expect(cellText({ provider_id: ID }, col)).toBe("匯出");
  });
});
