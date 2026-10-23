import { beforeEach, describe, expect, it } from "vitest";
import { displayToken, setDisplayToken } from "../warroom";

describe("war room display token", () => {
  beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });

  it("survives a browser restart (TV switched off overnight)", () => {
    setDisplayToken("tok-1");
    sessionStorage.clear();                        // 瀏覽器關掉重開：sessionStorage 沒了
    expect(displayToken()).toBe("tok-1");
  });

  it("clearing removes it everywhere", () => {
    setDisplayToken("tok-1");
    setDisplayToken(null);
    expect(displayToken()).toBeNull();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});

describe("migration from the old sessionStorage key", () => {
  beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
  it("moves a token saved by the previous version to localStorage", () => {
    sessionStorage.setItem("jtipam.warroom.display", "old-tok");
    expect(displayToken()).toBe("old-tok");
    sessionStorage.clear();
    expect(displayToken()).toBe("old-tok");
  });
});
