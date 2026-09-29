import { describe, expect, it } from "vitest";
import { h, nextTick, ref } from "vue";
import { mount } from "@vue/test-utils";
import { NDataTable } from "naive-ui";
import { installResizableColumns, withResizable } from "@/utils/resizableColumns";

// 使用者要求：所有有欄位標頭的表格都能拖拉改欄寬 —— 做在元件層，逐頁補一定會漏
installResizableColumns(NDataTable);

describe("withResizable", () => {
  it("資料欄預設可拖拉，明寫 false 的尊重", () => {
    const out = withResizable<Record<string, unknown>>([
      { key: "a", title: "A" }, { key: "b", title: "B", resizable: false },
    ]);
    expect(out[0].resizable).toBe(true);
    expect(out[1].resizable).toBe(false);
  });

  it("勾選欄與展開欄不動；群組欄往下套到子欄", () => {
    const out = withResizable<Record<string, unknown>>([
      { type: "selection" }, { type: "expand" },
      { title: "G", key: "g", children: [{ key: "c", title: "C" }] },
    ]);
    expect(out[0].resizable).toBeUndefined();
    expect(out[1].resizable).toBeUndefined();
    expect(out[2].resizable).toBeUndefined();
    expect((out[2].children as Record<string, unknown>[])[0].resizable).toBe(true);
  });

  it("寬度相關的設定一概不動（拖拉之前的排版要跟原本一模一樣）", () => {
    // 元件拿 width 當標頭的最小寬度；補 minWidth 會蓋掉它，沒有固定排版的表格在手機上會被擠成直排
    const out = withResizable<Record<string, unknown>>([
      { key: "a" }, { key: "ip", width: 160 }, { key: "m", minWidth: 140 }, { key: "p", width: "20%" },
    ]);
    expect(out.map((c) => [c.width, c.minWidth, c.maxWidth])).toEqual([
      [undefined, undefined, undefined], [160, undefined, undefined],
      [undefined, 140, undefined], ["20%", undefined, undefined],
    ]);
  });

  it("欄位的函式（render／sorter）原樣保留", () => {
    const render = () => "x";
    const sorter = () => 0;
    const [c] = withResizable<Record<string, unknown>>([{ key: "a", render, sorter }]);
    expect(c.render).toBe(render);
    expect(c.sorter).toBe(sorter);
  });
});

describe("NDataTable 套用後", () => {
  it("每個資料欄的標頭都有拖拉把手", () => {
    const w = mount(NDataTable, {
      props: {
        columns: [{ type: "selection" }, { key: "a", title: "A" }, { key: "b", title: "B" },
                  { key: "c", title: "C", resizable: false }],
        data: [{ a: 1, b: 2, c: 3, key: 1 }],
        rowKey: (r: { key: number }) => r.key,
      },
    });
    expect(w.findAll("[data-data-table-resizable]").length).toBe(2);
  });

  it("父層改欄位（響應式）時跟著更新", async () => {
    const cols = ref([{ key: "a", title: "A" }]);
    const w = mount({ render: () => h(NDataTable, { columns: cols.value, data: [] }) });
    expect(w.findAll("[data-data-table-resizable]").length).toBe(1);
    cols.value = [...cols.value, { key: "b", title: "B" }];
    await nextTick();
    expect(w.findAll("[data-data-table-resizable]").length).toBe(2);
  });
});
