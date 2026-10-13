import { describe, expect, it } from "vitest";
import { credSuffix, defaultCredId } from "../savedCreds";

describe("savedCreds", () => {
  it("skips credentials saved before the address changed hands", () => {
    expect(defaultCredId([{ id: "a", device_changed: true }, { id: "b" }])).toBe("b");
    expect(defaultCredId([{ id: "a", device_changed: true }])).toBeNull();
    expect(defaultCredId([])).toBeNull();
  });
  it("labels them", () => {
    const t = (k: string) => `<${k}>`;
    expect(credSuffix(t, { device_changed: true })).toBe("<ssh.cred_device_changed>");
    expect(credSuffix(t, {})).toBe("");
  });
});
