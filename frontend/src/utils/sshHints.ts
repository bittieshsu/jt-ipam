/**
 * SSH 主控台的提示。連到 Linux 時建議把長時間的工作放在 tmux 或 screen 裡：瀏覽器斷線、關掉分頁、
 * 閒置被切斷時，SSH 工作階段結束，裡面跑的程式會收到 SIGHUP 一起被結束（10-22 使用者要求）。
 * 只在看得出是 Linux 時顯示：OS 家族明確就以家族為準，不知道時才看 OS 猜測裡的發行版名稱。
 */
const DISTROS = /\b(linux|ubuntu|debian|centos|rocky|alma|red ?hat|rhel|fedora|suse|opensuse|alpine|arch|gentoo|proxmox|raspbian|kali|mint|oracle linux|amazon linux)\b/i;

export function isLinuxTarget(osFamily: string | null | undefined, osGuess: string | null | undefined): boolean {
  const fam = (osFamily || "").trim().toLowerCase();
  if (fam && fam !== "unknown") return fam === "linux";
  return DISTROS.test(osGuess || "");
}
