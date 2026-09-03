<script setup lang="ts">
/**
 * 系統設定（僅管理員）— 全域、所有使用者共用的設定，獨立於「個人設定」。
 * 含：地圖供應商、機櫃名稱對齊、上線判定閾值、GeoIP(MaxMind) 本地資料庫與排程。
 */
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  NCard, NSpace, NIcon, NSelect, NInput, NInputNumber, NSwitch, NCheckbox, NCheckboxGroup,
  NButton, NPopconfirm, NTag, useMessage,
} from "naive-ui";
const origin = window.location.origin;
import { AdminIcon, SaveIcon, RefreshIcon, WarnIcon } from "@/icons";
import { getRackEmbedConfig, setRackEmbedConfig, type RackEmbedConfig } from "@/api/racks";
import { getLdap, putLdap, testLdap, testLdapAuth, type LdapConfig,
  getAuditForward, putAuditForward, testAuditForward, type AuditForward,
  getOidcConfig, putOidcConfig, testOidc, type OidcConfig,
  getSamlConfig, putSamlConfig, testSaml, type SamlConfig,
  getConsoleSecurity, setConsoleSecurity,
  getUiDisplay, setUiDisplay } from "@/api/system";
import { listGroups } from "@/api/admin";
import { getAutolink, putAutolink, previewAutolink,
  type AutolinkConfig, type AutolinkPreview } from "@/api/system";
import { listSubnets } from "@/api/subnets";
import { fmtDateTime, fmtRelative } from "@/utils/datetime";
import { apiErrMsg } from "@/api/client";
import {
  getMapProvider, setMapProvider, getRackNameAlign, setRackNameAlign,
  getOnlineGrace, setOnlineGrace,
  getIpCooldown, setIpCooldown,
  getCertExpiryDays, setCertExpiryDays,
  getGeoipConfig, setGeoipConfig, updateGeoipDbNow,
  type GeoIPConfig, type RackNameAlign, type LivenessSource,
} from "@/api/basic";

const { t } = useI18n();
const msg = useMessage();

// 地圖供應商
// 連線管理資安：RDP 控制端貼上文字到被控端（預設關閉）
const rdpClipPaste = ref(false);
async function changeRdpClipPaste(v: boolean) {
  rdpClipPaste.value = v;
  try { await setConsoleSecurity({ rdp_clipboard_paste: v }); msg.success(t("common.ok")); }
  catch { rdpClipPaste.value = !v; msg.error(t("errors.network")); }
}

// 異動記錄淡化天數（超過 N 天的項目以淡色顯示；0 = 不淡化）
const changeLogDimDays = ref(30);
async function changeDimDays(v: number | null) {
  const days = Math.max(0, Math.min(3650, Math.round(v ?? 0)));
  changeLogDimDays.value = days;
  try { await setUiDisplay({ change_log_dim_days: days }); msg.success(t("common.ok")); }
  catch (e) { msg.error(apiErrMsg(e)); }
}

const mapProvider = ref<"builtin" | "osm" | "google">("builtin");
const mapProviderOpts = computed(() => [
  { label: t("settings.system.map_builtin"), value: "builtin" },
  { label: "OpenStreetMap", value: "osm" },
  { label: "Google Maps", value: "google" },
]);
async function changeMapProvider(p: "builtin" | "osm" | "google") {
  mapProvider.value = p;
  try { await setMapProvider(p); msg.success(t("common.ok")); } catch (e) { msg.error(apiErrMsg(e)); }
}

// 機櫃裝置名稱對齊
const rackAlign = ref<RackNameAlign>("left");
const rackAlignOpts = computed(() => [
  { label: t("settings.system.align_left"), value: "left" },
  { label: t("settings.system.align_center"), value: "center" },
  { label: t("settings.system.align_right"), value: "right" },
]);
async function changeRackAlign(a: RackNameAlign) {
  rackAlign.value = a;
  try { await setRackNameAlign(a); msg.success(t("common.ok")); } catch (e) { msg.error(apiErrMsg(e)); }
}

// 上線判定：閾值（分鐘）＋採信哪些證據
const grace = ref(30);
// LibreNMS 的 ARP 預設不勾 —— 它沒有時間概念，來源設備的快取不老化就會讓關機的
// 機器一直顯示上線。候選清單由後端給（只列這個站台真的有的整合）。
const livenessSrc = ref<string[]>(["scanner", "librenms"]);
const livenessAvail = ref<LivenessSource[]>([]);

const VENDOR_LABEL: Record<string, string> = {
  opnsense: "OPNsense", pfsense: "pfSense",
  fortigate: "FortiGate", paloalto: "Palo Alto",
};

/** `arp:opnsense` → 「ARP 表（OPNsense）」；沒有廠牌後綴的用既有翻譯。 */
function srcLabel(key: string): string {
  if (!key.includes(":")) return t(`system_settings.src_${key}`);
  const [kind, vendor] = key.split(":", 2);
  return `${t(`system_settings.src_kind_${kind}`)}（${VENDOR_LABEL[vendor] ?? vendor}）`;
}

/** 分組顯示時，種類已經在列首寫過了 → 每一格只寫廠牌名。 */
function vendorLabel(key: string): string {
  // 舊的籠統 "arp" 就是 LibreNMS 的 ARP（同一件事的舊鍵）→ 併進 ARP 那一列，
  // 放在「探測／監控」會既不同類、又因為字長而把那一列撐出換行。
  if (key === "arp") return "LibreNMS";
  const vendor = key.split(":", 2)[1] ?? key;
  return VENDOR_LABEL[vendor] ?? vendor;
}

/** 這個來源歸在哪一組。 */
function srcKind(key: string): string {
  if (key === "arp") return "arp";
  return key.includes(":") ? key.split(":", 1)[0] : "base";
}

//: 分組的順序＝可信度由高到低（探測 → 防火牆的 ARP／VPN → 不會過期的租約與 ARP 記錄）
const SRC_KINDS = ["base", "arp", "vpn", "lease"] as const;

const livenessGroups = computed(() =>
  SRC_KINDS.map((kind) => ({
    kind,
    title: kind === "base"
      ? t("system_settings.src_kind_base")
      : t(`system_settings.src_kind_${kind}`),
    // 會過期的排前面：那些才是預設採信的，不建議的（不會過期）擺最後
    items: livenessAvail.value.filter((s) => srcKind(s.key) === kind)
      .slice().sort((a, b) => Number(b.aging) - Number(a.aging)),
  })).filter((g) => g.items.length));
// IP 生命週期：釋放後的冷卻天數
const cooldownDays = ref(30);
// 憑證到期通知的全域預設。每張憑證可各自覆寫（憑證頁的鈴鐺按鈕）——
// 這裡只是「沒特別設定時用哪個」。
const certExpiryDays = ref(21);
async function changeCertExpiry(n: number | null) {
  if (n == null) return;
  certExpiryDays.value = n;
  try { await setCertExpiryDays(n); msg.success(t("common.ok")); } catch (e) { msg.error(apiErrMsg(e)); }
}
async function changeCooldown(v: number | null) {
  const n = v ?? 0;
  cooldownDays.value = n;
  try { await setIpCooldown(n); msg.success(t("common.ok")); } catch (e) { msg.error(apiErrMsg(e)); }
}

async function changeSources(v: (string | number)[]) {
  const list = v.map(String);
  livenessSrc.value = list;
  try { await setOnlineGrace(grace.value, list); msg.success(t("common.ok")); }
  catch (e) { msg.error(apiErrMsg(e)); }
}
async function changeGrace(v: number | null) {
  const n = v ?? 30;
  grace.value = n;
  try { await setOnlineGrace(n, livenessSrc.value); msg.success(t("common.ok")); }
  catch (e) { msg.error(apiErrMsg(e)); }
}

// GeoIP
// 機櫃圖對外嵌入：這裡只管系統層的總開關與權杖，要公開哪一櫃在機櫃頁逐櫃決定
const rackEmbed = ref<RackEmbedConfig | null>(null);
const showRackToken = ref(false);
const rackEmbedToken = computed(() => rackEmbed.value?.token ?? "");
async function loadRackEmbed() {
  try { rackEmbed.value = await getRackEmbedConfig(); } catch { rackEmbed.value = null; }
}
async function changeRackEmbed(v: boolean) {
  try {
    rackEmbed.value = await setRackEmbedConfig(v);
    msg.success(t("common.saved"));
  } catch { msg.error(t("errors.server")); }
}
async function regenRackToken() {
  try {
    rackEmbed.value = await setRackEmbedConfig(rackEmbed.value?.enabled ?? true, true);
    // 舊網址立刻失效，這件事一定要講出來，否則別人的儀表板會突然破圖而找不到原因
    msg.success(t("system_settings.rack_embed_regenerated"));
  } catch { msg.error(t("errors.server")); }
}
async function copyRackToken() {
  if (!rackEmbedToken.value) return;
  try {
    await navigator.clipboard.writeText(rackEmbedToken.value);
    msg.success(t("common.copied"));
  } catch { msg.error(t("errors.server")); }
}

const geoip = ref<GeoIPConfig | null>(null);
const geoipAccount = ref("");
const geoipKey = ref("");
const geoipSaving = ref(false);
const geoipUpdating = ref(false);
const geoipEditionOpts = computed(() => (geoip.value?.all_editions ?? []).map((e) => ({ label: e, value: e })));
const geoipFreqOpts = computed(() => (geoip.value?.frequencies ?? []).map((f) => ({ label: t(`settings.system.freq_${f.replace("-", "_")}`), value: f })));
async function loadGeoip() {
  try { geoip.value = await getGeoipConfig(); geoipAccount.value = geoip.value.account_id ?? ""; } catch { /* ignore */ }
}
async function saveGeoip() {
  if (!geoip.value) return;
  geoipSaving.value = true;
  try {
    geoip.value = await setGeoipConfig({
      account_id: geoipAccount.value.trim() || null,
      license_key: geoipKey.value.trim() || null,
      editions: geoip.value.editions,
      auto_update: geoip.value.auto_update,
      frequency: geoip.value.frequency,
    });
    geoipKey.value = "";
    msg.success(t("common.saved"));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { geoipSaving.value = false; }
}
async function updateGeoipNow() {
  geoipUpdating.value = true;
  try {
    const r = await updateGeoipDbNow();
    geoip.value = r.config;
    if (r.result?.error === "not_configured") msg.warning(t("settings.system.geoip_need_creds"));
    else msg.success(t("settings.system.geoip_updated"));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { geoipUpdating.value = false; }
}
function fmtBytes(n: number | null): string {
  if (!n) return "—";
  return n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : (n / 1e3).toFixed(0) + " KB";
}

// 外部認證 / LDAP（AD）
const ldap = ref<LdapConfig>({
  enabled: false, server: null, port: 389, use_ssl: false, use_starttls: true,
  bind_dn: null, password_set: false, search_base: null,
  user_filter: "(sAMAccountName={username})", attr_email: "mail",
  attr_display_name: "displayName", attr_member_of: "memberOf", admin_groups: [],
  default_group_id: null,
});
const ldapGroups = ref<{ label: string; value: string }[]>([]);
const ldapGroupOpts = computed(() => [{ label: t("settings.system.ldap_no_default_role"), value: "" }, ...ldapGroups.value]);
const ldapDefaultGroup = computed<string>({
  get: () => ldap.value.default_group_id ?? "",
  set: (v) => { ldap.value.default_group_id = v || null; },
});
async function loadLdapGroups() {
  try { const r = await listGroups(200, 0); ldapGroups.value = r.items.map((g) => ({ label: g.name, value: g.id })); } catch { /* ignore */ }
}
const ldapPw = ref("");           // 留空＝不變更；輸入＝更新
const ldapSaving = ref(false);
const ldapTesting = ref(false);
const ldapTlsOpts = computed(() => [
  { label: "StartTLS (389)", value: "starttls" },
  { label: "LDAPS (636)", value: "ssl" },
  { label: t("settings.system.ldap_tls_none"), value: "none" },
]);
const ldapTlsMode = computed<"starttls" | "ssl" | "none">({
  get: () => ldap.value.use_ssl ? "ssl" : ldap.value.use_starttls ? "starttls" : "none",
  set: (m) => { ldap.value.use_ssl = m === "ssl"; ldap.value.use_starttls = m === "starttls"; },
});
const ldapGroupsText = computed<string>({
  get: () => ldap.value.admin_groups.join("\n"),
  set: (v) => { ldap.value.admin_groups = v.split("\n").map((s) => s.trim()).filter(Boolean); },
});
async function loadLdap() { try { ldap.value = await getLdap(); ldapPw.value = ""; } catch { /* ignore */ } }
async function saveLdap() {
  ldapSaving.value = true;
  try {
    const { password_set: _ps, ...rest } = ldap.value;
    ldap.value = await putLdap({ ...rest, bind_password: ldapPw.value ? ldapPw.value : null });
    ldapPw.value = "";
    msg.success(t("common.saved"));
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.network")); } finally { ldapSaving.value = false; }
}
async function clearLdapPw() {
  ldapSaving.value = true;
  try {
    const { password_set: _ps, ...rest } = ldap.value;
    ldap.value = await putLdap({ ...rest, bind_password: "" });
    ldapPw.value = "";
    msg.success(t("common.ok"));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { ldapSaving.value = false; }
}
async function doTestLdap() {
  ldapTesting.value = true;
  try {
    const r = await testLdap();
    msg.success(`${t("settings.system.ldap_test_ok")} — ${r.who_am_i || r.server}`);
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("settings.system.ldap_test_fail")); }
  finally { ldapTesting.value = false; }
}

// ── OIDC SSO ──
const oidc = ref<OidcConfig>({
  enabled: false, issuer: null, client_id: null, client_secret_set: false,
  redirect_uri: null, scope: "openid profile email",
  groups_claim: "groups", username_claim: "preferred_username",
  admin_groups: [], default_group_id: null,
});
const oidcSecret = ref("");        // 留空＝不變更；輸入＝更新
const oidcSaving = ref(false);
const oidcTesting = ref(false);
const oidcAdminGroupsText = computed<string>({
  get: () => (oidc.value.admin_groups || []).join(", "),
  set: (v) => { oidc.value.admin_groups = v.split(",").map((x) => x.trim()).filter(Boolean); },
});
async function loadOidc() { try { oidc.value = await getOidcConfig(); oidcSecret.value = ""; } catch { /* ignore */ } }
async function saveOidc() {
  oidcSaving.value = true;
  try {
    const { client_secret_set: _s, ...rest } = oidc.value;
    const patch: any = { ...rest };
    if (oidcSecret.value) patch.client_secret = oidcSecret.value;  // 只在輸入時更新
    oidc.value = await putOidcConfig(patch);
    oidcSecret.value = "";
    msg.success(t("common.saved"));
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.network")); }
  finally { oidcSaving.value = false; }
}
async function doTestOidc() {
  oidcTesting.value = true;
  try {
    const r = await testOidc();   // 成功會回 discovery 資訊；失敗會丟錯
    msg.success(`${t("settings.system.oidc_test_ok")} — ${r.issuer || ""}`);
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("settings.system.oidc_test_fail")); }
  finally { oidcTesting.value = false; }
}

// ── SAML SSO ──
const saml = ref<SamlConfig>({
  enabled: false, idp_metadata_url: null, idp_metadata_xml: null,
  sp_entity_id: null, sp_acs_url: null, sp_sls_url: null, sp_x509_cert: null,
  sp_private_key_set: false, want_assertions_signed: true, want_assertions_encrypted: false,
  want_name_id_encrypted: false, authn_requests_signed: false,
  attr_username: "uid", attr_email: "email", attr_displayname: "displayName",
  attr_groups: "groups", admin_groups: [], default_group_id: null,
});
const samlKey = ref("");           // SP 私鑰，留空＝不變更
const samlSaving = ref(false);
const samlTesting = ref(false);
const samlAdminGroupsText = computed<string>({
  get: () => (saml.value.admin_groups || []).join(", "),
  set: (v) => { saml.value.admin_groups = v.split(",").map((x) => x.trim()).filter(Boolean); },
});
async function loadSaml() { try { saml.value = await getSamlConfig(); samlKey.value = ""; } catch { /* ignore */ } }
async function saveSaml() {
  samlSaving.value = true;
  try {
    const { sp_private_key_set: _s, ...rest } = saml.value;
    const patch: any = { ...rest };
    if (samlKey.value) patch.sp_private_key = samlKey.value;
    saml.value = await putSamlConfig(patch);
    samlKey.value = "";
    msg.success(t("common.saved"));
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.network")); }
  finally { samlSaving.value = false; }
}
async function doTestSaml() {
  samlTesting.value = true;
  try {
    const r = await testSaml();
    msg.success(`${t("settings.system.saml_test_ok")} — ${r.entity_id || ""}`);
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("settings.system.saml_test_fail")); }
  finally { samlTesting.value = false; }
}
// 用真實帳密測試完整驗證流程
const ldapTestUser = ref("");
const ldapTestPw = ref("");
const ldapAuthTesting = ref(false);
async function doTestLdapAuth() {
  if (!ldapTestUser.value || !ldapTestPw.value) { msg.warning(t("settings.system.ldap_authtest_need")); return; }
  ldapAuthTesting.value = true;
  try {
    const r = await testLdapAuth(ldapTestUser.value, ldapTestPw.value);
    msg.success(`✓ ${r.dn}${r.is_admin ? " · 管理員" : ""}${r.display_name ? " · " + r.display_name : ""}`, { duration: 8000 });
    ldapTestPw.value = "";
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("settings.system.ldap_test_fail")); }
  finally { ldapAuthTesting.value = false; }
}

// 稽核轉送到 Graylog
const af = ref<AuditForward>({ enabled: false, host: null, port: 12201, protocol: "udp", fmt: "gelf" });
const afSaving = ref(false);
const afTesting = ref(false);
const afProtoOpts = [{ label: "UDP", value: "udp" }, { label: "TCP", value: "tcp" }];
const afFmtOpts = [{ label: "GELF", value: "gelf" }, { label: "Syslog (RFC5424)", value: "syslog" }, { label: "CEF", value: "cef" }];
async function loadAf() { try { af.value = await getAuditForward(); } catch { /* ignore */ } }
async function saveAf() {
  afSaving.value = true;
  try { af.value = await putAuditForward(af.value); msg.success(t("common.saved")); }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.network")); } finally { afSaving.value = false; }
}
async function doTestAf() {
  if (!af.value.host) { msg.warning(t("settings.system.af_need_host")); return; }
  afTesting.value = true;
  try { const r = await testAuditForward(af.value); msg.success(`${t("settings.system.af_test_ok")} — ${r.sent_to}`); }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("settings.system.af_test_fail")); } finally { afTesting.value = false; }
}

onMounted(() => {
  void loadRackEmbed();
  getUiDisplay().then((d) => { changeLogDimDays.value = d.change_log_dim_days; }).catch(() => {});
  getConsoleSecurity().then((c) => { rdpClipPaste.value = c.rdp_clipboard_paste; }).catch(() => {});
  getMapProvider().then((p) => { mapProvider.value = p; }).catch(() => {});
  getRackNameAlign().then((a) => { rackAlign.value = a; }).catch(() => {});
  getOnlineGrace().then((c) => {
    grace.value = c.minutes; livenessSrc.value = c.sources; livenessAvail.value = c.available;
  })
    .catch(() => {});
  getIpCooldown().then((d) => { cooldownDays.value = d; }).catch(() => {});
  getCertExpiryDays().then((d) => { certExpiryDays.value = d; }).catch(() => {});
  void loadGeoip();
  void loadLdap();
  void loadLdapGroups();
  void loadOidc();
  void loadSaml();
  void loadAf();
  void loadAutolink();
});

// ── 依網卡 MAC 自動掛裝置（預設關閉）
const autolink = ref<AutolinkConfig>({ enabled: false, scope_subnet_ids: null });
const autolinkPreview = ref<AutolinkPreview | null>(null);
const autolinkBusy = ref(false);
const subnetOptions = ref<{ label: string; value: string }[]>([]);

async function loadAutolink() {
  try {
    autolink.value = await getAutolink();
    const subs = await listSubnets({ pageSize: 500 });
    const rows = Array.isArray(subs) ? subs : (subs as any).items ?? [];
    subnetOptions.value = rows.map((x: any) => ({ label: x.cidr, value: x.id }));
  } catch { /* 沒權限或尚未設定：維持預設關閉 */ }
}

async function saveAutolink(patch: Partial<AutolinkConfig>) {
  try {
    autolink.value = await putAutolink(patch);
    msg.success(t("common.saved"));
  } catch (e) { msg.error(apiErrMsg(e)); }
}

async function doPreviewAutolink() {
  autolinkBusy.value = true;
  try { autolinkPreview.value = await previewAutolink(); }
  catch (e) { msg.error(apiErrMsg(e)); }
  finally { autolinkBusy.value = false; }
}
</script>

<template>
  <div class="ss-page">
    <div class="ss-title">
      <n-icon :size="22"><AdminIcon /></n-icon>
      <span>{{ t("system_settings.title") }}</span>
    </div>
    <div class="ss-wrap">
      <!-- 資安：連線管理 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("system_settings.grp_security") }}</span></template>
        <div class="ss-grid">
          <div class="fld">
            <label>{{ t("settings.system.rdp_clip_paste") }}</label>
            <n-switch :value="rdpClipPaste" @update:value="changeRdpClipPaste" />
            <div class="hint">{{ t("settings.system.rdp_clip_paste_hint") }}</div>
          </div>
        </div>
      </n-card>

      <!-- 顯示與地圖 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("system_settings.grp_display") }}</span></template>
        <div class="ss-grid">
          <div class="fld">
            <label>{{ t("settings.system.map_provider") }}</label>
            <n-select :value="mapProvider" :options="mapProviderOpts" @update:value="changeMapProvider" />
            <div class="hint">{{ t("settings.system.map_provider_hint") }}</div>
          </div>
          <div class="fld">
            <label>{{ t("settings.system.rack_name_align") }}</label>
            <n-select :value="rackAlign" :options="rackAlignOpts" @update:value="changeRackAlign" />
            <div class="hint">{{ t("settings.system.rack_name_align_hint") }}</div>
          </div>
          <div class="fld">
            <label>{{ t("settings.system.change_log_dim_days") }}</label>
            <n-input-number :value="changeLogDimDays" :min="0" :max="3650" :step="1"
                            @update:value="changeDimDays" style="width: 160px" />
            <div class="hint">{{ t("settings.system.change_log_dim_days_hint") }}</div>
          </div>
        </div>
      </n-card>

      <!-- 上線判定 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("system_settings.grp_liveness") }}</span></template>
        <div class="fld fld-narrow-input">
          <label>{{ t("settings.prefs.online_grace_minutes") }}</label>
          <n-input-number :value="grace" :min="1" :max="43200" style="width: 100%" @update:value="changeGrace" />
          <div class="hint">{{ t("settings.prefs.online_grace_minutes_hint") }}</div>
        </div>
        <div class="fld" style="margin-top: 12px">
          <label>{{ t("system_settings.liveness_sources") }}</label>
          <!-- 依「證據種類」分組：同一種類的來源長度相近，網格才對得齊；
               混在一起自由換行會因為字數不一而每列參差（使用者回報）。 -->
          <n-checkbox-group :value="livenessSrc" @update:value="changeSources">
            <div v-for="g in livenessGroups" :key="g.kind" class="ss-src-row">
              <span class="ss-src-kind">{{ g.title }}</span>
              <div class="ss-src-grid">
                <n-checkbox v-for="s in g.items" :key="s.key" :value="s.key" class="ss-src-item">
                  <span class="ss-src-name">{{ g.kind === "base" ? srcLabel(s.key) : vendorLabel(s.key) }}</span>
                  <!-- 不會過期的證據勾了等於「看過一次就永遠上線」→ 講明白，不要只靠說明文字 -->
                  <n-tag v-if="!s.aging" size="tiny" :bordered="false" type="warning"
                         class="ss-src-tag">
                    {{ t("system_settings.src_no_aging") }}
                  </n-tag>
                </n-checkbox>
              </div>
            </div>
          </n-checkbox-group>
          <div class="ss-warn">
            <n-icon :size="15" :component="WarnIcon" />
            <span>{{ t("system_settings.liveness_no_aging_warn") }}</span>
          </div>
          <div class="hint">{{ t("system_settings.liveness_sources_hint") }}</div>
        </div>
      </n-card>

      <!-- IP 生命週期 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("system_settings.grp_lifecycle") }}</span></template>
        <div class="fld fld-narrow-input">
          <label>{{ t("system_settings.cooldown_days") }}</label>
          <n-input-number :value="cooldownDays" :min="0" :max="3650" style="width: 100%"
                          @update:value="changeCooldown" />
          <div class="hint">{{ t("system_settings.cooldown_days_hint") }}</div>
        </div>
      </n-card>

      <!-- 憑證到期通知 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("system_settings.grp_cert_alert") }}</span></template>
        <div class="fld fld-narrow-input">
          <label>{{ t("system_settings.cert_expiry_days") }}</label>
          <n-input-number :value="certExpiryDays" :min="1" :max="365" style="width: 100%"
                          @update:value="changeCertExpiry" />
          <div class="hint">{{ t("system_settings.cert_expiry_days_hint") }}</div>
        </div>
      </n-card>

      <!-- 機櫃圖對外嵌入 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("system_settings.grp_rack_embed") }}</span></template>
        <div class="ss-grid">
          <div class="fld">
            <label>{{ t("system_settings.rack_embed_enabled") }}</label>
            <n-switch :value="rackEmbed?.enabled ?? false" @update:value="changeRackEmbed" />
            <div class="hint">{{ t("system_settings.rack_embed_hint") }}</div>
          </div>
          <div v-if="rackEmbed?.enabled" class="fld">
            <label>{{ t("system_settings.rack_embed_token") }}</label>
            <n-input :value="rackEmbedToken" readonly
                     :type="showRackToken ? 'text' : 'password'" style="width: 100%" />
            <n-space size="small" style="margin-top: 6px">
              <n-button size="small" @click="showRackToken = !showRackToken">
                {{ showRackToken ? t("common.hide") : t("common.show") }}
              </n-button>
              <n-button size="small" @click="copyRackToken">{{ t("common.copy") }}</n-button>
              <n-popconfirm @positive-click="regenRackToken">
                <template #trigger>
                  <n-button size="small" type="warning" ghost>
                    {{ t("system_settings.rack_embed_regen") }}
                  </n-button>
                </template>
                {{ t("system_settings.rack_embed_regen_confirm") }}
              </n-popconfirm>
            </n-space>
            <div class="hint">{{ t("system_settings.rack_embed_token_hint") }}</div>
          </div>
        </div>
      </n-card>

      <!-- GeoIP -->
      <n-card v-if="geoip" class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("settings.system.geoip") }}</span></template>
        <div class="ss-grid">
          <div class="fld">
            <label>{{ t("settings.system.geoip_account") }}</label>
            <n-input v-model:value="geoipAccount" :placeholder="t('settings.system.geoip_account')" />
          </div>
          <div class="fld">
            <label>License Key</label>
            <n-input v-model:value="geoipKey" type="password" show-password-on="click"
                     :placeholder="geoip.has_key ? t('settings.system.geoip_key_set') : t('settings.system.geoip_key')" />
          </div>
        </div>
        <div class="fld" style="margin-top: 14px">
          <label>{{ t("settings.system.geoip_editions") }}</label>
          <n-select v-model:value="geoip.editions" :options="geoipEditionOpts" multiple />
          <div class="hint" style="line-height:1.5">{{ t("settings.system.geoip_asn_note") }}</div>
        </div>
        <div class="ss-row">
          <div style="display:flex; align-items:center; gap:8px">
            <n-switch v-model:value="geoip.auto_update" />
            <span style="font-size:13px">{{ t("settings.system.geoip_auto") }}</span>
          </div>
          <n-select v-model:value="geoip.frequency" :options="geoipFreqOpts" :disabled="!geoip.auto_update"
                    style="width: 200px" />
          <div style="flex:1"></div>
          <n-button size="small" :loading="geoipSaving" @click="saveGeoip">
            <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
          </n-button>
          <n-button size="small" type="primary" :loading="geoipUpdating" @click="updateGeoipNow">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("settings.system.geoip_update_now") }}
          </n-button>
        </div>
        <div class="ss-status">
          <div v-for="d in geoip.dbs" :key="d.edition" class="db-row">
            <n-tag size="tiny" :type="d.present ? 'success' : 'default'">{{ d.edition }}</n-tag>
            <span v-if="d.present" style="opacity:.7">{{ fmtBytes(d.size) }} · {{ fmtRelative(d.built_at) }}</span>
            <span v-else style="opacity:.5">{{ t("settings.system.geoip_db_missing") }}</span>
          </div>
          <div v-if="geoip.last_update_at" style="opacity:.6; margin-top:4px">
            {{ t("settings.system.geoip_last_update") }}: {{ fmtDateTime(geoip.last_update_at) }}
          </div>
          <div v-if="geoip.last_error" style="color:var(--err-color,#e88080); margin-top:4px">{{ geoip.last_error }}</div>
        </div>
        <div class="hint" style="line-height:1.6; margin-top:10px">
          {{ t("settings.system.geoip_hint") }}<br>
          {{ t("settings.system.geoip_freq_advice") }}
        </div>
      </n-card>

      <!-- 外部認證 / LDAP（AD） -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("settings.system.ldap_title") }}</span></template>
        <div class="fld">
          <n-space align="center">
            <n-switch v-model:value="ldap.enabled" />
            <span style="font-size:13px">{{ t("settings.system.ldap_enable") }}</span>
          </n-space>
        </div>
        <div style="display:grid; grid-template-columns:1fr 140px; gap:12px">
          <div class="fld">
            <label>{{ t("settings.system.ldap_server") }}</label>
            <n-input v-model:value="ldap.server" placeholder="dc01.example.com" />
          </div>
          <div class="fld">
            <label>{{ t("settings.system.ldap_port") }}</label>
            <n-input-number v-model:value="ldap.port" :min="1" :max="65535" style="width:100%" />
          </div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_tls") }}</label>
          <n-select v-model:value="ldapTlsMode" :options="ldapTlsOpts" />
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_bind_dn") }}</label>
          <n-input v-model:value="ldap.bind_dn" placeholder="CN=svc-ipam,OU=Svc,DC=example,DC=com" />
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_bind_pw") }}</label>
          <div style="display:flex; gap:8px; align-items:center">
            <n-input v-model:value="ldapPw" type="password" show-password-on="click"
                     :placeholder="ldap.password_set ? t('settings.system.ldap_pw_set') : t('settings.system.ldap_pw_unset')"
                     style="flex:1" />
            <n-button v-if="ldap.password_set" size="small" quaternary type="error" @click="clearLdapPw">
              {{ t("settings.system.ldap_pw_clear") }}
            </n-button>
          </div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_search_base") }}</label>
          <n-input v-model:value="ldap.search_base" placeholder="DC=example,DC=com" />
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_user_filter") }}</label>
          <n-input v-model:value="ldap.user_filter" placeholder="(sAMAccountName={username})" />
          <div class="hint" style="margin-top:4px">{{ t("settings.system.ldap_user_filter_hint") }}</div>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px">
          <div class="fld">
            <label>{{ t("settings.system.ldap_attr_email") }}</label>
            <n-input v-model:value="ldap.attr_email" placeholder="mail" />
          </div>
          <div class="fld">
            <label>{{ t("settings.system.ldap_attr_name") }}</label>
            <n-input v-model:value="ldap.attr_display_name" placeholder="displayName" />
          </div>
          <div class="fld">
            <label>{{ t("settings.system.ldap_attr_groups") }}</label>
            <n-input v-model:value="ldap.attr_member_of" placeholder="memberOf" />
          </div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_admin_groups") }}</label>
          <n-input v-model:value="ldapGroupsText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }"
                   placeholder="CN=IPAM-Admins,OU=Groups,DC=example,DC=com" />
          <div class="hint" style="margin-top:4px">{{ t("settings.system.ldap_admin_groups_hint") }}</div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.ldap_default_role") }}</label>
          <n-select v-model:value="ldapDefaultGroup" :options="ldapGroupOpts" />
          <div class="hint" style="margin-top:4px">{{ t("settings.system.ldap_default_role_hint") }}</div>
        </div>
        <n-space style="margin-top:6px">
          <n-button type="primary" :loading="ldapSaving" @click="saveLdap">
            <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
          </n-button>
          <n-button :loading="ldapTesting" @click="doTestLdap">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("settings.system.ldap_test") }}
          </n-button>
        </n-space>
        <div class="fld" style="margin-top:14px; border-top:1px dashed var(--n-border-color,#eee); padding-top:12px">
          <label>{{ t("settings.system.ldap_authtest") }}</label>
          <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
            <n-input v-model:value="ldapTestUser" :placeholder="t('settings.system.ldap_authtest_user')" style="flex:1; min-width:140px" />
            <n-input v-model:value="ldapTestPw" type="password" show-password-on="click" :placeholder="t('settings.system.ldap_authtest_pw')" style="flex:1; min-width:140px" @keyup.enter="doTestLdapAuth" />
            <n-button :loading="ldapAuthTesting" @click="doTestLdapAuth">
              <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("settings.system.ldap_authtest_btn") }}
            </n-button>
          </div>
          <div class="hint" style="margin-top:4px">{{ t("settings.system.ldap_authtest_hint") }}</div>
        </div>
        <div class="hint" style="line-height:1.6; margin-top:10px">{{ t("settings.system.ldap_hint") }}</div>
      </n-card>

      <!-- 單一登入 (OIDC) -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("settings.system.oidc_title") }}</span></template>
        <div class="fld">
          <n-space align="center">
            <n-switch v-model:value="oidc.enabled" />
            <span style="font-size:13px">{{ t("settings.system.oidc_enable") }}</span>
          </n-space>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.oidc_issuer") }}</label>
          <n-input v-model:value="oidc.issuer" placeholder="https://idp.example.com/realms/main" />
        </div>
        <div class="fld" style="display:flex; gap:12px; flex-wrap:wrap">
          <div style="flex:1; min-width:200px">
            <label>{{ t("settings.system.oidc_client_id") }}</label>
            <n-input v-model:value="oidc.client_id" />
          </div>
          <div style="flex:1; min-width:200px">
            <label>{{ t("settings.system.oidc_client_secret") }}</label>
            <n-input v-model:value="oidcSecret" type="password" show-password-on="click"
                     :placeholder="oidc.client_secret_set ? t('settings.system.oidc_secret_keep') : ''" />
          </div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.oidc_redirect_uri") }}</label>
          <n-input v-model:value="oidc.redirect_uri" placeholder="https://ipam.example.com/api/v1/auth/oidc/callback" />
        </div>
        <div class="fld" style="display:flex; gap:12px; flex-wrap:wrap">
          <div style="flex:1; min-width:160px">
            <label>{{ t("settings.system.oidc_scope") }}</label>
            <n-input v-model:value="oidc.scope" />
          </div>
          <div style="flex:1; min-width:140px">
            <label>{{ t("settings.system.oidc_username_claim") }}</label>
            <n-input v-model:value="oidc.username_claim" />
          </div>
          <div style="flex:1; min-width:140px">
            <label>{{ t("settings.system.oidc_groups_claim") }}</label>
            <n-input v-model:value="oidc.groups_claim" />
          </div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.oidc_admin_groups") }}</label>
          <n-input v-model:value="oidcAdminGroupsText" :placeholder="t('settings.system.oidc_admin_groups_ph')" />
        </div>
        <n-space style="margin-top:8px">
          <n-button type="primary" :loading="oidcSaving" @click="saveOidc">
            <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
          </n-button>
          <n-button :loading="oidcTesting" @click="doTestOidc">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("settings.system.oidc_test") }}
          </n-button>
        </n-space>
        <div class="hint" style="line-height:1.6; margin-top:10px">{{ t("settings.system.oidc_hint") }}</div>
      </n-card>

      <!-- 單一登入 (SAML 2.0) -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("settings.system.saml_title") }}</span></template>
        <div class="fld">
          <n-space align="center">
            <n-switch v-model:value="saml.enabled" />
            <span style="font-size:13px">{{ t("settings.system.saml_enable") }}</span>
          </n-space>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.saml_idp_metadata_url") }}</label>
          <n-input v-model:value="saml.idp_metadata_url" placeholder="https://idp.example.com/metadata" />
        </div>
        <div class="fld">
          <label>{{ t("settings.system.saml_idp_metadata_xml") }}</label>
          <n-input v-model:value="saml.idp_metadata_xml" type="textarea" :rows="3"
                   :placeholder="t('settings.system.saml_idp_metadata_xml_ph')" />
        </div>
        <div class="fld" style="display:flex; gap:12px; flex-wrap:wrap">
          <div style="flex:1; min-width:200px">
            <label>{{ t("settings.system.saml_sp_entity_id") }}</label>
            <n-input v-model:value="saml.sp_entity_id" :placeholder="t('settings.system.saml_auto_ph')" />
          </div>
          <div style="flex:1; min-width:200px">
            <label>{{ t("settings.system.saml_sp_acs_url") }}</label>
            <n-input v-model:value="saml.sp_acs_url" :placeholder="t('settings.system.saml_auto_ph')" />
          </div>
        </div>
        <div class="fld" style="display:flex; gap:12px; flex-wrap:wrap">
          <div style="flex:1; min-width:160px">
            <label>{{ t("settings.system.oidc_username_claim") }}</label>
            <n-input v-model:value="saml.attr_username" />
          </div>
          <div style="flex:1; min-width:160px">
            <label>{{ t("cols.email") }}</label>
            <n-input v-model:value="saml.attr_email" />
          </div>
          <div style="flex:1; min-width:160px">
            <label>{{ t("settings.system.oidc_groups_claim") }}</label>
            <n-input v-model:value="saml.attr_groups" />
          </div>
        </div>
        <div class="fld">
          <label>{{ t("settings.system.oidc_admin_groups") }}</label>
          <n-input v-model:value="samlAdminGroupsText" :placeholder="t('settings.system.oidc_admin_groups_ph')" />
        </div>
        <n-space align="center" style="margin-bottom:10px">
          <n-checkbox v-model:checked="saml.want_assertions_signed">{{ t("settings.system.saml_want_signed") }}</n-checkbox>
          <n-checkbox v-model:checked="saml.authn_requests_signed">{{ t("settings.system.saml_authn_signed") }}</n-checkbox>
          <n-checkbox v-model:checked="saml.want_assertions_encrypted">{{ t("settings.system.saml_want_encrypted") }}</n-checkbox>
        </n-space>
        <n-space>
          <n-button type="primary" :loading="samlSaving" @click="saveSaml">
            <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
          </n-button>
          <n-button :loading="samlTesting" @click="doTestSaml">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("settings.system.saml_test") }}
          </n-button>
          <a :href="`${origin}/api/v1/auth/saml/metadata`" target="_blank" rel="noopener"
             style="font-size:13px; align-self:center">{{ t("settings.system.saml_sp_metadata") }}</a>
        </n-space>
        <div class="hint" style="line-height:1.6; margin-top:10px">{{ t("settings.system.saml_hint") }}</div>
      </n-card>

      <!-- 稽核轉送到 Graylog -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("settings.system.af_title") }}</span></template>
        <div class="fld">
          <n-space align="center">
            <n-switch v-model:value="af.enabled" />
            <span style="font-size:13px">{{ t("settings.system.af_enable") }}</span>
          </n-space>
        </div>
        <div style="display:grid; grid-template-columns:1fr 130px; gap:12px">
          <div class="fld">
            <label>{{ t("settings.system.af_host") }}</label>
            <n-input v-model:value="af.host" placeholder="graylog.example.com" />
          </div>
          <div class="fld">
            <label>{{ t("settings.system.af_port") }}</label>
            <n-input-number v-model:value="af.port" :min="1" :max="65535" style="width:100%" />
          </div>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px">
          <div class="fld">
            <label>{{ t("settings.system.af_protocol") }}</label>
            <n-select v-model:value="af.protocol" :options="afProtoOpts" />
          </div>
          <div class="fld">
            <label>{{ t("settings.system.af_format") }}</label>
            <n-select v-model:value="af.fmt" :options="afFmtOpts" />
          </div>
        </div>
        <n-space style="margin-top:6px">
          <n-button type="primary" :loading="afSaving" @click="saveAf">
            <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
          </n-button>
          <n-button :loading="afTesting" @click="doTestAf">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("settings.system.af_test") }}
          </n-button>
        </n-space>
        <div class="hint" style="line-height:1.6; margin-top:10px">{{ t("settings.system.af_hint") }}</div>
      </n-card>
      <!-- 依網卡 MAC 自動掛裝置。預設關閉：升級之後突然多出一個每 5 分鐘自動改
           資料的作業，本身就是不該發生的事。開啟前可以先預覽會動到什麼。 -->
      <n-card class="ss-group" size="small">
        <template #header><span class="ss-h">{{ t("autolink.title") }}</span></template>
        <div class="ss-grid">
          <div class="fld">
            <label>{{ t("autolink.enable") }}</label>
            <n-switch :value="autolink.enabled"
                      @update:value="(v: boolean) => saveAutolink({ enabled: v })" />
            <div class="hint">{{ t("autolink.enable_hint") }}</div>
          </div>
          <div class="fld">
            <label>{{ t("autolink.scope") }}</label>
            <n-select :value="autolink.scope_subnet_ids ?? []" multiple filterable
                      :options="subnetOptions" :placeholder="t('autolink.scope_all')"
                      @update:value="(v: string[]) => saveAutolink({ scope_subnet_ids: v })" />
            <div class="hint">{{ t("autolink.scope_hint") }}</div>
          </div>
        </div>
        <n-space align="center" style="margin-top:10px">
          <n-button size="small" :loading="autolinkBusy" @click="doPreviewAutolink">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>
            {{ t("autolink.preview") }}
          </n-button>
          <span v-if="autolinkPreview" class="hint" style="margin:0">
            {{ t("autolink.preview_result", { n: autolinkPreview.would_link }) }}
            <template v-if="Object.values(autolinkPreview.skipped).some((x) => x > 0)">
              · {{ t("autolink.preview_skipped", {
                    s: Object.entries(autolinkPreview.skipped)
                         .filter(([, v]) => v > 0)
                         .map(([k, v]) => `${t("autolink.skip_" + k)} ${v}`).join("、") }) }}
            </template>
          </span>
        </n-space>
        <div v-if="autolinkPreview?.samples?.length" class="hint" style="margin-top:6px">
          <div v-for="s in autolinkPreview.samples.slice(0, 8)" :key="s.ip">
            {{ s.ip }}<template v-if="s.hostname"> ({{ s.hostname }})</template> → {{ s.device }}
          </div>
          <div v-if="autolinkPreview.samples.length > 8">…</div>
        </div>
        <div class="hint" style="line-height:1.6; margin-top:10px">{{ t("autolink.rules_hint") }}</div>
      </n-card>
    </div>
  </div>
</template>

<style scoped>
/* 每個分類各自一張卡片，寬度撐滿內容區（原本全部擠在一張大卡片、右側留白） */
.ss-page { display: flex; flex-direction: column; gap: 16px; }
.ss-title { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 600;
  padding: 2px 2px 0; }
.ss-wrap { display: flex; flex-direction: column; gap: 16px; max-width: none; }
/* 卡片外觀（底色／邊框／深色模式）交給 n-card，不要自己刻 —— 自己刻就會與其他頁面不一致 */
.ss-group { border-radius: 14px; }
.ss-h { display: inline-block; font-size: 16px; font-weight: 700; padding-left: 12px;
  line-height: 1.25; border-left: 4px solid #18a058; }
/* 統一卡片內的垂直節奏。
   原本只對「卡片內容的直接子元素」下間距，但很多區塊是包在 grid / div 裡的，
   於是開關與底下的欄位就貼在一起（使用者回報：轉送稽核記錄那格最明顯）。
   這裡改成三條規則一起管：卡片第一層、相鄰的欄位、以及開關列與後面的東西。 */
/* 卡片內「直向堆疊」的區塊之間留白。
   ⚠️ 間距只能加在**直向容器**上，不可以用「相鄰兄弟就補上邊距」——
   欄位並排時（.ss-grid 的兩、三欄）那條規則會把右邊那欄整個往下推 14px，
   看起來就是左右沒對齊（使用者在「顯示與地圖」與 GeoIP 兩處都抓到）。 */
.ss-group :deep(.n-card__content) > * + * { margin-top: 16px; }
/* 直向排列的容器用 gap，橫向排列的（.ss-grid）由它自己的 gap 負責 */
.ss-group :deep(.n-card__content) { display: flex; flex-direction: column; }
.ss-group :deep(.n-card__content) > .fld + .fld { margin-top: 14px; }
/* 開關列之後一定要留白：開關本身沒有下邊界，視覺上會黏住下一個欄位的標題 */
.ss-group :deep(.n-switch) { margin-bottom: 2px; }
.ss-group :deep(.fld > .n-space) { margin-bottom: 6px; }
/* 按鈕列與底下的說明文字 */
.ss-group :deep(.n-button + .hint),
.ss-group :deep(.ss-row + .hint) { margin-top: 10px; }
.ss-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
/* 只有一個欄位時就別佔半格：右半邊空著、說明卻提早換行（使用者回報）。 */
.ss-grid > :only-child { grid-column: 1 / -1; }
/* 寬螢幕改三欄，把右邊的空間用掉 */
@media (min-width: 1500px) { .ss-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 640px) { .ss-grid { grid-template-columns: 1fr; } }
/* 單一數字欄位：只把輸入框收窄，說明文字仍用滿卡片寬度。
   先前是把整格限成 320px，說明被擠成窄長一條、右邊整片空白（使用者回報）。 */
.ss-src-row { display: flex; align-items: flex-start; gap: 10px; padding: 12px 0; }
/* 種類之間畫一條淡線：只靠左邊的標題分辨，兩組黏在一起時看起來像同一堆 */
.ss-src-row + .ss-src-row { border-top: 1px solid var(--n-border-color, rgba(127, 127, 127, .18)); }
.ss-src-kind {
  flex: 0 0 96px; font-size: 12.5px; color: var(--n-text-color-3, #8a8a8a);
  line-height: 22px; text-align: right; font-weight: 500;
}
/* 固定欄寬的網格：每一格一樣寬，列與列才對得齊 */
.ss-src-grid {
  /* ⚠️ `min-width: 0` 不能省：flex 子項預設 min-width:auto，撐不小於內容的最小寬度，
     視窗一窄格線就整片衝出卡片外（實測 820px 視窗下超出 195px）。 */
  /* `width: 100%` 是關鍵：只給 flex 屬性時，格線的寬度仍會由內容決定（欄數 × 最小欄寬），
     視窗一窄就整片衝出卡片外。明確綁定容器寬度之後，欄數才會跟著縮。 */
  flex: 1 1 auto; min-width: 0; width: 100%; display: grid; gap: 8px 14px;
  grid-template-columns: repeat(auto-fit, minmax(min(200px, 100%), 1fr)); max-width: 940px;
}
/* 選項本身也要能縮：長標籤（Wazuh 代理 keep-alive）在窄欄位裡要換行而不是撐開格線 */
.ss-src-item { min-width: 0; }
.ss-src-item :deep(.n-checkbox__label) { white-space: normal; }
.ss-src-item { line-height: 22px; min-width: 0; }
.ss-src-name { vertical-align: middle; }
.ss-src-tag { margin-left: 4px; vertical-align: middle; }
@media (max-width: 900px) {
  .ss-src-row { flex-direction: column; gap: 2px; }
  .ss-src-kind { text-align: left; flex-basis: auto; }
}
.fld-narrow-input :deep(.n-input-number) { max-width: 260px; }
.fld label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 5px; }
.hint { font-size: 11px; opacity: 0.65; margin-top: 4px; }
/* 「不會過期」是會讓判定失真的選項 —— 從灰色說明裡拉出來，別讓它埋在一整段字裡 */
.ss-warn {
  display: flex; align-items: flex-start; gap: 6px; margin-top: 10px;
  padding: 8px 10px; border-radius: 8px; font-size: 12.5px; line-height: 1.6;
  color: #a2680a; background: rgba(240, 160, 32, 0.12);
  border: 1px solid rgba(240, 160, 32, 0.35);
}
.ss-row { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.ss-status { margin-top: 12px; font-size: 12px; display: flex; flex-direction: column; gap: 3px; }
.db-row { display: flex; gap: 8px; align-items: center; }
</style>
