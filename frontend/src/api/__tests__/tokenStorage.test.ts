import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

/**
 * 權杖不可以放 localStorage（2026-10-09 合規核對：以前存取權杖與 14 天的更新權杖都在那裡，
 * 任何 XSS 都拿得走）。更新權杖只在後端設的 HttpOnly Cookie；存取權杖只經過 api/token.ts。
 */
const SRC = resolve(__dirname, "../..");
function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "__tests__" || name === "node_modules") continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(?:ts|vue)$/.test(name)) out.push(p);
  }
  return out;
}

describe("權杖存放位置", () => {
  it("沒有地方把權杖寫進 localStorage，也沒有地方繞過 api/token 直接讀寫", () => {
    const bad: string[] = [];
    for (const p of walk(SRC)) {
      const rel = relative(SRC, p).split("\\").join("/");
      const src = readFileSync(p, "utf8");
      if (/localStorage\.setItem\(\s*["'](?:access|refresh)_token/.test(src)) bad.push(`${rel}: localStorage.setItem`);
      if (rel !== "api/token.ts" && /(?:local|session)Storage\.\w+\(\s*["'](?:access|refresh)_token/.test(src)) {
        bad.push(`${rel}: 直接讀寫 Storage`);
      }
    }
    expect(bad).toEqual([]);
  });
});
