import { describe, expect, it } from "vitest";
import { fmtBytes } from "../bytes";

describe("fmtBytes", () => {
  it("1024 進位、有效位數隨大小調整、去掉多餘的 0", () => {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(94 * 1024 * 1024)).toBe("94 MB");
    expect(fmtBytes(1.25 * 1024 ** 3)).toBe("1.25 GB");
    expect(fmtBytes(12.5 * 1024 ** 3)).toBe("12.5 GB");
    expect(fmtBytes(2 * 1024 ** 3)).toBe("2 GB");
  });
  it("沒有值顯示「—」", () => {
    expect(fmtBytes(null)).toBe("—");
    expect(fmtBytes(undefined)).toBe("—");
  });
});
