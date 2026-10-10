import { apiClient } from "@/api/client";

export interface EnrollResponse {
  secret: string;
  otpauth_uri: string;
}

export async function enroll(): Promise<EnrollResponse> {
  const { data } = await apiClient.post<EnrollResponse>("/api/v1/auth/totp/enroll");
  return data;
}

/** 確認啟用；回傳 10 組復原碼（只此一次）。 */
export async function confirm(secret: string, code: string): Promise<string[]> {
  const { data } = await apiClient.post<{ recovery_codes: string[] }>("/api/v1/auth/totp/confirm", { secret, code });
  return data.recovery_codes;
}

/** 停用 TOTP 需要升級驗證：本機帳號給密碼，外部認證帳號給當前 6 位數驗證碼。 */
export async function disable(payload: { password?: string; code?: string }): Promise<void> {
  await apiClient.post("/api/v1/auth/totp/disable", payload);
}

/** 重新產生復原碼（舊的全部作廢）；同樣要升級驗證。 */
export async function regenerateRecoveryCodes(payload: { password?: string; code?: string }): Promise<string[]> {
  const { data } = await apiClient.post<{ recovery_codes: string[] }>("/api/v1/auth/totp/recovery-codes", payload);
  return data.recovery_codes;
}
