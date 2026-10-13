/**
 * 主控台「已存帳密」的預設選擇（2026-10-12）：綁在位址上的帳密，存下之後那個位址換了一台設備（MAC 變了，
 * 後端回 device_changed）→ 不自動帶入，選項後面註明，使用者確認後仍可手動選。
 * 以前一律預設選最近一筆：DHCP 位址換給別台機器後，RDP（不驗憑證）會直接把上一台的帳密送過去。
 */
type T = (key: string) => string;

export function defaultCredId<C extends { id: string; device_changed?: boolean }>(list: C[]): string | null {
  return list.find((c) => !c.device_changed)?.id ?? null;
}

export function credSuffix(t: T, c: { device_changed?: boolean }): string {
  return c.device_changed ? t("ssh.cred_device_changed") : "";
}
