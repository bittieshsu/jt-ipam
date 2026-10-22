/**
 * 戰情室（大螢幕看板）的 API。
 *
 * 兩種身分：
 * - 一般登入：走共用的 apiClient（帶存取權杖、401 會嘗試換新）
 * - 電視用的顯示權杖：獨立的 axios（只帶 X-Warroom-Token，不碰登入的權杖；401 不跳登入頁，畫面自己說明）
 */
import axios, { type AxiosInstance } from "axios";
import { apiClient } from "./client";

const DISPLAY_KEY = "jtipam.warroom.display";

export type CellState = string;

export interface WarroomConfig {
  pages: string[];
  all_pages: string[];
  ready_pages: string[];
  dwell_seconds: number;
  max_subpages: number;
  group_by: "location" | "section" | "customer";
  language: string;
  reduce_motion: boolean;
  thresholds: { warn: number; crit: number };
  via: "session" | "display";
  is_admin: boolean;
  /** 顯示範圍（null＝全部）：頁首顯示前幾個名稱 */
  scope: { names: string[]; more: number } | null;
}

export interface Section<T = Record<string, unknown>> { state: "ok" | "empty" | "forbidden" | "disabled"; [k: string]: unknown; items?: T[] }

export interface PageData {
  key: string;
  data_at: string;
  page: number;
  pages: number;
  total?: number;
  metrics: Record<string, number | null> | null;
  [k: string]: unknown;
}

/** 這個分頁的顯示權杖（網址帶 ?display= 時存進 sessionStorage，網址列隨即清掉） */
export function displayToken(): string | null {
  try { return sessionStorage.getItem(DISPLAY_KEY); } catch { return null; }
}
export function setDisplayToken(t: string | null): void {
  try {
    if (t) sessionStorage.setItem(DISPLAY_KEY, t);
    else sessionStorage.removeItem(DISPLAY_KEY);
  } catch { /* 無痕或封鎖儲存：只是重新整理後要再帶一次網址 */ }
}

function client(): AxiosInstance {
  const t = displayToken();
  if (!t) return apiClient;
  return axios.create({ baseURL: "/", headers: { "X-Warroom-Token": t }, timeout: 30000 });
}

export async function getWarroomConfig(): Promise<WarroomConfig> {
  const { data } = await client().get<WarroomConfig>("/api/v1/warroom/config");
  return data;
}

export async function getWarroomPage(key: string, page = 1): Promise<PageData> {
  const { data } = await client().get<PageData>(`/api/v1/warroom/pages/${encodeURIComponent(key)}`, { params: { page } });
  return data;
}

// ── 管理：設定與顯示權杖 ──
export interface WarroomSettings {
  enabled: boolean;
  pages: string[];
  all_pages: string[];
  ready_pages: string[];
  dwell_seconds: number;
  max_subpages: number;
  group_by: string;
  capacity_warn: number;
  capacity_crit: number;
  language: string;
  reduce_motion: boolean;
  ai_summary: string;
  display_enabled: boolean;
  principal_user_id: string | null;
  token_set: boolean;
  token_expires_at: string | null;
  scope: Record<ScopeKind, string[]>;
  scope_labels?: Record<ScopeKind, ScopeOption[]>;
}

export type ScopeKind = "customers" | "sections" | "subnets";
export interface ScopeOption { value: string; label: string }

/** 顯示範圍的選單：後端依名稱搜尋、最多 50 筆（大站台不整份載入） */
export async function searchWarroomScope(kind: ScopeKind, q: string): Promise<ScopeOption[]> {
  const { data } = await apiClient.get<ScopeOption[]>("/api/v1/system/warroom/scope-options", { params: { kind, q } });
  return data;
}

export async function getWarroomSettings(): Promise<WarroomSettings> {
  const { data } = await apiClient.get<WarroomSettings>("/api/v1/system/warroom");
  return data;
}
export async function putWarroomSettings(body: Partial<WarroomSettings>): Promise<WarroomSettings> {
  const { data } = await apiClient.put<WarroomSettings>("/api/v1/system/warroom", body);
  return data;
}
export async function rotateWarroomToken(days: number): Promise<{ token: string; expires_at: string | null }> {
  const { data } = await apiClient.post("/api/v1/system/warroom/token", { days });
  return data;
}
export async function revealWarroomToken(): Promise<{ token: string; expires_at: string | null }> {
  const { data } = await apiClient.get("/api/v1/system/warroom/token");
  return data;
}
export async function revokeWarroomToken(): Promise<void> {
  await apiClient.delete("/api/v1/system/warroom/token");
}
