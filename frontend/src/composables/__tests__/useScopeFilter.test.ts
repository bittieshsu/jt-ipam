import { describe, it, expect } from "vitest";
import { nextTick, ref } from "vue";
import { useScopeFilter } from "../useScopeFilter";

const rows = ref([
  { ip: "a", section_id: "s1", section_name: "HQ", subnet_id: "n1", subnet_cidr: "10.0.1.0/24", customer_id: "c1", customer_name: "ACME" },
  { ip: "b", section_id: "s1", section_name: "HQ", subnet_id: "n2", subnet_cidr: "10.0.2.0/24", customer_id: null, customer_name: null },
  { ip: "c", section_id: "s2", section_name: "Branch", subnet_id: "n3", subnet_cidr: "10.1.0.0/24", customer_id: "c1", customer_name: "ACME" },
]);

describe("useScopeFilter", () => {
  it("選項來自資料、去重並排序", () => {
    const f = useScopeFilter(rows);
    expect(f.sectionOpts.value.map((o) => o.label)).toEqual(["Branch", "HQ"]);
    expect(f.customerOpts.value).toEqual([{ value: "c1", label: "ACME" }]);
  });

  it("三個條件同時成立才留下", () => {
    const f = useScopeFilter(rows);
    f.customer.value = "c1";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["a", "c"]);
    f.section.value = "s1";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["a"]);
    expect(f.active.value).toBe(true);
  });

  it("選了區段，子網路選單只列該區段的；原本選的不在裡面就清掉", async () => {
    const f = useScopeFilter(rows);
    f.subnet.value = "n3";
    f.section.value = "s1";
    await nextTick();
    expect(f.subnetOpts.value.map((o) => o.value)).toEqual(["n1", "n2"]);
    expect(f.subnet.value).toBeNull();
  });
});
