/**
 * 憑證集中保管 + 派送 API。
 */
import { apiClient } from "@/api/client";
import type { Paginated } from "@/api/admin";

export interface CertVersion {
  id: string;
  fingerprint_sha256: string;
  serial: string | null;
  subject: string | null;
  issuer: string | null;
  not_before: string | null;
  not_after: string;
  domains: string[] | null;
  is_current: boolean;
  uploaded_by: string | null;
  created_at: string;
}

export interface Certificate {
  id: string;
  name: string;
  description: string | null;
  domains: string[] | null;
  created_at: string;
  updated_at: string;
  current_fingerprint: string | null;
  current_not_after: string | null;
  current_days_remaining: number | null;
  version_count: number;
}

export interface CertAgent {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  scope_cert_ids: string[] | null;
  last_seen_at: string | null;
  last_source_ip: string | null;
  agent_version: string | null;
  reported: Array<Record<string, unknown>> | null;
  has_key: boolean;
  created_at: string;
  updated_at: string;
}
export interface CertAgentCreated extends CertAgent { enroll_key: string; }

// ── 憑證 ──
export async function listCertificates(): Promise<Paginated<Certificate>> {
  const { data } = await apiClient.get("/certificates");
  return data;
}
export async function createCertificate(payload: { name: string; description?: string | null }): Promise<Certificate> {
  const { data } = await apiClient.post("/certificates", payload);
  return data;
}
export async function deleteCertificate(id: string): Promise<void> {
  await apiClient.delete(`/certificates/${id}`);
}
export async function listVersions(id: string): Promise<CertVersion[]> {
  const { data } = await apiClient.get(`/certificates/${id}/versions`);
  return data;
}
export async function uploadVersion(
  id: string, files: { cert: File; key: File; chain?: File | null }, allowExpired = false,
): Promise<CertVersion> {
  const fd = new FormData();
  fd.append("cert_file", files.cert);
  fd.append("key_file", files.key);
  if (files.chain) fd.append("chain_file", files.chain);
  fd.append("allow_expired", String(allowExpired));
  const { data } = await apiClient.post(`/certificates/${id}/versions`, fd);
  return data;
}
export async function generateSelfSigned(
  id: string, payload: { common_name: string; sans: string[]; days: number },
): Promise<CertVersion> {
  const { data } = await apiClient.post(`/certificates/${id}/self-signed`, payload);
  return data;
}

// ── 派送代理 ──
export async function listCertAgents(): Promise<Paginated<CertAgent>> {
  const { data } = await apiClient.get("/cert-agents");
  return data;
}
export async function createCertAgent(payload: {
  name: string; description?: string | null; enabled?: boolean; scope_cert_ids: string[];
}): Promise<CertAgentCreated> {
  const { data } = await apiClient.post("/cert-agents", payload);
  return data;
}
export async function updateCertAgent(id: string, payload: Partial<{
  name: string; description: string | null; enabled: boolean; scope_cert_ids: string[];
}>): Promise<CertAgent> {
  const { data } = await apiClient.patch(`/cert-agents/${id}`, payload);
  return data;
}
export async function rotateCertAgentKey(id: string): Promise<CertAgentCreated> {
  const { data } = await apiClient.post(`/cert-agents/${id}/rotate-key`);
  return data;
}
export async function deleteCertAgent(id: string): Promise<void> {
  await apiClient.delete(`/cert-agents/${id}`);
}

// ── 唯讀現況（進階，global-read 可看）──
export interface CertStatusDeployment {
  cert: string | null;
  profile: string | null;
  status: string | null;
  applied_at: string | null;
  dry_run: boolean | null;
  reported_fingerprint: string | null;
  current_fingerprint: string | null;
  up_to_date: boolean;
  not_before: string | null;
  not_after: string | null;
  days_remaining: number | null;
}
export interface CertAgentStatus {
  agent: string;
  enabled: boolean;
  last_seen_at: string | null;
  agent_version: string | null;
  deployments: CertStatusDeployment[];
}
export async function getCertAgentStatus(): Promise<{ agents: CertAgentStatus[] }> {
  const { data } = await apiClient.get("/cert-agents/status");
  return data;
}
