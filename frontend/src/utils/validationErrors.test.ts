import { describe, expect, it } from "vitest";
import { formatValidationErrors, fieldOf } from "./validationErrors";
import zh from "@/i18n/zh-TW.json";
import en from "@/i18n/en-US.json";
import ja from "@/i18n/ja-JP.json";

// 測試用的翻譯：回傳 key 與參數，方便看出用了哪一條
const dict: Record<string, string> = {
  "fields.scope_subnet_ids": "允許同步的子網路",
  "fields.base_url": "Base URL",
  "errors.v_field": "{field}：{reason}",
  "errors.v_sep": "；",
};
const t = (k: string, p?: Record<string, unknown>) => {
  if (k in dict) return dict[k].replace(/\{(\w+)\}/g, (_m, name: string) => String(p?.[name] ?? ""));
  return p && Object.keys(p).length ? `${k}(${JSON.stringify(p)})` : k;
};
const te = (k: string) => k in dict;

describe("validationErrors", () => {
  it("takes the field name from loc, skipping body/query and list indexes", () => {
    expect(fieldOf(["body", "scope_subnet_ids"])).toBe("scope_subnet_ids");
    expect(fieldOf(["body", "rules", 2, "port"])).toBe("port");
    expect(fieldOf(["query", "page"])).toBe("page");
    expect(fieldOf(["body"])).toBe("");
  });

  it("names the field and the reason instead of 'Invalid request'", () => {
    const out = formatValidationErrors(t, te, [
      { type: "too_short", loc: ["body", "scope_subnet_ids"], msg: "List should have at least 1 item", ctx: { min_length: 1 } },
      { type: "value_error", loc: ["body", "base_url"], msg: "Value error, base_url must start with http:// or https://" },
    ]);
    expect(out).toContain("允許同步的子網路");
    expect(out).toContain('errors.v_too_short_list({"n":1})');
    expect(out).toContain("Base URL");
    expect(out).toContain("base_url must start with http:// or https://");
    expect(out).not.toContain("Value error,");
  });

  it("falls back to the raw field name and pydantic message", () => {
    const out = formatValidationErrors(t, te, [{ type: "something_new", loc: ["body", "weird_field"], msg: "Weird" }]);
    expect(out).toContain("weird_field");
    expect(out).toContain("Weird");
  });

  it("caps the list and says how many more there are", () => {
    const errs = Array.from({ length: 5 }, (_, i) => ({ type: "missing", loc: ["body", `f${i}`], msg: "Field required" }));
    const out = formatValidationErrors(t, te, errs);
    expect(out).toContain("f0");
    expect(out).toContain("f2");
    expect(out).not.toContain("f3");
    expect(out).toContain('errors.v_more({"n":2})');
  });

  it("returns empty for anything that is not a validation error list", () => {
    expect(formatValidationErrors(t, te, [])).toBe("");
    expect(formatValidationErrors(t, te, "nope" as unknown as unknown[])).toBe("");
  });

  it("every reason and field label exists in all three locales with the same placeholders", () => {
    const vars = (x: string) => (x.match(/\{\w+\}/g) ?? []).sort().join(",");
    const pick = (loc: any) => ({
      ...Object.fromEntries(Object.entries(loc.errors).filter(([k]) => k.startsWith("v_")).map(([k, v]) => [`errors.${k}`, v])),
      ...Object.fromEntries(Object.entries(loc.fields).map(([k, v]) => [`fields.${k}`, v])),
    }) as Record<string, string>;
    const z = pick(zh), e = pick(en), j = pick(ja);
    expect(Object.keys(z).length).toBeGreaterThan(20);
    expect(Object.keys(e).sort()).toEqual(Object.keys(z).sort());
    expect(Object.keys(j).sort()).toEqual(Object.keys(z).sort());
    for (const k of Object.keys(z)) {
      expect(vars(e[k]), k).toBe(vars(z[k]));
      expect(vars(j[k]), k).toBe(vars(z[k]));
    }
  });
});
