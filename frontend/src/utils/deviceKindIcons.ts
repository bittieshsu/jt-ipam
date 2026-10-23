/** 設備類型 → 圖示（DeviceKindIcon 與戰情室判斷線索共用一份，類型名稱見後端 services/device_identity） */
import type { Component } from "vue";
import {
  Computer, CubeDots, HardDrive, Headset, Industry, ModernTv, Network, NetworkLeft, PcFirewall, Printer,
  Server, SmartphoneDevice, VideoCamera, Wifi,
} from "@iconoir/vue";

export const DEVICE_KIND_ICONS: Record<string, Component> = {
  router: Network, switch: NetworkLeft, firewall: PcFirewall, wireless_ap: Wifi, printer: Printer,
  camera: VideoCamera, voip: Headset, storage: HardDrive, hypervisor: CubeDots, media: ModernTv,
  specialized: Industry, server: Server, windows: Computer, mobile: SmartphoneDevice,
};
