/**
 * 機櫃一個 U 的橫向分格（GitHub issue #31）。
 *
 * 資料模型是「起始格 + 跨幾格」的區間，網格 **60 格 = 1~6 的最小公倍數**，
 * 所以 1/2、1/3、1/4、1/5、1/6 都表達得出來（1/5 是 issue #30 層架要的，12 格做不到）。
 *
 * 介面上不讓使用者面對 60 個格子，而是選「寬度 + 第幾格」，由這裡換算。
 * 必須與後端 `app/services/rack.py` 的 RACK_SLOTS 一致。
 */
export const RACK_SLOTS = 60;

/** 介面提供的寬度：1=整列、2=1/2、3=1/3、4=1/4、5=1/5、6=1/6。分母都要整除 RACK_SLOTS。 */
export const WIDTH_PARTS = [1, 2, 3, 4, 5, 6] as const;

/** 寬度 1/parts 佔幾格。 */
export function spanFor(parts: number): number {
  return RACK_SLOTS / parts;
}

/** 跨幾格 → 是幾分之一（用來把既有資料還原成介面上的選項）。 */
export function partsFor(span: number | null | undefined): number {
  const n = Number(span ?? RACK_SLOTS);
  const parts = RACK_SLOTS / (n || RACK_SLOTS);
  // 資料若是手動塞的非整齊跨度（模型允許），就近取一個能顯示的選項
  return (WIDTH_PARTS as readonly number[]).includes(parts) ? parts : 1;
}

/** 第 pos 格（1-based）的起始格。 */
export function slotFor(parts: number, pos: number): number {
  return (pos - 1) * spanFor(parts);
}

/** 起始格 → 第幾格（1-based）。 */
export function posFor(slot: number | null | undefined, parts: number): number {
  const s = Number(slot ?? 0);
  return Math.floor(s / spanFor(parts)) + 1;
}

/**
 * 各型態的預設外寬與每層高度（mm）。留白時後端就是用這組值，介面拿它當提示文字，
 * 使用者才知道「不填會變成多少」。
 *
 * 必須與後端 `app/services/rack.py` 的 _DEFAULT_WIDTH_MM / _DEFAULT_ROW_MM 一致
 * （backend/tests/test_rack_slots.py 有守門測試會比對這個檔）。
 */
export const RACK_DEFAULTS: Record<string, { width: number; row: number }> = {
  rack: { width: 483, row: 44 },
  industrial: { width: 600, row: 44 },
  shelf: { width: 900, row: 350 },
  wire_shelf: { width: 1200, row: 400 },
  // IKEA IVAR 最小的那組：層板 42×30 公分、側架高 179 公分（官方要求至少 4 層，
  // 實務上多半放 6 層 → 每層約 300mm）。
  wood_shelf: { width: 420, row: 300 },
};

/**
 * 各型態的層板厚度預設（mm）。層高填的是**淨空高**（不含板），所以板厚另計。
 * 木質層架 18mm 是 IKEA IVAR 層板的官方規格（1.8 cm）。
 * 必須與後端 `app/services/rack.py` 的 _DEFAULT_BOARD_MM 一致（有守門測試比對）。
 */
export const RACK_BOARD_MM: Record<string, number> = {
  rack: 0, industrial: 0, shelf: 20, wire_shelf: 35, wood_shelf: 18,
};
export function boardDefault(kind: string | null | undefined): number {
  return RACK_BOARD_MM[kind || "rack"] ?? 0;
}

/** 以「層」計的型態。多一種層架時只改這裡，標籤、表單、圖都會跟著對。 */
const LEVEL_KINDS = new Set(["shelf", "wire_shelf", "wood_shelf"]);

/** 型態的預設值（未知型態一律當標準機櫃）。 */
export function rackDefaults(kind: string | null | undefined) {
  return RACK_DEFAULTS[kind || "rack"] ?? RACK_DEFAULTS.rack;
}

/** 這個型態的列是「層」而不是「U」。標題、下拉選單與匯出都問這支，才不會某一處
 *  漏掉而出現「SHELF-01 (4U)」這種矛盾。必須與後端 rack.py 的 uses_rack_units 相反。 */
export function usesLevels(kind: string | null | undefined): boolean {
  return LEVEL_KINDS.has(kind || "");
}
