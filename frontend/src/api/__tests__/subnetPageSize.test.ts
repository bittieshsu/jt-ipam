/**
 * 子網路清單 API 每頁上限 500（backend endpoints/subnets.py 的 page_size le=500）。
 * 超過就 422，畫面只會跳一個「page_size：不可大於 500」—— 儀表板趨勢設定曾經一打開就這樣（2026-10-20）。
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) return f === "__tests__" ? [] : files(p);
    return /\.(vue|ts)$/.test(f) ? [p] : [];
  });
}

describe("listSubnets 的 pageSize 不可超過後端上限", () => {
  it("沒有任何呼叫要求超過 500 筆", () => {
    const bad: string[] = [];
    for (const f of files(join(__dirname, "..", ".."))) {
      const src = readFileSync(f, "utf8");
      for (const m of src.matchAll(/listSubnets\(\{[^}]*pageSize:\s*(\d+)/g)) {
        if (Number(m[1]) > 500) bad.push(`${f}: pageSize ${m[1]}`);
      }
    }
    expect(bad).toEqual([]);
  });
});
