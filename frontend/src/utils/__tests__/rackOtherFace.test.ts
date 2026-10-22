import { describe, expect, it } from "vitest";
import { otherFaceHints, type FaceDevice } from "../rackOtherFace";

const dev = (id: string, u: number, size: number, face: "front" | "rear" | null, extra: Partial<FaceDevice> = {}): FaceDevice =>
  ({ device_id: id, name: id, type: "server", u_position: u, u_size: size, rack_face: face, ...extra });

describe("背面裝置提示（看正面時）", () => {
  it("標準機櫃：重疊或相鄰的 U 併成一段，同一台只算一次", () => {
    const devices = [
      dev("front-1", 7, 2, "front"),
      dev("rear-a", 9, 1, "rear"),
      dev("rear-b", 7, 2, "rear"),          // 7–8，與 9 相鄰 → 同一段
      dev("rear-c", 3, 2, "rear"),
      dev("rear-d", 4, 1, "rear"),          // 落在 rear-c 的 3–4 裡
    ];
    const hints = otherFaceHints(devices, "front", "rack");
    expect(hints.map((h) => [h.from, h.to, h.devices.map((d) => d.device_id)])).toEqual([
      [7, 9, ["rear-a", "rear-b"]],         // 由上往下：U 高的那段先
      [3, 4, ["rear-c", "rear-d"]],
    ]);
  });

  it("沒標面的視為正面；看背面時反過來提示正面", () => {
    const devices = [dev("a", 5, 1, null), dev("b", 5, 1, "rear")];
    expect(otherFaceHints(devices, "front", "rack").map((h) => h.devices.length)).toEqual([1]);
    const back = otherFaceHints(devices, "rear", "rack");
    expect(back[0].devices.map((d) => d.device_id)).toEqual(["a"]);
  });

  it("層架：每一層各一個提示（不跨層合併），裝置算在它最底下那層", () => {
    const devices = [
      dev("r1", 2, 1, "rear"), dev("r2", 2, 1, "rear"), dev("r3", 2, 1, "rear"),
      dev("r4", 3, 1, "rear"),
      dev("tall", 1, 2, "rear"),            // 第 1–2 層：只算第 1 層
    ];
    const hints = otherFaceHints(devices, "front", "angle_shelf");
    expect(hints.map((h) => [h.from, h.to, h.devices.length])).toEqual([[3, 3, 1], [2, 2, 3], [1, 1, 1]]);
  });

  it("格櫃：提示對齊格位（第幾欄），同一格的合併", () => {
    const devices = [
      dev("k1", 1, 1, "rear", { rack_slot: 30, rack_slot_span: 30 }),   // 2 欄的右邊那格
      dev("k2", 1, 1, "rear", { rack_slot: 45, rack_slot_span: 15 }),   // 也在右邊那格
      dev("k3", 2, 1, "rear", { rack_slot: 0, rack_slot_span: 30 }),
    ];
    const hints = otherFaceHints(devices, "front", "kallax", 2);
    expect(hints.map((h) => [h.from, h.col, h.devices.length])).toEqual([[2, 0, 1], [1, 1, 2]]);
  });

  it("沒有另一面的裝置就沒有提示", () => {
    expect(otherFaceHints([dev("a", 1, 1, "front")], "front", "rack")).toEqual([]);
  });
});
