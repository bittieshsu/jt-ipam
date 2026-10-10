import { apiClient } from "@/api/client";

/** 登入中的工作階段（「登入中的裝置」）。 */
export interface LoginSession {
  id: string;
  created_at: string;
  last_used_at: string;
  expires_at: string;
  ip: string | null;
  user_agent: string | null;
  method: string;
  mfa: boolean;
  current?: boolean;
}

export async function listMySessions(): Promise<LoginSession[]> {
  return (await apiClient.get<LoginSession[]>("/api/v1/auth/sessions")).data;
}
export async function revokeMySession(id: string): Promise<void> {
  await apiClient.delete(`/api/v1/auth/sessions/${id}`);
}
export async function revokeMyOtherSessions(): Promise<number> {
  return (await apiClient.post<{ revoked: number }>("/api/v1/auth/sessions/revoke-others")).data.revoked;
}

// ── 管理員 ──
export async function listUserSessions(userId: string): Promise<LoginSession[]> {
  return (await apiClient.get<LoginSession[]>(`/api/v1/users/${userId}/sessions`)).data;
}
export async function revokeUserSessions(userId: string): Promise<number> {
  return (await apiClient.post<{ revoked: number }>(`/api/v1/users/${userId}/revoke-sessions`)).data.revoked;
}
export async function resetUserMfa(userId: string): Promise<{ had_totp: boolean; sessions_revoked: number }> {
  return (await apiClient.post(`/api/v1/users/${userId}/reset-mfa`)).data;
}

export interface AuthPolicy {
  mfa_required: "off" | "admins" | "all";
  mfa_apply_to_sso: boolean;
}
export async function getAuthPolicy(): Promise<AuthPolicy> {
  return (await apiClient.get<AuthPolicy>("/api/v1/system/auth-policy")).data;
}
export async function putAuthPolicy(p: AuthPolicy): Promise<AuthPolicy> {
  return (await apiClient.put<AuthPolicy>("/api/v1/system/auth-policy", p)).data;
}

/** 使用者代理字串 → 看得懂的「瀏覽器 · 作業系統」（看不出來就給原字串前段）。 */
export function describeAgent(ua: string | null | undefined): string {
  if (!ua) return "—";
  const browser = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  const os = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS"
    : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return browser || os ? [browser, os].filter(Boolean).join(" · ") : ua.slice(0, 60);
}
