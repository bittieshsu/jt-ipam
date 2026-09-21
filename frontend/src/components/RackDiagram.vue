<script setup lang="ts">
/**
 * 機櫃 U 位視覺化 (phpIPAM 招牌功能)。
 *
 * 比 phpIPAM 改進：
 *  - 顏色按 device type 區分 (router/switch/firewall/server/...)
 *  - 越界 / 重疊衝突明顯標示
 *  - 點 device 跳詳細資料
 *  - U 編號從上到下標示，符合機房現場認知
 */
import { computed, ref, onMounted } from "vue";
import { RACK_SLOTS, usesLevels } from "@/utils/rackSlots";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { NCard, NEmpty, NAlert, NSpace, NTooltip, NButton, NButtonGroup, NIcon, NDropdown } from "naive-ui";
import type { RackDiagram } from "@/api/racks";
import { rackTypeColor as colorFor } from "@/utils/rackColors";
import { exportTable, type ExportColumn } from "@/utils/tableExport";
import { ExportIcon } from "@/icons";
import { getRackNameAlign, type RackNameAlign } from "@/api/basic";

// 全域設定：機櫃中裝置名稱靠左/置中/靠右（管理員在系統設定調整）
const nameAlign = ref<RackNameAlign>("left");
onMounted(() => { void getRackNameAlign().then((a) => { nameAlign.value = a; }); });
const nameJustify = computed(() =>
  nameAlign.value === "center" ? "center" : nameAlign.value === "right" ? "flex-end" : "flex-start");

const { t } = useI18n();

// 匯出 SVG 的幾何：寬與列高跟著機櫃走（issue #30 的層架非標準尺寸），其餘固定。
const GEO = computed(() => ({
  rowH: props.diagram?.render_row_px ?? 28,
  colW: props.diagram?.render_width_px ?? 250,
  gutter: 32, pad: 12, headerH: 30,
}));
const esc = (s: unknown) => String(s ?? "").replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c] as string));

function devLabel(dev: any): string {
  // 機櫃示意圖（含匯出）只標裝置名稱，與畫面一致；不加類型 / IP
  return dev.name;
}

const pct = (n: number) => `${(n / RACK_SLOTS) * 100}%`;
// 機架型態（issue #30）。層架類的列叫「層」不叫 U，外觀也各自不同。
const rackKind = computed(() => (props.diagram as any)?.kind || "rack");
const isShelf = computed(() => usesLevels(rackKind.value));
const isWire = computed(() => rackKind.value === "wire_shelf");
const isIndustrial = computed(() => rackKind.value === "industrial");
const isWood = computed(() => rackKind.value === "wood_shelf");
/**
 * 木質層架（IKEA IVAR）背面的 OBSERVATÖR 支撐桿：**一根**斜桿，不是 X。
 * 它是固定 100 公分的鋼條，所以跨幾層由層架寬度決定 —— 跨距由後端算好
 * （`brace_levels`），前端只負責畫在最底下那一段。
 */
const braceLevels = computed(() => (props.diagram as any)?.brace_levels ?? 0);
const braceStyle = computed(() => {
  const n = props.diagram?.u_height ?? 0;
  const lv = Math.min(braceLevels.value, n);
  if (!isWood.value || lv <= 0) return null;
  // 支撐桿在最底下那幾層。畫面由上往下依 cells 的順序排，所以直接把 cells 的高度
  // 加總 —— 各層高度可能不同，不能用「層數 × 列高」。
  const px = cells.value.map((c) => rowPxOf(c.u));
  const bottom = props.diagram?.numbering === "bottom-up";
  // bottom-up 時第 1 層畫在最上面，所以「最底下那幾層」在畫面上是最前面幾格
  const span = bottom ? px.slice(0, lv) : px.slice(px.length - lv);
  const before = bottom ? 0 : px.slice(0, px.length - lv).reduce((a, b) => a + b, 0);
  return {
    top: before + "px",
    height: span.reduce((a, b) => a + b, 0) + "px",
  };
});
/** 「16U」或「4 層」—— 標題與匯出都叫這支，兩邊不會各寫各的。 */
function rowsText(n: number, kind?: string | null): string {
  return usesLevels(kind) ? t("racks.rows_levels", { n }) : `${n}U`;
}
const rowsLabel = computed(() => rowsText(props.diagram?.u_height ?? 0, rackKind.value));

/**
 * 畫出來的列高與寬度。**只有這裡算**：以前是行內 style 設一個值、`.rd-compact` 的 CSS
 * 再設另一個值，而行內贏 CSS —— 結果縮圖的格子被 CSS 壓成 18px、左邊的層號卻吃行內的
 * 160px，號碼就掉到整頁下面去了。縮圖要變小就在這裡變，不要兩邊各設一次。
 */
/**
 * 每一層的高度 px，**由第 1 層起算**（不是畫面由上往下）。層架的層板一層一層可調，
 * 所以這是一個陣列；後端沒給（舊版）就用均一值補滿，畫出來與以前一樣。
 */
const rowPxList = computed<number[]>(() => {
  const n = props.diagram?.u_height ?? 0;
  const base = props.diagram?.render_row_px ?? 28;
  const raw = ((props.diagram as any)?.render_row_px_list ?? []) as number[];
  const list = raw.slice(0, n).map((v) => Number(v) || base);
  while (list.length < n) list.push(base);
  if (!props.compact) return list;
  // 縮圖：等比縮小，保留各層的相對比例（直接夾住會把高低層壓成一樣高）
  const cap = usesLevels(rackKind.value) ? 36 : 18;
  const k = Math.min(1, cap / Math.max(...list, 1));
  return list.map((v) => Math.max(12, v * k));
});
/** 層板畫出來多厚 px。層高填的是淨空高，板厚另外占位置。 */
const boardPx = computed(() => Number((props.diagram as any)?.render_board_px ?? 0) || 0);
/** 層架的最上面那片板**上面**也放得了東西 → 多一列可放的位置。 */
const openTop = computed(() => Boolean((props.diagram as any)?.open_top));
/** 第 u 層的淨空高度 px（u 是層號，1 起算）。超出的是「頂板上方」，比照最高層。 */
const rowPxOf = (u: number) =>
  rowPxList.value[u - 1] ?? rowPxList.value[rowPxList.value.length - 1] ?? 28;
/** 給 CSS 當退路用的代表值（層高均一時就是它）。 */
const rowPx = computed(() => rowPxList.value[0] ?? 28);
/**
 * 每一層邊界在畫面上的 y（含最上與最下）。鍍鉻層架的套環要裝在每一片層板的位置，
 * 層高可以一層一層不同，所以不能用「固定間距的重複漸層」去畫。
 */
/**
 * 立柱／側架的起點（距離框頂多少 px）。立柱只到**最上面那片層板**為止 —— 頂板上面是
 * 開放的，東西就放在那裡，柱子不會再往上長。沒有開放頂端時就從最上緣起算。
 */
const postTop = computed(() => (openTop.value && cells.value.length ? cells.value[0].px : 0));
const boundaryTops = computed<number[]>(() => {
  const out = [-3];
  let acc = 0;
  for (const c of cells.value) { acc += c.px; out.push(acc - 3); }
  return out;
});
const colPx = computed(() => {
  const base = props.diagram?.render_width_px ?? 250;
  return props.compact ? Math.min(base, 300) : base;
});

// 共用：產生機櫃 SVG 字串 + 尺寸
// 依橫向格位算寬度，否則同一個 U 的多台裝置會疊在一起
function partGeom(dev: any): { x: number; w: number; cx: number; half: boolean } {
  const { colW, gutter } = GEO.value;
  const slot = Number(dev.rack_slot ?? 0);
  const span = Number(dev.rack_slot_span ?? RACK_SLOTS);
  const cell = colW / RACK_SLOTS;
  const x = gutter + 2 + slot * cell;
  const w = Math.max(span * cell - 4, 2);
  return { x, w, cx: x + w / 2, half: span < RACK_SLOTS };
}

function buildSvg(): { svg: string; W: number; H: number } | null {
  const d = props.diagram;
  if (!d) return null;
  const { rowH, colW, gutter, pad, headerH } = GEO.value;
  const U = d.u_height || 0;
  const W = gutter + colW + pad * 2;
  const H = headerH + U * rowH + pad * 2;
  const p: string[] = [];
  p.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" font-family="sans-serif">`);
  p.push(`<rect x="0" y="0" width="${W}" height="${H}" fill="#ffffff"/>`);
  p.push(`<text x="${pad}" y="${pad + 16}" font-size="14" font-weight="bold">Rack: ${esc(d.name)} (${rowsText(U, d.kind)})</text>`);
  const top = headerH + pad;
  p.push(`<rect x="${gutter}" y="${top}" width="${colW}" height="${U * rowH}" fill="#f5f5f5" stroke="#888" stroke-width="1.5"/>`);
  for (let i = 0; i < U; i++) {
    const uNum = U - i;
    const y = top + i * rowH;
    p.push(`<text x="${gutter - 4}" y="${y + rowH / 2 + 4}" font-size="10" text-anchor="end" fill="#666">${uNum}</text>`);
    p.push(`<line x1="${gutter}" y1="${y}" x2="${gutter + colW}" y2="${y}" stroke="#dddddd" stroke-width="0.5"/>`);
  }
  for (const dev of (d.devices || [])) {
    if (!dev.u_position || !dev.u_size) continue;
    const uTop = dev.u_position + dev.u_size - 1;
    const yTop = top + (U - uTop) * rowH;
    const hgt = dev.u_size * rowH;
    const g = partGeom(dev);
    p.push(`<rect x="${g.x}" y="${yTop + 1}" width="${g.w}" height="${hgt - 2}" rx="3" fill="${colorFor(dev.type)}" stroke="rgba(0,0,0,0.3)"/>`);
    const a = nameAlign.value;
    // 半 U 太窄，一律置中；全寬才依名稱對齊偏好
    const tx = g.half ? g.cx : a === "center" ? gutter + colW / 2 : a === "right" ? gutter + colW - 10 : gutter + 10;
    const anchor = g.half ? "middle" : a === "center" ? "middle" : a === "right" ? "end" : "start";
    p.push(`<text x="${tx}" y="${yTop + hgt / 2 + 4}" text-anchor="${anchor}" font-size="11" font-weight="bold" fill="#ffffff">${esc(devLabel(dev))}</text>`);
    // 安裝於機櫃後側 → 右上角標一個 R 角標（前側為預設，不標）
    if (dev.rack_face === "rear") {
      const rx = g.x + g.w;
      p.push(`<path d="M${rx - 14} ${yTop + 1} L${rx} ${yTop + 1} L${rx} ${yTop + 15} Z" fill="rgba(0,0,0,0.55)"/>`);
      p.push(`<text x="${rx - 2}" y="${yTop + 11}" text-anchor="end" font-size="9" font-weight="bold" fill="#ffffff">R</text>`);
    }
  }
  p.push(`</svg>`);
  return { svg: p.join("\n"), W, H };
}

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function exportSvg() {
  const r = buildSvg();
  if (!r) return;
  download(new Blob([r.svg], { type: "image/svg+xml" }), `rack-${props.diagram!.name}.svg`);
}

// SVG → canvas → PNG（2x 解析度）
function exportPng() {
  const r = buildSvg();
  if (!r) return;
  const scale = 2;
  const img = new Image();
  const svgUrl = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(r.svg)));
  img.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = r.W * scale; canvas.height = r.H * scale;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0);
    canvas.toBlob((blob) => { if (blob) download(blob, `rack-${props.diagram!.name}.png`); }, "image/png");
  };
  img.src = svgUrl;
}

// 匯出為 draw.io（.drawio）：mxGraphModel，機櫃框 + 每台裝置一個可編輯方塊
function exportDrawio() {
  const d = props.diagram;
  if (!d) return;
  const { rowH, colW, gutter, pad, headerH } = GEO.value;
  const U = d.u_height || 0;
  const top = headerH + pad;
  const cells: string[] = [];
  cells.push('<mxCell id="0"/>');
  cells.push('<mxCell id="1" parent="0"/>');
  // 標題（放外框上方，不與最上層裝置重疊）
  cells.push(`<mxCell id="title" value="${esc(`Rack: ${d.name} (${rowsText(U, d.kind)})`)}" style="text;html=1;align=left;verticalAlign=middle;fontStyle=1;fontSize=14;" vertex="1" parent="1"><mxGeometry x="${gutter}" y="${pad}" width="${colW}" height="20" as="geometry"/></mxCell>`);
  // 機櫃外框：明確較粗框線（strokeWidth=2，對應示意圖的外框粗細）
  cells.push(`<mxCell id="rack" value="" style="rounded=0;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#888888;strokeWidth=2;" vertex="1" parent="1"><mxGeometry x="${gutter}" y="${top}" width="${colW}" height="${U * rowH}" as="geometry"/></mxCell>`);
  // 左側 U 數編號（與示意圖一致）
  for (let i = 0; i < U; i++) {
    const uNum = U - i;
    const y = top + i * rowH;
    cells.push(`<mxCell id="u${uNum}" value="${uNum}" style="text;html=1;align=right;verticalAlign=middle;fontSize=10;fontColor=#666666;" vertex="1" parent="1"><mxGeometry x="${gutter - 28}" y="${y}" width="24" height="${rowH}" as="geometry"/></mxCell>`);
  }
  let n = 0;
  for (const dev of (d.devices || [])) {
    if (!dev.u_position || !dev.u_size) continue;
    const uTop = dev.u_position + dev.u_size - 1;
    const yTop = top + (U - uTop) * rowH;
    const hgt = dev.u_size * rowH;
    const fill = colorFor(dev.type);
    const g = partGeom(dev);
    const align = g.half ? "center" : nameAlign.value;
    cells.push(`<mxCell id="dev${n++}" value="${esc(devLabel(dev))}" style="rounded=0;whiteSpace=wrap;html=1;fillColor=${fill};strokeColor=#000000;fontColor=#ffffff;fontStyle=1;align=${align};spacingLeft=6;spacingRight=6;" vertex="1" parent="1"><mxGeometry x="${g.x}" y="${yTop + 1}" width="${g.w}" height="${hgt - 2}" as="geometry"/></mxCell>`);
  }
  const xml =
    `<mxfile host="jt-ipam"><diagram name="${esc(d.name)}">` +
    `<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" math="0" shadow="0">` +
    `<root>${cells.join("")}</root></mxGraphModel></diagram></mxfile>`;
  download(new Blob([xml], { type: "application/xml" }), `rack-${d.name}.drawio`);
}

const exportOptions = computed(() => [
  { label: "SVG", key: "svg" },
  { label: "PNG", key: "png" },
  { label: "draw.io", key: "drawio" },
  { type: "divider", key: "d1" },
  { label: "CSV", key: "csv" },
  { label: "Excel (.xlsx)", key: "xlsx" },
  { label: "OpenDocument (.ods)", key: "ods" },
  { label: "Markdown (.md)", key: "md" },
  { label: t("export.fmt_txt"), key: "txt" },
]);
// 機櫃裝置清單的資料匯出（csv/xlsx/ods/md/txt）
function exportData(fmt: "csv" | "xlsx" | "ods" | "md" | "txt") {
  const d = props.diagram;
  if (!d) return;
  const cols: ExportColumn[] = [
    { key: "u_position", label: "U" },
    { key: "u_size", label: "U Size" },
    { key: "name", label: t("cols.name") },
    { key: "type", label: t("cols.type") },
    { key: "rack_face", label: t("racks.face") },
    { key: "primary_ip", label: "IP" },
    { key: "vendor", label: t("cols.vendor") },
    { key: "model", label: t("cols.model") },
  ];
  const rows = [...d.devices].sort((a, b) => (b.u_position ?? 0) - (a.u_position ?? 0));
  exportTable(fmt, `rack-${d.name}`, cols, rows as any, `Rack ${d.name}`);
}
function onExport(key: string) {
  if (key === "svg") exportSvg();
  else if (key === "png") exportPng();
  else if (key === "drawio") exportDrawio();
  else if (["csv", "xlsx", "ods", "md", "txt"].includes(key)) exportData(key as any);
}
const router = useRouter();
function goDevice(id: string) {
  router.push({ name: "device-detail", params: { id } });
}

interface Props {
  diagram: RackDiagram | null;
  showLegend?: boolean;   // 多機櫃並排時可關掉，由頁面放一個共用圖例
  editable?: boolean;     // admin：點空 U 位可挑裝置放入
  floorAlignTo?: number;  // 多機櫃並排時傳入該排最高 U 數 → 矮櫃頂端補空白，使底部(U1)靠下對齊
  highlightId?: string | null;  // 常駐高亮某裝置（裝置詳細資料頁標示本機在機櫃的位置）
  compact?: boolean;            // 較小列高（嵌在裝置詳細資料等空間有限處）
  bare?: boolean;               // 去掉卡片外框與標題（嵌入用）
  face?: "front" | "rear" | null;  // 外部強制指定檢視面（合併卡共用切換用）；null = 用自身切換
  controls?: boolean;              // 是否顯示自身的面切換 + 匯出（合併卡傳 false 改由外層統一）
}
const props = withDefaults(defineProps<Props>(), { showLegend: true, editable: false, floorAlignTo: 0, highlightId: null, compact: false, bare: false, face: null, controls: true });
const faceView = ref<"front" | "rear">("front");   // 機櫃正面 / 背面切換
// 實際採用的檢視面：外部有指定就用外部（合併卡共用），否則用自身切換
const effFace = computed(() => props.face ?? faceView.value);
const hasRear = computed(() => (props.diagram?.devices || []).some((d: any) => d.rack_face === "rear"));
// 落地對齊：比該排最高櫃矮幾 U，就在頂端補幾 U 的空白。
// 用這台自己的列高換算 —— 以前寫死 28px，非標準尺寸的機櫃／層架會對不齊。
const floorPad = computed(() => {
  const u = props.diagram?.u_height ?? 0;
  return props.floorAlignTo > u ? (props.floorAlignTo - u) * rowPx.value : 0;
});
const emit = defineEmits<{ (e: "pick-empty", u: number, rackId: string, slot?: number): void }>();
const hoveredId = ref<string | null>(null);   // hover 某 U → 整台裝置點亮+框線

interface DevPart {
  id: string;
  name: string;
  type: string;
  vendor: string | null;
  model: string | null;
  u_size: number;
  /** 這台在畫面上占幾格（用來讓名稱跨整台置中） */
  run: number;
  /** 這台跨過那幾格的**高度總和** px。層高可以一層一層不同，所以不能用 run × 列高。 */
  runPx: number;
  /** 層內的垂直位置（0 貼著層板，往上長）與佔幾格 —— 層架一層可以疊放、也可不放滿 */
  vslot: number;
  vspan: number;
  slot: number;        // 起始格（0..RACK_SLOTS-1）
  span: number;        // 跨幾格
  is_top: boolean;     // device 最上格
  is_bottom: boolean;  // device 最下格
  is_mid: boolean;     // device 垂直中間格（顯示名字 → 跨多 U 置中）
  primary_ip: string | null;
}
interface Cell {
  u: number;                       // 1-based, top-most U
  px: number;                      // 這一層畫出來多高（淨空高 + 層板厚度）
  isTop: boolean;                  // 是不是「頂板上方」那一列（層架才有）
  parts: DevPart[];                // 這個 U 上的裝置（依起始格排序）
  gaps: { slot: number; span: number }[];   // 沒被佔到的空隙（可點來新增）
}

// 衝突訊息：整理成好讀的繁中句子（不直接吐 JSON）
const conflictLines = computed<string[]>(() => {
  const d = props.diagram;
  if (!d) return [];
  const nameOf = new Map(d.devices.map((s) => [String(s.device_id), s.name]));
  return d.conflicts.map((c: any) => {
    if (c.type === "overlap") {
      const names = (c.device_ids ?? [])
        .map((id: string) => nameOf.get(String(id)) ?? String(id).slice(0, 8)).join("、");
      return t("rack_diagram.conflict_overlap", { u: c.u, names });
    }
    if (c.type === "out_of_bounds") {
      return t("rack_diagram.conflict_oob",
        { name: c.name, u: c.u_position, size: c.u_size, h: c.rack_u_height });
    }
    if (c.type === "unpositioned") {
      return t("rack_diagram.conflict_unpos", { name: c.name });
    }
    return JSON.stringify(c);
  });
});

const cells = computed<Cell[]>(() => {
  if (!props.diagram) return [];
  // 層架多一列：最上面那片板的**上面**（第 u_height + 1 層），標「頂」不標數字
  const u_height = props.diagram.u_height + (openTop.value ? 1 : 0);
  const real = props.diagram.u_height;
  const bottomUp = props.diagram.numbering === "bottom-up";
  const map: Record<number, Cell> = {};
  for (let u = 1; u <= u_height; u++)
    map[u] = { u, px: rowPxOf(u) + boardPx.value, isTop: u > real, parts: [], gaps: [] };
  const mk = (d: any): DevPart => ({
    id: d.device_id, name: d.name, type: d.type, vendor: d.vendor, model: d.model,
    u_size: d.u_size, is_top: false, is_bottom: false, is_mid: false, run: 1, runPx: 0,
    slot: Number(d.rack_slot ?? 0), span: Number(d.rack_slot_span ?? RACK_SLOTS),
  vslot: Number(d.rack_vslot ?? 0), vspan: Number(d.rack_vslot_span ?? RACK_SLOTS),
    primary_ip: d.primary_ip,
  });
  for (const d of props.diagram.devices) {
    // 只顯示目前檢視面（正/背）的裝置；未標面者視為正面
    if (((d.rack_face ?? "front") as string) !== effFace.value) continue;
    for (let u = d.u_position; u < d.u_position + d.u_size; u++) {
      if (!map[u]) continue;
      map[u].parts.push(mk(d));
    }
  }
  // 顯示順序：top-down → 高 U 在上（u_height..1）；bottom-up → U1 在上（1..u_height）
  const order: Cell[] = bottomUp
    ? Array.from({ length: u_height }, (_, i) => map[i + 1])
    : Array.from({ length: u_height }, (_, i) => map[u_height - i]);
  // 「頂板上方」在實體上永遠是**最上面**那一列，跟編號方向無關。bottom-up 時編號由上
  // 往下遞增，照編號排會把它排到最底下 —— 那是地板不是頂板，所以要另外提到最前面。
  if (openTop.value && bottomUp) {
    const i = order.findIndex((c) => c.isTop);
    if (i > 0) order.unshift(...order.splice(i, 1));
  }

  for (const c of order) {
    c.parts.sort((a, b) => a.slot - b.slot);
    // 沒被任何裝置佔到的橫向空隙 → 可點擊新增。逐格掃過去再併成連續區段，
    // 比「左半/右半」那套通用，重疊的資料也不會讓它算爆。
    const taken = new Array(RACK_SLOTS).fill(false);
    for (const p of c.parts)
      for (let i = p.slot; i < Math.min(p.slot + p.span, RACK_SLOTS); i++) taken[i] = true;
    let i = 0;
    while (i < RACK_SLOTS) {
      if (taken[i]) { i++; continue; }
      const from = i;
      while (i < RACK_SLOTS && !taken[i]) i++;
      c.gaps.push({ slot: from, span: i - from });
    }
  }

  // 垂直連續段（跨多 U 的同一台）：**依裝置 id** 判斷，與它佔哪些格無關。
  const runs: Record<string, number[]> = {};
  order.forEach((c, i) => {
    for (const p of c.parts) {
      const prev = order[i - 1]?.parts.some((x) => x.id === p.id);
      const next = order[i + 1]?.parts.some((x) => x.id === p.id);
      p.is_top = !prev;
      p.is_bottom = !next;
      (runs[p.id] ??= []).push(i);
    }
  });
  for (const [id, idxs] of Object.entries(runs)) {
    // 名稱畫在**最上面**那一格，再用絕對定位跨滿整台的高度置中。
    //
    // 原本是挑「中間那一格」來畫（`Math.floor((len-1)/2)`），但偶數 U 沒有正中間的
    // 一格：2U 會取到上面那格，名稱因此偏高半格。跨整台置中就與 U 數的奇偶無關。
    const head = order[idxs[0]].parts.find((x) => x.id === id);
    if (head) head.is_mid = true;
    const runPx = idxs.reduce((a, i) => a + order[i].px, 0);
    for (const i of idxs)
      for (const p of order[i].parts) if (p.id === id) { p.run = idxs.length; p.runPx = runPx; }
  }
  return order;
});
</script>

<template>
  <n-card v-if="diagram" class="rack-diagram-card" :class="{ 'rd-compact': compact, 'rd-bare': bare }"
          :bordered="!bare" :title="bare ? undefined : `${t('nav.racks')}: ${diagram.name} (${rowsLabel})`">
    <n-space vertical :size="12">
      <!-- 控制列：自標題列搬到內文最上方。
           用 flex 而不是 n-space：n-space 的每個項目是 block 包 inline-flex，靠 baseline
           對齊，而兩顆按鈕的 line-height 不同（22.4px vs 12px），就會差個 1~2px 對不齊。 -->
      <div v-if="controls" class="rd-toolbar">
        <n-button-group size="tiny">
          <n-button :type="faceView === 'front' ? 'primary' : 'default'" @click="faceView = 'front'">
            {{ t("racks.face_front") }}
          </n-button>
          <n-button :type="faceView === 'rear' ? 'primary' : 'default'" @click="faceView = 'rear'">
            {{ t("racks.face_rear") }}<span v-if="hasRear" style="margin-left:3px">•</span>
          </n-button>
        </n-button-group>
        <n-dropdown trigger="click" :options="exportOptions" @select="onExport">
          <n-button size="tiny" :title="t('rack_diagram.export_svg_hint')">
            <template #icon><n-icon><ExportIcon /></n-icon></template>
            {{ t("common.export") }}
          </n-button>
        </n-dropdown>
      </div>
      <n-alert
        v-if="diagram.conflicts.length > 0"
        type="warning"
        :title="t('rack_diagram.conflict_title', { n: diagram.conflicts.length })"
      >
        <ul class="conflict-list">
          <li v-for="(line, i) in conflictLines" :key="i">{{ line }}</li>
        </ul>
      </n-alert>

      <!-- 只要機櫃有設定 U 數，即使沒有任何 device 也畫出空機櫃框 -->
      <n-empty
        v-if="!diagram.u_height"
        :description="t('rack_diagram.empty')"
      />

      <div v-else class="rack-wrap" :style="floorPad ? { marginTop: floorPad + 'px' } : undefined">
        <!-- U 編號：機櫃框外左側 gutter -->
        <div class="u-gutter">
          <div v-for="cell in cells" :key="'g' + cell.u" class="u-num-out"
               :style="{ height: cell.px + 'px' }">{{ cell.isTop ? t("racks.level_top") : cell.u }}</div>
        </div>
        <div class="rack-frame"
             :class="{ 'is-shelf': isShelf, 'is-wire': isWire, 'is-industrial': isIndustrial, 'is-wood': isWood }"
             :style="{ '--rd-col-w': colPx + 'px', '--rd-row-h': rowPx + 'px',
                       '--rd-board': boardPx + 'px', '--rd-post-top': postTop + 'px' }">
          <!-- 頂板：CSS 是用每一列的 border-bottom 畫層板，最上面那片畫不出來 ——
               層架頂端幾乎一定有一片板，少了就像少一層（SVG 那邊是多畫一片解決的）。 -->
          <!-- 開放頂端時**不畫**這片：最上面那一列就是頂板的上面，那裡沒有板。
               11 列的 border-bottom 剛好給出 11 片板（10 層 + 頂板）。 -->
          <div v-if="isShelf && !openTop" class="shelf-top" aria-hidden="true" />
          <!-- 鍍鉻層架的套環：每一片層板的位置各一個（層高逐層可調，不能用固定間距畫） -->
          <i v-for="(y, i) in (isWire ? boundaryTops : [])" :key="'c' + i"
             class="wire-collar" :style="{ top: y + 'px' }" aria-hidden="true" />
          <!-- 背面的 OBSERVATÖR 支撐桿。放在最前面＝畫在裝置後面，空層才看得到。 -->
          <div v-if="braceStyle" class="wood-brace" :style="braceStyle" aria-hidden="true" />
          <template v-for="cell in cells" :key="cell.u">
            <!-- 一個 U = 12 格的橫向網格；裝置與空隙都用百分比絕對定位，
                 所以整 U / 1/2 / 1/3 / 1/4 / 1/6 走的是同一條渲染路徑（issue #31）。 -->
            <div class="u-row u-slots" :style="{ height: cell.px + 'px' }">
              <n-tooltip v-for="p in cell.parts" :key="p.id + '@' + p.slot"
                         trigger="hover" :delay="60" placement="right">
                <template #trigger>
                  <div
                    class="u-part u-occupied"
                    :class="{ 'u-top': p.is_top, 'u-bottom': p.is_bottom, 'u-hl': hoveredId === p.id || highlightId === p.id, 'u-dim': !!highlightId && highlightId !== p.id }"
                    :style="{ background: colorFor(p.type), left: pct(p.slot), width: pct(p.span),
                              bottom: pct(p.vslot), height: pct(p.vspan), top: 'auto',
                              justifyContent: p.span >= 12 ? nameJustify : 'center' }"
                    @mouseenter="hoveredId = p.id"
                    @mouseleave="hoveredId = null"
                    @click="goDevice(p.id)"
                  >
                    <span v-if="p.is_mid" class="d-name-span"
                          :class="{ 'd-name-span-half': p.span < 12 }"
                          :style="{ height: (p.vslot || p.vspan < RACK_SLOTS)
                                              ? '100%' : p.runPx + 'px' }">
                      <span class="d-name" :class="{ 'd-name-half': p.span < 12 }">{{ p.name }}</span>
                    </span>
                  </div>
                </template>
                <div class="rack-tip">
                  <div class="rt-name">{{ p.name }}</div>
                  <div class="rt-row"><span>{{ t("cols.type") }}</span><b>{{ p.type }}</b></div>
                  <div v-if="p.vendor" class="rt-row"><span>{{ t("cols.vendor") }}</span><b>{{ p.vendor }}</b></div>
                  <div v-if="p.model" class="rt-row"><span>{{ t("cols.model") }}</span><b>{{ p.model }}</b></div>
                  <div v-if="p.primary_ip" class="rt-row"><span>IP</span><b>{{ p.primary_ip }}</b></div>
                  <div class="rt-row"><span>{{ t("rack_diagram.height") }}</span><b>{{ p.u_size }}U</b></div>
                  <div v-if="p.span < 12" class="rt-row">
                    <span>{{ t("rack_diagram.width") }}</span><b>{{ p.span }}/12</b>
                  </div>
                </div>
              </n-tooltip>

              <!-- 空隙：點了就帶著「哪一個 U、哪一格」去新增 -->
              <div v-for="g in cell.gaps" :key="'gap' + g.slot"
                   class="u-part u-gap" :class="{ 'u-pickable': editable }"
                   :style="{ left: pct(g.slot), width: pct(g.span) }"
                   :title="editable ? t('racks.pick_device_here') : `Empty (U${cell.u})`"
                   @click="editable && props.diagram && emit('pick-empty', cell.u, props.diagram.rack_id, g.slot)">
                <span v-if="editable && g.span >= 3" class="u-plus">＋</span>
              </div>
            </div>
          </template>
        </div>
      </div>

      <div v-if="showLegend" class="legend">
        <span class="legend-item" :style="{ background: colorFor('router') }">router</span>
        <span class="legend-item" :style="{ background: colorFor('switch') }">switch</span>
        <span class="legend-item" :style="{ background: colorFor('firewall') }">firewall</span>
        <span class="legend-item" :style="{ background: colorFor('server') }">server</span>
        <span class="legend-item" :style="{ background: colorFor('storage') }">storage</span>
        <span class="legend-item" :style="{ background: colorFor('ap') }">ap</span>
        <span class="legend-item" :style="{ background: colorFor('ipmi') }">ipmi</span>
        <span class="legend-item" :style="{ background: colorFor('patch_panel') }">patch panel</span>
        <span class="legend-item" :style="{ background: colorFor('pdu') }">pdu</span>
        <span class="legend-item" :style="{ background: colorFor('ups') }">ups</span>
        <span class="legend-note">{{ t("racks.rear_legend") }}</span>
      </div>
    </n-space>
  </n-card>
</template>

<style scoped>
/* 衝突清單：繁中可讀句子（取代原本的 JSON dump） */
.conflict-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.7; }

/* 多機櫃並排落地對齊：矮櫃由 floorPad（inline margin-top）在頂端補空白，使各櫃底部(U1)
   對齊同一條地板線；補白後各櫃內容等高，卡片自然等高。 */
.rack-wrap { display: flex; align-items: flex-start; gap: 6px; }
/* 左側 U 編號 gutter：頂端內距 = 機櫃框 border(2)+padding(4) = 6px，讓每個編號與
   右側對應 U 列等高(28px)且垂直置中對齊。 */
.u-gutter { display: flex; flex-direction: column; padding-top: 6px; flex: 0 0 auto; }
.u-num-out {
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  width: 26px;
  padding-right: 6px;
  font: bold 12px ui-monospace, SFMono-Regular, Menlo, monospace;
  color: rgba(127, 127, 127, 0.75);
}
.rack-frame {
  /* 一格 U 的高度。名稱要跨整台置中，得知道一格多高 —— 所以放成變數，
     compact 模式只要改這一個值。 */
  --rd-row-h: 28px;
  /* 裝置外框的線寬（見 .u-occupied）。名稱置中要用它把邊框的厚度補回去。 */
  --rd-border: 2px;
  border: 2px solid rgba(127, 127, 127, 0.5);
  border-radius: 4px;
  padding: 4px;
  width: var(--rd-col-w, 250px);
  background: rgba(127, 127, 127, 0.04);
}
/* 一般層架（issue #30）：沒有機櫃導軌，畫成層板 —— 每一層下緣一條實線，兩側不封邊。 */
.rack-frame.is-shelf {
  border-left: none; border-right: none; border-radius: 0;
  background: transparent;
}
.rack-frame.is-shelf .u-row { border-bottom: var(--rd-board, 2px) solid rgba(127, 127, 127, 0.55); }

/* 工業機櫃：箱體，比標準機櫃厚重 —— 粗外框 + 深色底。 */
.rack-frame.is-industrial {
  border: 4px solid rgba(90, 95, 105, 0.85);
  border-radius: 3px;
  background: rgba(90, 95, 105, 0.10);
}

/* 鍍鉻層架：兩側圓管立柱 + 網狀層板。立柱疊三層背景 ——
   (1) 每層層板位置的套環、(2) 整根立柱的細溝槽環、(3) 圓柱高光漸層。
   套環與溝槽是鍍鉻層架最好認的特徵：沒有它們，立柱只會像兩根灰色長條。
   套環的間距直接綁 --rd-row-h，所以層高改變時會自己對齊到每一層。 */
.rack-frame.is-wire {
  position: relative;
  border: none; border-radius: 0; background: transparent;
  padding-left: 14px; padding-right: 14px;
}
.rack-frame.is-wire::before,
.rack-frame.is-wire::after {
  content: ""; position: absolute; width: 13px;
  top: calc(var(--rd-post-top, 0px) - 7px); bottom: -7px;
  border-radius: 7px;
  background:
    /* 整根的細溝槽環。套環改用 .wire-collar 元素畫在每片層板的位置 ——
       層高可以一層一層不同，固定間距的重複漸層會對不準。 */
    repeating-linear-gradient(180deg,
      rgba(70, 76, 82, 0.20) 0 1px, transparent 1px 5px),
    /* (3) 圓柱高光 */
    linear-gradient(90deg,
      #5f646a 0%, #90969c 16%, #e9edf0 36%, #ffffff 47%,
      #cfd4d9 60%, #969ca2 80%, #55595e 100%);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.22), 0 1px 3px rgba(0, 0, 0, 0.18);
}
.rack-frame.is-wire::before { left: 0; }
.rack-frame.is-wire::after { right: 0; }
/* 套環：裝在每一片層板的位置，左右立柱各一個 */
.wire-collar {
  position: absolute; left: 0; right: 0; height: 6px; pointer-events: none;
}
.wire-collar::before,
.wire-collar::after {
  content: ""; position: absolute; top: 0; width: 13px; height: 6px;
  background: linear-gradient(90deg,
    #5f646a 0%, #90969c 16%, #e9edf0 36%, #ffffff 47%,
    #cfd4d9 60%, #969ca2 80%, #55595e 100%);
  box-shadow: 0 0 0 0.8px rgba(60, 66, 72, 0.5);
}
.wire-collar::before { left: 0; }
.wire-collar::after { right: 0; }

/* 網狀層板：前緣鍍鉻橫桿 + 十字網格（空層才看得到網格，有裝置時會被蓋住）。 */
.rack-frame.is-wire .u-row {
  /* 層板厚度來自機櫃設定（層高填的是淨空高，板厚另計） */
  border-bottom: var(--rd-board, 6px) solid transparent;
  border-image: linear-gradient(180deg,
    #ffffff 0%, #dfe4e8 25%, #a8aeb4 70%, #71767b 100%) 1;
  background-image:
    repeating-linear-gradient(90deg, rgba(127,127,127,0.22) 0 1px, transparent 1px 9px),
    repeating-linear-gradient(0deg, rgba(127,127,127,0.13) 0 1px, transparent 1px 9px);
}
/* 木質層架（IKEA IVAR）：松木側架 + 松木層板 + 背面的鋼製交叉支撐桿。
   側架整根畫在層板外側（實物就是層板架在兩片側架之間），上面那排調整孔是最好認的地方。 */
.rack-frame.is-wood {
  position: relative;
  border: none; border-radius: 0; background: transparent;
  padding-left: 15px; padding-right: 15px;
}
.rack-frame.is-wood::before,
.rack-frame.is-wood::after {
  /* 方柱：沒有圓角、也沒有圓管那種中央高光，只有一面受光一面暗 */
  content: ""; position: absolute; width: 14px;
  top: calc(var(--rd-post-top, 0px) - 8px); bottom: -8px;
  background:
    /* 調整孔：一排**圓孔**。用橫向色帶畫會變成一圈一圈的條紋 —— 那是圓管的樣子，
       IVAR 的側架是平板 + 圓孔，所以要用 radial-gradient。 */
    /* 孔距 32mm、孔徑 7mm（IVAR 實物規格），照圖面比例尺 28px/44.45mm 換算
       → 間距 20.16px、直徑 4.41px。與後端 rack_svg.py 的 PEG_PITCH_MM 一致。 */
    radial-gradient(circle at 50% 50%,
      rgba(74, 48, 24, 0.5) 0 2.2px, rgba(255, 245, 230, 0.35) 2.2px 2.8px,
      transparent 2.9px) 0 0 / 14px 20.16px,
    /* 直的木紋（不是橫紋） */
    repeating-linear-gradient(90deg,
      rgba(120, 80, 40, 0.08) 0 1px, transparent 1px 7px),
    /* 平板：幾乎同色，只有右緣一條暗邊當厚度 */
    linear-gradient(90deg, #dcb98d 0%, #d4ae7f 80%, #b38a58 100%);
  box-shadow: 0 0 0 1px rgba(90, 60, 30, 0.4);
}
.rack-frame.is-wood::before { left: 0; }
.rack-frame.is-wood::after { right: 0; }
/* 層板：實心松木，比鍍鉻的橫桿厚 */
.rack-frame.is-wood .u-row {
  border-bottom: var(--rd-board, 6px) solid transparent;
  border-image: linear-gradient(180deg,
    #e9c99d 0%, #d2a56f 55%, #a5763f 100%) 1;
}
/* 支撐桿：兩根交叉成 X（官方商品圖就是 X），高度＝它實際跨的層數。
   注意漸層方向：色帶是「垂直於」漸層軸的，所以兩條軸分別指向右下與右上。 */
.wood-brace {
  position: absolute;
  left: 0; right: 0;
  pointer-events: none;
  background-image:
    linear-gradient(to bottom right, transparent calc(50% - 1.3px),
      rgba(150, 156, 162, 0.9) calc(50% - 1.3px) calc(50% + 1.3px), transparent calc(50% + 1.3px)),
    linear-gradient(to top right, transparent calc(50% - 1.3px),
      rgba(150, 156, 162, 0.9) calc(50% - 1.3px) calc(50% + 1.3px), transparent calc(50% + 1.3px));
}

/* 控制列：flex 對齊，不吃 baseline */
.rd-toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 10px;
}

/* 頂板：與各型態的層板同色同厚 */
.shelf-top {
  position: absolute;
  left: 0; right: 0; top: 0;
  pointer-events: none;
}
.rack-frame.is-shelf .shelf-top {
  height: 2px; background: #8b9096;
}
.rack-frame.is-wire .shelf-top {
  height: var(--rd-board, 6px);
  background: linear-gradient(180deg, #ffffff 0%, #dfe4e8 25%, #a8aeb4 70%, #71767b 100%);
}
.rack-frame.is-wood .shelf-top {
  height: var(--rd-board, 6px);
  background: linear-gradient(180deg, #e9c99d 0%, #d2a56f 55%, #a5763f 100%);
  box-shadow: 0 0 0 0.6px rgba(90, 60, 30, 0.35);
}

.u-row {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  height: var(--rd-row-h, 28px);
  border-bottom: 1px dashed rgba(127, 127, 127, 0.2);
  padding: 0 8px;
  font-size: 12px;
  font-family: monospace;
  color: white;
  position: relative;
}
.u-row:last-child {
  border-bottom: none;
}
.u-row.u-pickable { cursor: pointer; color: var(--n-text-color-3, #999); justify-content: center; }
.u-row.u-pickable:hover { background: rgba(24,160,88,0.14); color: var(--primary-color, #18a058); }
.u-plus { font-size: 13px; opacity: 0; }
.u-row.u-pickable:hover .u-plus { opacity: 1; }
.u-row:not(.u-occupied) {
  color: rgba(127, 127, 127, 0.5);
  background: transparent;
}
/* 裝置外框：每台（含多 U）都框起來，多 U 之間不畫內線 → 一眼看出佔幾 U */
.u-occupied {
  border-bottom: none;
  border-left: 2px solid rgba(0, 0, 0, 0.32);
  border-right: 2px solid rgba(0, 0, 0, 0.32);
  cursor: pointer;
}
.u-occupied.u-top { border-top: 2px solid rgba(0, 0, 0, 0.32); }
.u-occupied.u-bottom { border-bottom: 2px solid rgba(0, 0, 0, 0.32); }
/* hover 任一 U → 整台裝置點亮 + 框線（左右框；最上/最下格補上下框）
   ⚠️ **不要用 `filter` 或 `opacity` 來打亮**：那兩個都會讓每一格變成獨立的堆疊環境，
   跨多 U 的名稱（絕對定位在最上面那一格、往下延伸）就跳不出去，會被下面幾格蓋掉 ——
   實機症狀是「游標移過去，裝置名稱就不見了」。改用疊一層半透明白色，不建立堆疊環境。 */
.u-row.u-hl {
  box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24;
}
.u-row.u-hl::after,
.u-part.u-hl::after {
  content: "";
  position: absolute;
  inset: 0;
  background: rgba(255, 255, 255, 0.16);
  pointer-events: none;
}
.u-row.u-hl.u-top { box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24, inset 0 2px 0 #fbbf24; }
.u-row.u-hl.u-bottom { box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24, inset 0 -2px 0 #fbbf24; }
.u-row.u-hl.u-top.u-bottom { box-shadow: inset 0 0 0 2px #fbbf24; }
/* 橫向分格（issue #31）：一個 U 是 12 格的網格，裝置與空隙都用百分比絕對定位。
   整 U 也走同一條路（span=12），所以只有一套幾何要維護。 */
.u-row.u-slots { padding: 0; position: relative; }
.u-part {
  position: absolute; top: 0; bottom: 0;
  display: flex; align-items: center;
  box-sizing: border-box;
  /* ⚠️ 這裡**不能**設 overflow:hidden。跨多 U 的名稱是靠 .d-name-span 絕對定位往下
     溢出自己那一格來置中的（見下方註解），一加 overflow 就會被裁掉 ——
     實測症狀正是「2U 名稱被切一半、4U 完全看不見」。
     橫向的截斷由內層 .d-name 的 ellipsis 負責，不需要在這層裁。 */
}
.u-part + .u-part { border-left: 1px dashed rgba(127, 127, 127, 0.28); }
.u-part.u-occupied { color: #fff; font-size: 12px; }
.u-part.u-occupied.u-top { border-top: 2px solid rgba(0, 0, 0, 0.32); }
.u-part.u-occupied.u-bottom { border-bottom: 2px solid rgba(0, 0, 0, 0.32); }
.u-part.u-hl { box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24; }
.u-part.u-gap { color: rgba(127, 127, 127, 0.5); justify-content: center; }
.u-part.u-pickable { cursor: pointer; }
.u-part.u-pickable:hover { background: rgba(24, 160, 88, 0.14); color: var(--primary-color, #18a058); }
.u-part.u-pickable:hover .u-plus { opacity: 1; }
.d-name-half { max-width: 100%; padding: 0 3px; }
/* 名稱跨整台裝置置中。
   **一定要絕對定位並貼齊 top**：只給高度的話，它會以所在的那一格為中心上下溢出，
   文字仍然停在第一格的中央 —— 也就是原本 2U 偏高半格的老問題，換個寫法而已。 */
.d-name-span {
  position: absolute;
  left: 0;
  right: 0;
  /* 跨多 U 時名稱會溢出「自己那一格」，而下面幾格是後面才畫的兄弟元素 ——
     沒有這個 z-index，2U 的名稱會被下一格蓋掉一半、4U 的整個看不見（實測）。 */
  z-index: 2;
  /* 絕對定位是相對「內距框」，但裝置的可見範圍是「邊框框」——
     不補回上邊框的厚度，名稱會整個往下偏一個邊框的量（1U 實測差 2px）。 */
  top: calc(-1 * var(--rd-border, 2px));
  padding: 0 8px;
  display: flex;
  align-items: center;
  justify-content: inherit;
  pointer-events: none;
}
.d-name-span-half { padding: 0 3px; }
.u-num {
  display: inline-block;
  width: 22px;
  text-align: right;
  margin-right: 6px;
  opacity: 0.8;
  font-weight: bold;
  flex-shrink: 0;
}
.d-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  /* #32：用實際裝置寬度（外層 .d-name-span 已 left:0/right:0 撐滿該格），
     不要再套半 U 的固定上限，否則全寬裝置的名稱會在半 U 處就被截斷。 */
  max-width: 100%;
}
/* ⚠️ `max-width` 只能放在**內層**：放在絕對定位的外層時，`left:0; right:0` 與
   `max-width` 同時成立會讓瀏覽器保留 left、丟掉 right，整個名稱框被釘在左邊 ——
   畫面上就是「對齊設定選了置中卻沒反應」（實測 span 寬 126、整列寬 250）。 */
.d-ip {
  margin-left: auto;
  font-size: 11px;
  opacity: 0.85;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 90px;
}
/* bare：去掉卡片外框/底色/標題與內距，純嵌入 */
.rd-bare { background: transparent; box-shadow: none; border: none; }
.rd-bare :deep(.n-card__content) { padding: 0; }
.rd-bare :deep(.n-card-header) { display: none; }
/* 聚焦模式：設了 highlightId 時，其他裝置淡化，只突顯本裝置 */
/* 同上：`opacity` / `filter` 會建立堆疊環境 → 用疊一層背景色來壓暗 */
.u-dim { color: rgba(255, 255, 255, 0.55); }
.u-dim::after {
  content: "";
  position: absolute;
  inset: 0;
  background: rgba(250, 250, 250, 0.62);
  pointer-events: none;
}
.u-dim .d-name { opacity: 0.75; }
/* compact：較小列高，給裝置詳細資料側欄用 */
/* 高度由上面的 rowPx 決定（行內 style），這裡只調字級 —— 兩邊各設一次就會打架 */
.rd-compact .u-row { font-size: 10px; }
.rd-compact .u-num-out { font-size: 9px; }
.rd-compact .d-name { font-size: 10px; max-width: 100%; }
.rd-compact .d-name-half { font-size: 9px; }
.rd-compact :deep(.n-card-header) { padding: 10px 14px; }
.rd-compact :deep(.n-card-header__main) { font-size: 13px; }
.rack-tip { font-size: 12px; line-height: 1.6; min-width: 150px; }
.rack-tip .rt-name { font-weight: 700; margin-bottom: 4px; font-size: 13px; }
.rack-tip .rt-row { display: flex; justify-content: space-between; gap: 16px; }
.rack-tip .rt-row > span { opacity: 0.65; }
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 11px;
}
.legend-item {
  padding: 2px 8px;
  border-radius: 3px;
  color: white;
  font-family: monospace;
}
.legend-note {
  font-size: 12px;
  opacity: 0.7;
  align-self: center;
}
</style>
