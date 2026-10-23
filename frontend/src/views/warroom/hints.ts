/** 未授權 IP 等項目的判斷線索（後端 services/warroom/hints.py）：先放最能認出設備的。 */
import type { Component } from "vue";
import { AppWindow, QuestionMark } from "@iconoir/vue";
import { DnsIcon, MacIcon, VendorIcon, VirtualizationIcon } from "@/icons";
import { DEVICE_KIND_ICONS } from "@/utils/deviceKindIcons";

export interface IpHints {
  mac?: string; vendor?: string; random_mac?: boolean; sources?: string[]; vm?: string; platform?: string; dns?: string;
  /** 推測的作業系統與設備類型（10-22：「如果有推測的 OS 或裝置類型也要顯示」） */
  os?: string; device_kind?: string; device_model?: string;
}
export interface HintItem { key: string; icon: Component; text: string; strong?: boolean; mono?: boolean }

type T = (k: string, p?: Record<string, unknown>) => string;
type TE = (k: string) => boolean;

/** 每條線索一個項目（圖示＋文字）：畫面上逐項排開、各自不折斷，比一整串「·」好讀（10-22） */
export function hintItems(h: IpHints | undefined, t: T, te: TE = () => false): HintItem[] {
  if (!h) return [];
  const out: HintItem[] = [];
  if (h.vm) out.push({ key: "vm", icon: VirtualizationIcon, text: t("warroom.hint_vm", { name: h.vm }), strong: true });
  if (h.dns) out.push({ key: "dns", icon: DnsIcon, text: h.dns, strong: !h.vm });
  if (h.device_kind) {
    const k = `identify.type.${h.device_kind}`;
    const label = te(k) ? t(k) : h.device_kind;
    out.push({ key: "kind", icon: DEVICE_KIND_ICONS[h.device_kind] ?? QuestionMark,
               text: h.device_model ? `${label} ${h.device_model}` : label, strong: !h.vm && !h.dns });
  }
  if (h.os) out.push({ key: "os", icon: AppWindow, text: h.os });
  if (h.vendor) out.push({ key: "vendor", icon: VendorIcon, text: h.vendor });
  else if (h.random_mac) out.push({ key: "vendor", icon: VendorIcon, text: t("warroom.hint_random_mac") });
  if (h.mac) out.push({ key: "mac", icon: MacIcon, text: h.mac, mono: true });
  return out;
}

export function hintParts(h: IpHints | undefined, t: T, te?: TE): string[] {
  return hintItems(h, t, te).map((i) => i.text);
}
