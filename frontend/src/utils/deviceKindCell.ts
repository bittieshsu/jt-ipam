import { h, type VNodeChild } from "vue";
import DeviceKindIcon from "@/components/DeviceKindIcon.vue";

/**
 * IP 清單的「設備類型」欄位內容（圖示＋名稱，滑過顯示型號）。
 *
 * 「IP 位址」頁與子網路頁的 IP 清單共用這一份：同一份資料兩個入口，欄位與顯示要一樣
 * （子網路頁曾經整個少了這一欄，使用者在 IP 詳細資料看得到、在子網路的清單卻選不到）。
 */
export function renderDeviceKind(
  r: { device_kind?: string | null; device_model?: string | null; __gap?: unknown },
  t: (k: string) => string, te: (k: string) => boolean,
): VNodeChild {
  if (r.__gap || !r.device_kind) return "—";
  const key = `identify.type.${r.device_kind}`;
  const label = te(key) ? t(key) : r.device_kind;
  return h("div", {
    style: "display:flex;align-items:center;gap:4px;min-width:0;white-space:nowrap",
    title: r.device_model ? `${label} · ${r.device_model}` : label,
  }, [
    h(DeviceKindIcon, { kind: r.device_kind, size: 16 }),
    h("span", { style: "overflow:hidden;text-overflow:ellipsis" }, label),
  ]);
}
