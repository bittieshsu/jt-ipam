/**
 * 後端欄位檢查失敗（422）→ 看得懂的訊息（2026-10-12：客戶新增 ISOinsight 整合只看到「Invalid request」）。
 *
 * 後端的 RequestValidationError 處理回 `{detail: "Invalid request", errors: [...]}`，真正的原因在 errors
 * （pydantic 的 type／loc／msg／ctx），以前前端只顯示 detail，每個表單填錯都只有那一句。
 * 後端回應的格式不動（API 使用者靠它），在這裡翻成「欄位：原因」。
 */

type T = (key: string, params?: Record<string, unknown>) => string;
type TE = (key: string) => boolean;

interface ValidationItem {
  type?: string;
  loc?: unknown[];
  msg?: string;
  ctx?: Record<string, unknown>;
}

const LOC_ROOTS = new Set(["body", "query", "path", "header", "cookie"]);
const MAX_SHOWN = 3;

/** loc 裡最後一個欄位名稱（略過 body／query 與清單索引） */
export function fieldOf(loc: unknown[] | undefined): string {
  const names = (loc ?? []).filter((x): x is string => typeof x === "string" && !LOC_ROOTS.has(x));
  return names.length ? names[names.length - 1] : "";
}

function label(t: T, te: TE, name: string): string {
  return te(`fields.${name}`) ? t(`fields.${name}`) : name;
}

function reason(t: T, e: ValidationItem): string {
  const ctx = e.ctx ?? {};
  switch (e.type) {
    case "missing": return t("errors.v_missing");
    case "too_short": return t("errors.v_too_short_list", { n: ctx.min_length ?? 1 });
    case "too_long": return t("errors.v_too_long_list", { n: ctx.max_length ?? "" });
    case "string_too_short": return t("errors.v_string_too_short", { n: ctx.min_length ?? 1 });
    case "string_too_long": return t("errors.v_string_too_long", { n: ctx.max_length ?? "" });
    case "greater_than_equal": return t("errors.v_ge", { n: ctx.ge ?? "" });
    case "less_than_equal": return t("errors.v_le", { n: ctx.le ?? "" });
    case "greater_than": return t("errors.v_gt", { n: ctx.gt ?? "" });
    case "less_than": return t("errors.v_lt", { n: ctx.lt ?? "" });
    case "literal_error":
    case "enum": return t("errors.v_choice");
    case "int_parsing":
    case "int_type":
    case "int_from_float": return t("errors.v_integer");
    case "uuid_parsing":
    case "uuid_type":
    case "ip_v4_address":
    case "ip_any_address": return t("errors.v_format");
    // 自訂檢查的說明（例如 base_url 要用 http:// 或 https:// 開頭）：pydantic 前面會加「Value error, 」
    case "value_error": return String(e.msg ?? "").replace(/^Value error,\s*/i, "") || t("errors.v_invalid");
    default: return e.msg || t("errors.v_invalid");
  }
}

/** errors 清單 → 「欄位：原因；欄位：原因」，最多列 3 筆、其餘講還有幾筆。不是清單就回空字串。 */
export function formatValidationErrors(t: T, te: TE, errors: unknown[]): string {
  if (!Array.isArray(errors) || !errors.length) return "";
  const items = errors.filter((x): x is ValidationItem => !!x && typeof x === "object");
  if (!items.length) return "";
  const shown = items.slice(0, MAX_SHOWN).map((e) => {
    const name = fieldOf(e.loc);
    const why = reason(t, e);
    return name ? t("errors.v_field", { field: label(t, te, name), reason: why }) : why;
  });
  const more = items.length > MAX_SHOWN ? t("errors.v_more", { n: items.length - MAX_SHOWN }) : "";
  return [shown.join(t("errors.v_sep")), more].filter(Boolean).join(" ");
}
