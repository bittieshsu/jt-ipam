/**
 * SNMP 測試／立即收集的輪詢：代理領走工作之前是 pending，回報後 done／failed，過期是 expired。
 * 守的是「結束了就停」「按鈕的忙碌狀態跟著輪詢」「送出失敗照樣丟出例外（畫面要顯示原因）」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const getSnmpJob = vi.fn();
vi.mock("@/api/snmp", () => ({ getSnmpJob: (id: string) => getSnmpJob(id) }));

import { isJobFinished, useSnmpJob } from "../useSnmpJob";

const start = () => Promise.resolve({
  job_id: "j1", run_id: "r1", agent_id: "a1", agent_name: "agent-1", ip: "192.0.2.1",
  expires_at: new Date(Date.now() + 180_000).toISOString(),
});

describe("useSnmpJob", () => {
  beforeEach(() => { getSnmpJob.mockReset(); });

  it("輪詢到 done 就停，回傳最後的結果", async () => {
    getSnmpJob
      .mockResolvedValueOnce({ job_id: "j1", status: "pending" })
      .mockResolvedValueOnce({ job_id: "j1", status: "running" })
      .mockResolvedValueOnce({ job_id: "j1", status: "done", result: { items: [] } });
    const job = useSnmpJob(1);
    const p = job.run(start);
    expect(job.running.value).toBe(true);
    const j = await p;
    expect(j?.status).toBe("done");
    expect(getSnmpJob).toHaveBeenCalledTimes(3);
    expect(job.running.value).toBe(false);
    expect(job.started.value?.agent_name).toBe("agent-1");
  });

  it("過期與失敗也算結束", async () => {
    getSnmpJob.mockResolvedValueOnce({ job_id: "j1", status: "expired" });
    const job = useSnmpJob(1);
    expect((await job.run(start))?.status).toBe("expired");
    expect(isJobFinished("failed")).toBe(true);
    expect(isJobFinished("pending")).toBe(false);
  });

  it("送出被拒（例如收集者判定不出來）→ 丟出例外、不再轉圈", async () => {
    const job = useSnmpJob(1);
    await expect(job.run(() => Promise.reject(new Error("409")))).rejects.toThrow("409");
    expect(job.running.value).toBe(false);
    expect(getSnmpJob).not.toHaveBeenCalled();
  });

  it("stop() 之後不再輪詢", async () => {
    getSnmpJob.mockResolvedValue({ job_id: "j1", status: "pending" });
    const job = useSnmpJob(5);
    const p = job.run(start);
    await new Promise((r) => setTimeout(r, 20));
    job.stop();
    expect(await p).toBeNull();
    const calls = getSnmpJob.mock.calls.length;
    await new Promise((r) => setTimeout(r, 30));
    expect(getSnmpJob.mock.calls.length).toBe(calls);
  });
});
