<script setup lang="ts">
/**
 * 機櫃 U 位視覺化 (phpIPAM 招牌功能)。
 *
 * 比 phpIPAM 改進：
 *  - 顏色按 device type 區分 (router/switch/firewall/server/...)
 *  - 越界 / 重疊衝突明顯標示
 *  - 點 device 跳詳情
 *  - U 編號從上到下標示，符合機房現場認知
 */
import { computed, ref, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { NCard, NTag, NEmpty, NAlert, NSpace, NTooltip, NButton, NIcon, NDropdown } from "naive-ui";
import type { RackDiagram } from "@/api/racks";
import { rackTypeColor as colorFor } from "@/utils/rackColors";
import { ExportIcon } from "@/icons";
import { getRackNameAlign, type RackNameAlign } from "@/api/basic";

// 全域設定：機櫃中裝置名稱靠左/置中/靠右（管理員在系統設定調整）
const nameAlign = ref<RackNameAlign>("left");
onMounted(() => { void getRackNameAlign().then((a) => { nameAlign.value = a; }); });
const nameJustify = computed(() =>
  nameAlign.value === "center" ? "center" : nameAlign.value === "right" ? "flex-end" : "flex-start");

const { t } = useI18n();

const GEO = { rowH: 24, colW: 260, gutter: 32, pad: 12, headerH: 30 };
const esc = (s: unknown) => String(s ?? "").replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c] as string));

function devLabel(dev: any): string {
  // 機櫃示意圖只標裝置名稱 + 類型，不標 IP
  return `${dev.name} · ${dev.type}`;
}

// 共用：產生機櫃 SVG 字串 + 尺寸
function buildSvg(): { svg: string; W: number; H: number } | null {
  const d = props.diagram;
  if (!d) return null;
  const { rowH, colW, gutter, pad, headerH } = GEO;
  const U = d.u_height || 0;
  const W = gutter + colW + pad * 2;
  const H = headerH + U * rowH + pad * 2;
  const p: string[] = [];
  p.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" font-family="sans-serif">`);
  p.push(`<rect x="0" y="0" width="${W}" height="${H}" fill="#ffffff"/>`);
  p.push(`<text x="${pad}" y="${pad + 16}" font-size="14" font-weight="bold">Rack: ${esc(d.name)} (${U}U)</text>`);
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
    p.push(`<rect x="${gutter + 2}" y="${yTop + 1}" width="${colW - 4}" height="${hgt - 2}" rx="3" fill="${colorFor(dev.type)}" stroke="rgba(0,0,0,0.3)"/>`);
    const a = nameAlign.value;
    const tx = a === "center" ? gutter + colW / 2 : a === "right" ? gutter + colW - 10 : gutter + 10;
    const anchor = a === "center" ? "middle" : a === "right" ? "end" : "start";
    p.push(`<text x="${tx}" y="${yTop + hgt / 2 + 4}" text-anchor="${anchor}" font-size="11" font-weight="bold" fill="#ffffff">${esc(devLabel(dev))}</text>`);
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
  const { rowH, colW, gutter, pad, headerH } = GEO;
  const U = d.u_height || 0;
  const top = headerH + pad;
  const cells: string[] = [];
  cells.push('<mxCell id="0"/>');
  cells.push('<mxCell id="1" parent="0"/>');
  // 標題（放外框上方，不與最上層裝置重疊）
  cells.push(`<mxCell id="title" value="${esc(`Rack: ${d.name} (${U}U)`)}" style="text;html=1;align=left;verticalAlign=middle;fontStyle=1;fontSize=14;" vertex="1" parent="1"><mxGeometry x="${gutter}" y="${pad}" width="${colW}" height="20" as="geometry"/></mxCell>`);
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
    cells.push(`<mxCell id="dev${n++}" value="${esc(devLabel(dev))}" style="rounded=1;whiteSpace=wrap;html=1;fillColor=${fill};strokeColor=#000000;fontColor=#ffffff;fontStyle=1;align=${nameAlign.value};spacingLeft=6;spacingRight=6;" vertex="1" parent="1"><mxGeometry x="${gutter + 2}" y="${yTop + 1}" width="${colW - 4}" height="${hgt - 2}" as="geometry"/></mxCell>`);
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
]);
function onExport(key: string) {
  if (key === "svg") exportSvg();
  else if (key === "png") exportPng();
  else if (key === "drawio") exportDrawio();
}
const router = useRouter();
function goDevice(id: string) {
  router.push({ name: "device-detail", params: { id } });
}

interface Props {
  diagram: RackDiagram | null;
  showLegend?: boolean;   // 多機櫃並排時可關掉，由頁面放一個共用圖例
}
const props = withDefaults(defineProps<Props>(), { showLegend: true });
const hoveredId = ref<string | null>(null);   // hover 某 U → 整台裝置點亮+框線

interface Cell {
  u: number;          // 1-based, top-most U
  device: {
    id: string;
    name: string;
    type: string;
    vendor: string | null;
    model: string | null;
    u_size: number;
    is_top: boolean;     // device 最上格
    is_bottom: boolean;  // device 最下格
    is_mid: boolean;     // device 垂直中間格（顯示名字 → 跨多 U 置中）
    primary_ip: string | null;
  } | null;
}

const cells = computed<Cell[]>(() => {
  if (!props.diagram) return [];
  const u_height = props.diagram.u_height;
  const bottomUp = props.diagram.numbering === "bottom-up";
  const map: Record<number, Cell> = {};
  for (let u = 1; u <= u_height; u++) map[u] = { u, device: null };
  for (const d of props.diagram.devices) {
    for (let u = d.u_position; u < d.u_position + d.u_size; u++) {
      if (map[u]) {
        map[u] = {
          u,
          device: {
            id: d.device_id, name: d.name, type: d.type,
            vendor: d.vendor, model: d.model, u_size: d.u_size,
            is_top: false, is_bottom: false, is_mid: false,
            primary_ip: d.primary_ip,
          },
        };
      }
    }
  }
  // 顯示順序：top-down → 高 U 在上（u_height..1）；bottom-up → U1 在上（1..u_height）
  const order: Cell[] = bottomUp
    ? Array.from({ length: u_height }, (_, i) => map[i + 1])
    : Array.from({ length: u_height }, (_, i) => map[u_height - i]);
  // 依「顯示上的鄰居」標出每台裝置的最上/最下/中間格（兩種編號方向皆正確）
  const runs: Record<string, number[]> = {};
  order.forEach((c, i) => {
    if (!c.device) return;
    const prev = order[i - 1]?.device?.id;
    const next = order[i + 1]?.device?.id;
    c.device.is_top = prev !== c.device.id;      // 顯示上最上格 → 畫上框
    c.device.is_bottom = next !== c.device.id;   // 顯示上最下格 → 畫下框
    (runs[c.device.id] ??= []).push(i);
  });
  for (const idxs of Object.values(runs)) {
    order[idxs[Math.floor((idxs.length - 1) / 2)]].device!.is_mid = true;
  }
  return order;
});
</script>

<template>
  <n-card v-if="diagram" :title="`Rack: ${diagram.name} (${diagram.u_height}U)`">
    <template #header-extra>
      <n-dropdown trigger="click" :options="exportOptions" @select="onExport">
        <n-button size="tiny" quaternary :title="t('rack_diagram.export_svg_hint')">
          <template #icon><n-icon><ExportIcon /></n-icon></template>
          {{ t("common.export") }}
        </n-button>
      </n-dropdown>
    </template>
    <n-space vertical :size="12">
      <n-alert
        v-if="diagram.conflicts.length > 0"
        type="warning"
        :title="`${diagram.conflicts.length} conflict(s)`"
      >
        <pre style="font-size: 11px; margin: 0">{{ JSON.stringify(diagram.conflicts, null, 2) }}</pre>
      </n-alert>

      <!-- 只要機櫃有設定 U 數，即使沒有任何 device 也畫出空機櫃框 -->
      <n-empty
        v-if="!diagram.u_height"
        :description="t('rack_diagram.empty')"
      />

      <div v-else class="rack-wrap">
        <!-- U 編號：機櫃框外左側 gutter -->
        <div class="u-gutter">
          <div v-for="cell in cells" :key="'g' + cell.u" class="u-num-out">{{ cell.u }}</div>
        </div>
        <div class="rack-frame">
          <template v-for="cell in cells" :key="cell.u">
            <!-- 有裝置：hover 即時彈出結構化資訊 -->
            <n-tooltip v-if="cell.device" trigger="hover" :delay="60" placement="right">
              <template #trigger>
                <div
                  class="u-row u-occupied"
                  :class="{ 'u-top': cell.device.is_top, 'u-bottom': cell.device.is_bottom, 'u-hl': hoveredId === cell.device.id }"
                  :style="{ background: colorFor(cell.device.type), justifyContent: nameJustify }"
                  @mouseenter="hoveredId = cell.device.id"
                  @mouseleave="hoveredId = null"
                  @click="goDevice(cell.device.id)"
                >
                  <template v-if="cell.device.is_mid">
                    <span class="d-name">{{ cell.device.name }}</span>
                    <n-tag size="tiny" :bordered="false" style="margin-left: 6px">
                      {{ cell.device.type }}
                    </n-tag>
                  </template>
                </div>
              </template>
              <div class="rack-tip">
                <div class="rt-name">{{ cell.device.name }}</div>
                <div class="rt-row"><span>{{ t("cols.type") }}</span><b>{{ cell.device.type }}</b></div>
                <div v-if="cell.device.vendor" class="rt-row"><span>{{ t("cols.vendor") }}</span><b>{{ cell.device.vendor }}</b></div>
                <div v-if="cell.device.model" class="rt-row"><span>{{ t("cols.model") }}</span><b>{{ cell.device.model }}</b></div>
                <div v-if="cell.device.primary_ip" class="rt-row"><span>IP</span><b>{{ cell.device.primary_ip }}</b></div>
                <div class="rt-row"><span>{{ t("rack_diagram.height") }}</span><b>{{ cell.device.u_size }}U</b></div>
              </div>
            </n-tooltip>
            <!-- 空位 -->
            <div v-else class="u-row" :title="`Empty (U${cell.u})`"></div>
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
      </div>
    </n-space>
  </n-card>
</template>

<style scoped>
/* 實體 19" rack 比例：寬 19" × 每 U 高 1.75" → 每 U 寬高比 ≈ 10.86 : 1。
   用 width 280px / U-row 28px 接近真實機櫃外觀 (18U 1:1.8、42U 1:4.2)。 */
.rack-wrap { display: flex; align-items: flex-start; gap: 6px; }
.u-gutter { display: flex; flex-direction: column; padding-top: 6px; flex: 0 0 auto; }
.u-num-out {
  height: 28px;
  line-height: 28px;
  width: 26px;
  text-align: right;
  padding-right: 6px;
  font: bold 12px ui-monospace, SFMono-Regular, Menlo, monospace;
  color: rgba(127, 127, 127, 0.75);
}
.rack-frame {
  border: 2px solid rgba(127, 127, 127, 0.5);
  border-radius: 4px;
  padding: 4px;
  width: 250px;
  background: rgba(127, 127, 127, 0.04);
}
.u-row {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  height: 28px;
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
/* hover 任一 U → 整台裝置點亮 + 框線（左右框；最上/最下格補上下框） */
.u-row.u-hl {
  filter: brightness(1.18) saturate(1.25);
  box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24;
  z-index: 1;
}
.u-row.u-hl.u-top { box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24, inset 0 2px 0 #fbbf24; }
.u-row.u-hl.u-bottom { box-shadow: inset 2px 0 0 #fbbf24, inset -2px 0 0 #fbbf24, inset 0 -2px 0 #fbbf24; }
.u-row.u-hl.u-top.u-bottom { box-shadow: inset 0 0 0 2px #fbbf24; }
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
  max-width: 110px;
}
.d-ip {
  margin-left: auto;
  font-size: 11px;
  opacity: 0.85;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 90px;
}
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
</style>
