/** SSH 連到 Linux 時提示搭配 tmux／screen（10-22 使用者：「如果是 ssh 且是 linux，上面可以有個訊息，
 *  建議搭配 tmux 或 screen 確保連線上的工作不因斷線而被回收」）。只在看得出是 Linux 時顯示。 */
import { describe, expect, it } from "vitest";
import { isLinuxTarget } from "../sshHints";

describe("isLinuxTarget", () => {
  it("OS 家族是 linux", () => {
    expect(isLinuxTarget("linux", null)).toBe(true);
    expect(isLinuxTarget("Linux", "")).toBe(true);
  });
  it("家族不知道時看 OS 猜測裡的發行版名稱", () => {
    for (const g of ["Ubuntu 24.04", "Debian GNU/Linux 12", "Linux 5.x", "Rocky Linux 9", "Proxmox VE 8", "CentOS 7", "Alpine"])
      expect(isLinuxTarget(null, g), g).toBe(true);
  });
  it("其他系統或看不出來：不顯示", () => {
    expect(isLinuxTarget("windows", "Windows Server 2022")).toBe(false);
    expect(isLinuxTarget("network", "RouterOS 7")).toBe(false);
    expect(isLinuxTarget("bsd", "FreeBSD 14")).toBe(false);
    expect(isLinuxTarget(null, null)).toBe(false);
    expect(isLinuxTarget("windows", "WSL Ubuntu")).toBe(false);   // 家族明確就以家族為準
  });
});
