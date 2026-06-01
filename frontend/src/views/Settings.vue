<script setup lang="ts">
/**
 * 使用者設定頁。
 *
 * 比 phpIPAM 改進：
 *  - 三個 tab，每個 tab 不超過 ~5-7 個選項，不堆一頁
 *  - TOTP 啟用流程內嵌 SVG QR code(不要逼使用者貼 URI)
 *  - Preferences 即時儲存到 /api/v1/me/preferences，不需手動按 save
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NCard,
  NTabs,
  NTabPane,
  NSpace,
  NDescriptions,
  NDescriptionsItem,
  NSelect,
  NInputNumber,
  NInput,
  NButton,
  NAlert,
  NCode,
  NPopconfirm,
  NSwitch,
  NTag,
  useMessage,
} from "naive-ui";
import { NIcon } from "naive-ui";
import { fmtDateTime, fmtRelative } from "@/utils/datetime";
import { SettingsIcon, UsersIcon, LockIcon, LocationsIcon } from "@/icons";
import { getMapProvider, setMapProvider, getRackNameAlign, setRackNameAlign, getGeoipConfig, setGeoipConfig, updateGeoipDbNow, type GeoIPConfig, type RackNameAlign } from "@/api/basic";
import QRCode from "qrcode";
import { storeToRefs } from "pinia";
import { useAuthStore } from "@/stores/auth";
import { useUiStore } from "@/stores/ui";
import {
  getPreferences,
  updatePreferences,
} from "@/api/preferences";
import {
  type UserPreferences,
} from "@/api/preferences";
import * as totpApi from "@/api/totp";

const { t } = useI18n();
const auth = useAuthStore();
const ui = useUiStore();
const { me } = storeToRefs(auth);
const msg = useMessage();

// ── Preferences ──
const prefs = ref<UserPreferences | null>(null);
const prefsLoading = ref(false);

async function loadPrefs() {
  prefsLoading.value = true;
  try {
    prefs.value = await getPreferences();
    // locale 同步到 ui store；theme 不在這裡覆寫——以 ui store(localStorage) 為準，
    // 否則使用者剛在右上切換的佈景會在開設定頁時被後端舊值蓋回去。
    ui.setLocale(prefs.value.locale);
  } catch {
    msg.error(t("errors.network"));
  } finally {
    prefsLoading.value = false;
  }
}

async function patchPref<K extends keyof UserPreferences>(
  key: K,
  value: UserPreferences[K],
) {
  if (!prefs.value) return;
  prefs.value[key] = value;
  try {
    const updated = await updatePreferences({ [key]: value } as Partial<UserPreferences>);
    prefs.value = updated;
    if (key === "locale") ui.setLocale(value as "zh-TW" | "en-US");
    if (key === "theme") ui.setTheme(value as "light" | "dark" | "auto");
  } catch {
    msg.error(t("errors.network"));
  }
}

// ── System（admin only）：全域地圖供應商 ──
const mapProvider = ref<"osm" | "google">("osm");
const mapProviderOpts = [
  { label: "OpenStreetMap", value: "osm" },
  { label: "Google Maps", value: "google" },
];
async function changeMapProvider(p: "osm" | "google") {
  mapProvider.value = p;
  try {
    await setMapProvider(p);
    msg.success(t("common.ok"));
  } catch {
    msg.error(t("errors.network"));
  }
}

// ── 機櫃示意圖：裝置名稱對齊（全域）──
const rackAlign = ref<RackNameAlign>("left");
const rackAlignOpts = computed(() => [
  { label: t("settings.system.align_left"), value: "left" },
  { label: t("settings.system.align_center"), value: "center" },
  { label: t("settings.system.align_right"), value: "right" },
]);
async function changeRackAlign(a: RackNameAlign) {
  rackAlign.value = a;
  try { await setRackNameAlign(a); msg.success(t("common.ok")); }
  catch { msg.error(t("errors.network")); }
}

// ── GeoIP（MaxMind 本地 mmdb + 排程更新）──
const geoip = ref<GeoIPConfig | null>(null);
const geoipAccount = ref("");
const geoipKey = ref("");
const geoipSaving = ref(false);
const geoipUpdating = ref(false);
const geoipEditionOpts = computed(() => (geoip.value?.all_editions ?? []).map((e) => ({ label: e, value: e })));
const geoipFreqOpts = computed(() => (geoip.value?.frequencies ?? []).map((f) => ({ label: t(`settings.system.freq_${f.replace("-", "_")}`), value: f })));
async function loadGeoip() {
  try {
    geoip.value = await getGeoipConfig();
    geoipAccount.value = geoip.value.account_id ?? "";
  } catch { /* ignore */ }
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
  } catch {
    msg.error(t("errors.network"));
  } finally {
    geoipSaving.value = false;
  }
}
async function updateGeoipNow() {
  geoipUpdating.value = true;
  try {
    const r = await updateGeoipDbNow();
    geoip.value = r.config;
    if (r.result?.error === "not_configured") msg.warning(t("settings.system.geoip_need_creds"));
    else msg.success(t("settings.system.geoip_updated"));
  } catch {
    msg.error(t("errors.network"));
  } finally {
    geoipUpdating.value = false;
  }
}
function fmtBytes(n: number | null): string {
  if (!n) return "—";
  return n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : (n / 1e3).toFixed(0) + " KB";
}

// ── TOTP enrollment ──
const enrollment = ref<{ secret: string; otpauth_uri: string } | null>(null);
const qrSvg = ref<string>("");
const code = ref("");
const totpBusy = ref(false);

async function startEnroll() {
  totpBusy.value = true;
  try {
    enrollment.value = await totpApi.enroll();
    qrSvg.value = await QRCode.toString(enrollment.value.otpauth_uri, {
      type: "svg",
      margin: 1,
      width: 200,
      errorCorrectionLevel: "M",
    });
  } catch {
    msg.error(t("errors.network"));
  } finally {
    totpBusy.value = false;
  }
}

async function confirmEnroll() {
  if (!enrollment.value || !code.value) return;
  totpBusy.value = true;
  try {
    await totpApi.confirm(enrollment.value.secret, code.value);
    enrollment.value = null;
    qrSvg.value = "";
    code.value = "";
    await auth.fetchMe();
    msg.success(t("settings.security.totp_enabled_msg"));
  } catch (e: any) {
    msg.error(e?.response?.data?.detail ?? t("settings.security.totp_invalid"));
  } finally {
    totpBusy.value = false;
  }
}

async function disableTotp() {
  totpBusy.value = true;
  try {
    await totpApi.disable();
    await auth.fetchMe();
    msg.success(t("settings.security.totp_disabled_msg"));
  } catch {
    msg.error(t("errors.network"));
  } finally {
    totpBusy.value = false;
  }
}

function cancelEnroll() {
  enrollment.value = null;
  qrSvg.value = "";
  code.value = "";
}

const localeOptions = [
  { label: "繁體中文", value: "zh-TW" },
  { label: "English", value: "en-US" },
];
const themeOptions = computed(() => [
  { label: t("settings.prefs.theme_light"), value: "light" },
  { label: t("settings.prefs.theme_dark"),  value: "dark"  },
  { label: t("settings.prefs.theme_auto"),  value: "auto"  },
]);
const calendarOptions = computed(() => [
  { label: t("settings.prefs.calendar_gregorian"), value: "gregorian" },
  { label: t("settings.prefs.calendar_minguo"),    value: "minguo"    },
]);

// 是否啟用 TOTP — me 物件目前未含此欄位；若沒有 enrollment 就視為「未啟用 / 已啟用」皆可，
// 但我們以「server 接受 disable」為依據；先用 confirm 失敗作 fallback。
// (Phase 1.5 在 /me 增加 totp_enabled 欄位)
const totpStateUnknown = ref(true);

onMounted(() => {
  void loadPrefs();
  if (me.value?.is_admin) {
    getMapProvider().then((p) => { mapProvider.value = p; }).catch(() => {});
    getRackNameAlign().then((a) => { rackAlign.value = a; }).catch(() => {});
    void loadGeoip();
  }
});
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><SettingsIcon /></n-icon>
        <span>{{ t("settings.title") }}</span>
      </n-space>
    </template>
    <n-tabs type="line" default-value="profile">
      <!-- Profile -->
      <n-tab-pane name="profile">
        <template #tab>
          <span style="display:inline-flex;align-items:center;gap:6px"><n-icon :size="16"><UsersIcon /></n-icon>{{ t('settings.profile.tab') }}</span>
        </template>
        <n-descriptions v-if="me" bordered :column="1" label-placement="left"
                        label-style="width: 160px">
          <n-descriptions-item :label="t('settings.profile.username')">{{ me.username }}</n-descriptions-item>
          <n-descriptions-item :label="t('settings.profile.email')">{{ me.email }}</n-descriptions-item>
          <n-descriptions-item :label="t('settings.profile.display_name')">
            {{ me.display_name ?? "—" }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('settings.profile.auth_provider')">{{ me.auth_provider }}</n-descriptions-item>
          <n-descriptions-item :label="t('settings.profile.admin')">
            {{ me.is_admin ? t("common.yes") : t("common.no") }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('settings.profile.last_login')">
            {{ me.last_login_at ?? "—" }}
          </n-descriptions-item>
        </n-descriptions>
      </n-tab-pane>

      <!-- Security: TOTP -->
      <n-tab-pane name="security">
        <template #tab>
          <span style="display:inline-flex;align-items:center;gap:6px"><n-icon :size="16"><LockIcon /></n-icon>{{ t('settings.security.tab') }}</span>
        </template>
        <n-space vertical :size="16">
          <n-alert type="info">
            <strong>{{ t("settings.security.totp_title") }}</strong>
            <span v-html="t('settings.security.totp_intro_html')"></span>
          </n-alert>

          <!-- 未在 enrollment 流程中：給「啟用 / 停用」按鈕 -->
          <n-space v-if="!enrollment">
            <n-button type="primary" :loading="totpBusy" @click="startEnroll">
              {{ t("settings.security.enable_totp") }}
            </n-button>
            <n-popconfirm @positive-click="disableTotp">
              <template #trigger>
                <n-button :loading="totpBusy">
                  {{ t("settings.security.disable_totp") }}
                </n-button>
              </template>
              {{ t("settings.security.disable_confirm") }}
            </n-popconfirm>
          </n-space>

          <!-- enrollment 流程中：顯示 QR + 驗證碼輸入 -->
          <div v-else>
            <n-space vertical :size="12">
              <strong>{{ t("settings.security.step1") }}</strong>
              <div v-html="qrSvg" class="qr"></div>
              <details>
                <summary>{{ t("settings.security.cannot_scan") }}</summary>
                <n-code :code="enrollment.otpauth_uri" language="plain" />
                <p style="font-size: 12px; opacity: 0.7">
                  Secret：<code>{{ enrollment.secret }}</code>
                </p>
              </details>

              <strong>{{ t("settings.security.step2") }}</strong>
              <n-space>
                <n-input
                  v-model:value="code"
                  placeholder="123456"
                  maxlength="6"
                  style="width: 160px"
                  @keyup.enter="confirmEnroll"
                />
                <n-button type="primary" :loading="totpBusy" @click="confirmEnroll">
                  {{ t("settings.security.confirm_enable") }}
                </n-button>
                <n-button @click="cancelEnroll">
                  {{ t("common.cancel") }}
                </n-button>
              </n-space>
            </n-space>
          </div>
        </n-space>
      </n-tab-pane>

      <!-- Preferences -->
      <n-tab-pane name="preferences">
        <template #tab>
          <span style="display:inline-flex;align-items:center;gap:6px"><n-icon :size="16"><SettingsIcon /></n-icon>{{ t('settings.prefs.tab') }}</span>
        </template>
        <n-space v-if="prefs" vertical :size="16" style="max-width: 480px">
          <div>
            <label>{{ t("settings.prefs.language") }}</label>
            <n-select
              :value="prefs.locale"
              :options="localeOptions"
              @update:value="(v: any) => patchPref('locale', v)"
            />
          </div>
          <div>
            <label>{{ t("settings.prefs.theme") }}</label>
            <n-select
              :value="ui.theme"
              :options="themeOptions"
              @update:value="(v: any) => patchPref('theme', v)"
            />
          </div>
          <div>
            <label>{{ t("settings.prefs.calendar") }}</label>
            <n-select
              :value="prefs.calendar"
              :options="calendarOptions"
              @update:value="(v: any) => patchPref('calendar', v)"
            />
          </div>
          <div>
            <label>{{ t("settings.prefs.timezone") }}</label>
            <n-input
              :value="prefs.timezone"
              placeholder="Asia/Taipei"
              @update:value="(v: any) => patchPref('timezone', v)"
            />
          </div>
          <div>
            <label>{{ t("settings.prefs.page_size") }}</label>
            <n-input-number
              :value="prefs.page_size"
              :min="10"
              :max="500"
              @update:value="(v: any) => patchPref('page_size', v)"
            />
          </div>
        </n-space>
        <p v-else style="opacity: 0.7">{{ t("common.loading") }}</p>
      </n-tab-pane>

      <!-- LLM (admin only) -->
    </n-tabs>
  </n-card>
</template>

<style scoped>
.qr {
  background: white;
  padding: 8px;
  border-radius: 4px;
  display: inline-block;
}
:deep(.qr svg) {
  display: block;
}
label {
  display: block;
  font-size: 12px;
  margin-bottom: 4px;
  opacity: 0.8;
}
</style>
