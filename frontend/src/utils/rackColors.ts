/** 機櫃 device type → 顏色（RackDiagram U 位塊 + 共用圖例共用同一套）。
 *  **對外嵌入的 SVG（backend/app/services/rack_svg.py 的 _COLORS）要同一份**，tests/test_rack_type_colors.py 會比對。
 *  伺服器以前是灰色、「其他」也是灰色只差透明度，深色主題下整櫃灰灰的分不出來（10-23 客戶）：
 *  伺服器改成鋼藍；「其他」維持中性灰、畫面上加斜紋（rackTypeFill），一看就是未分類／配件。 */
export const RACK_DEVICE_TYPES = [
  "router", "switch", "firewall", "server", "storage", "ap", "ipmi",
  "patch_panel", "pdu", "ups", "workstation", "other",
] as const;

/** 「其他」與沒分類的底色（匯出 SVG／draw.io 用；畫面另加斜紋） */
export const RACK_OTHER_COLOR = "rgba(107, 114, 128, 0.7)";
const OTHER_STRIPES = "repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.22) 0 5px, rgba(255, 255, 255, 0) 5px 11px)";

/** 畫面上的底（CSS background）：「其他」與沒分類的是灰底加斜紋 */
export function rackTypeFill(type: string): string {
  const c = rackTypeColor(type);
  return c === RACK_OTHER_COLOR ? `${OTHER_STRIPES}, ${RACK_OTHER_COLOR}` : c;
}

export function rackTypeColor(type: string): string {
  switch (type) {
    case "router":
      return "rgba(99, 102, 241, 0.85)"; // indigo
    case "switch":
      return "rgba(34, 197, 94, 0.85)";  // green
    case "firewall":
      return "rgba(239, 68, 68, 0.85)";  // red
    case "ap":
      return "rgba(59, 130, 246, 0.85)"; // blue
    case "server":
      return "rgba(52, 104, 158, 0.9)";  // steel blue（以前是灰色，跟「其他」分不出來）
    case "storage":
      return "rgba(245, 158, 11, 0.85)"; // amber
    case "ipmi":
      return "rgba(236, 72, 153, 0.6)";  // pink
    case "patch_panel":
      return "rgba(20, 184, 166, 0.75)"; // teal
    case "pdu":
      return "rgba(217, 119, 6, 0.8)";   // dark amber
    case "ups":
      return "rgba(202, 138, 4, 0.85)";  // yellow-amber
    case "workstation":
      return "rgba(14, 165, 233, 0.8)";  // sky
    default:
      return RACK_OTHER_COLOR;
  }
}
