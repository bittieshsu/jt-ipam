<script setup lang="ts">
/**
 * SFTP 檔案瀏覽器：先換 ticket → 開 WebSocket → 後端橋接 asyncssh 的 SFTP。
 *
 * 與 SSH 終端機共用同一道權限閘門與同一個憑證金庫 —— 能開 SSH 的人本來就能在 shell 裡
 * 讀寫檔案，所以這裡不另設一套權限；但也絕不能比它鬆。
 *
 * 下載採「收完再存檔」：這個功能是給設定檔、憑證、log 片段用的，後端已把單檔上限訂在
 * 100 MB，收在記憶體再落地最單純。真要搬大檔請用 scp/rsync —— 把工具用在它擅長的地方，
 * 比在瀏覽器裡硬做串流落地實在。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  NAlert, NButton, NDataTable, NIcon, NInput, NInputNumber, NPopconfirm,
  NSelect, NSpace, NSpin, useMessage,
} from "naive-ui";
import type { DataTableColumns } from "naive-ui";
import { h } from "vue";
import {
  buildSshWsUrl, listSshCredentials, requestSftpTicket,
  type SftpEntry, type SshCredential,
} from "@/api/ssh";
import { fmtDateTime } from "@/utils/datetime";
import { RefreshIcon, PlusIcon } from "@/icons";

const props = defineProps<{ addressId: string; host: string }>();
const { t } = useI18n();
const msg = useMessage();

const connected = ref(false);
const connecting = ref(false);
const errorMsg = ref("");
const cwd = ref("/");
const entries = ref<SftpEntry[]>([]);
const truncated = ref(false);
const busy = ref(false);

// ── 連線設定（與 SSH 相同：已存憑證，或當次輸入）
const creds = ref<SshCredential[]>([]);
const form = ref({
  credential_id: null as string | null,
  username: "", port: 22, auth: "password" as "password" | "key",
  password: "", private_key: "", passphrase: "",
});

let ws: WebSocket | null = null;
/** 下載中的檔案：收到 file_begin 後開始累積二進位框，file_end 才落地。 */
let incoming: { name: string; size: number; chunks: Uint8Array[]; got: number } | null = null;
/** 等待中的請求：讓 send 之後可以 await 到結果。 */
let pending: { resolve: (v: any) => void; reject: (e: Error) => void } | null = null;

function send(obj: Record<string, unknown>) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

/** 送一個請求並等它的回覆（同一時間只會有一個 —— 這個介面是逐步操作的）。 */
function request(obj: Record<string, unknown>): Promise<any> {
  return new Promise((resolve, reject) => {
    pending = { resolve, reject };
    send(obj);
  });
}

async function connect() {
  errorMsg.value = "";
  connecting.value = true;
  try {
    const tk = await requestSftpTicket(props.addressId);
    ws = new WebSocket(buildSshWsUrl(tk.ws_path, tk.ticket));
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      const cfg: Record<string, unknown> = { type: "config", port: form.value.port };
      if (form.value.credential_id) {
        cfg.credential_id = form.value.credential_id;
      } else {
        cfg.username = form.value.username;
        cfg.auth = form.value.auth;
        if (form.value.auth === "password") cfg.password = form.value.password;
        else { cfg.private_key = form.value.private_key; cfg.passphrase = form.value.passphrase; }
      }
      send(cfg);
      // 明文不留在畫面狀態裡
      form.value.password = ""; form.value.private_key = ""; form.value.passphrase = "";
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") {
        if (incoming) {
          const u8 = new Uint8Array(ev.data as ArrayBuffer);
          incoming.chunks.push(u8);
          incoming.got += u8.byteLength;
        }
        return;
      }
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "ready":
          connected.value = true;
          connecting.value = false;
          cwd.value = m.cwd || "/";
          void refresh();
          break;
        case "list":
          cwd.value = m.path;
          entries.value = m.entries ?? [];
          truncated.value = !!m.truncated;
          pending?.resolve(m); pending = null;
          break;
        case "file_begin":
          incoming = { name: m.name, size: m.size, chunks: [], got: 0 };
          break;
        case "file_end": {
          if (incoming) {
            const blob = new Blob(incoming.chunks as BlobPart[]);
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = incoming.name;
            a.click();
            setTimeout(() => URL.revokeObjectURL(a.href), 4000);
            incoming = null;
          }
          pending?.resolve(m); pending = null;
          break;
        }
        case "put_ready":
          pending?.resolve(m); pending = null;
          break;
        case "ok":
          pending?.resolve(m); pending = null;
          break;
        case "error":
          errorMsg.value = m.message ?? "";
          connecting.value = false;
          pending?.reject(new Error(m.message)); pending = null;
          break;
      }
    };

    ws.onclose = () => {
      connected.value = false;
      connecting.value = false;
      pending?.reject(new Error(t("sftp.disconnected"))); pending = null;
    };
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail ?? String(e);
    connecting.value = false;
  }
}

async function refresh(path?: string) {
  busy.value = true;
  try { await request({ type: "list", path: path ?? cwd.value }); }
  catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

function enter(row: SftpEntry) {
  if (row.is_dir) void refresh(row.path);
}

function goUp() {
  const p = cwd.value.replace(/\/+$/, "");
  void refresh(p.slice(0, p.lastIndexOf("/")) || "/");
}

async function download(row: SftpEntry) {
  busy.value = true;
  try { await request({ type: "get", path: row.path }); }
  catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

const uploadInput = ref<HTMLInputElement | null>(null);

async function onUpload(ev: Event) {
  const file = (ev.target as HTMLInputElement).files?.[0];
  if (!file || !ws) return;
  busy.value = true;
  try {
    const path = `${cwd.value.replace(/\/+$/, "")}/${file.name}`;
    await request({ type: "put", path, size: file.size });
    // put_ready 之後才開始送二進位框，一次 256 KiB
    const CHUNK = 256 * 1024;
    const done = new Promise((resolve, reject) => { pending = { resolve, reject }; });
    for (let off = 0; off < file.size; off += CHUNK) {
      const buf = await file.slice(off, off + CHUNK).arrayBuffer();
      ws.send(buf);
    }
    await done;
    msg.success(t("sftp.uploaded", { name: file.name }));
    await refresh();
  } catch (e: any) {
    msg.error(e?.message ?? String(e));
  } finally {
    busy.value = false;
    if (uploadInput.value) uploadInput.value.value = "";
  }
}

async function doMkdir() {
  const name = window.prompt(t("sftp.new_folder_prompt"));
  if (!name) return;
  busy.value = true;
  try {
    await request({ type: "mkdir", path: `${cwd.value.replace(/\/+$/, "")}/${name}` });
    await refresh();
  } catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

async function doRename(row: SftpEntry) {
  const name = window.prompt(t("sftp.rename_prompt", { name: row.name }), row.name);
  // 取消（null）與「改成同一個名字」都不該送出去
  if (!name || name === row.name) return;
  // 只准改同一層的檔名：帶 / 的輸入會變成搬移，那是另一件事，先擋掉
  if (name.includes("/")) { msg.error(t("sftp.rename_no_slash")); return; }
  busy.value = true;
  try {
    const dir = cwd.value.replace(/\/+$/, "");
    await request({ type: "rename", path: row.path, to: `${dir}/${name}` });
    await refresh();
  } catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

async function doDelete(row: SftpEntry) {
  busy.value = true;
  try {
    await request({ type: "delete", path: row.path, is_dir: row.is_dir });
    await refresh();
  } catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

function fmtSize(n: number | null): string {
  // null＝遠端沒回報，不要顯示成 0 B —— 那是兩件事
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const cols = computed<DataTableColumns<SftpEntry>>(() => [
  {
    title: t("sftp.col_name"), key: "name", minWidth: 240,
    render: (r) => h("a", {
      class: r.is_dir ? "sftp-dir" : "sftp-file",
      onClick: () => enter(r),
    }, `${r.is_dir ? "📁 " : ""}${r.name}${r.is_link ? " ↗" : ""}`),
    sorter: (a, b) => Number(!!b.is_dir) - Number(!!a.is_dir)
      || a.name.localeCompare(b.name),
  },
  { title: t("sftp.col_size"), key: "size", width: 110,
    render: (r) => (r.is_dir ? "—" : fmtSize(r.size)),
    sorter: (a, b) => (a.size ?? -1) - (b.size ?? -1) },
  { title: t("sftp.col_mtime"), key: "mtime", width: 170,
    render: (r) => (r.mtime ? fmtDateTime(new Date(r.mtime * 1000).toISOString()) : "—"),
    sorter: (a, b) => (a.mtime ?? 0) - (b.mtime ?? 0) },
  { title: t("sftp.col_mode"), key: "mode", width: 120,
    render: (r) => h("span", { class: "mono" }, r.mode ?? "—") },
  {
    title: t("common.actions"), key: "actions", width: 210,
    render: (r) => h(NSpace, { size: 4 }, () => [
      r.is_dir ? null : h(NButton, {
        size: "tiny", secondary: true, onClick: () => download(r),
      }, () => t("sftp.download")),
      h(NButton, {
        size: "tiny", secondary: true, onClick: () => doRename(r),
      }, () => t("sftp.rename")),
      h(NPopconfirm, { onPositiveClick: () => doDelete(r) }, {
        trigger: () => h(NButton, { size: "tiny", secondary: true, type: "error" },
          () => t("common.delete")),
        default: () => t("sftp.delete_confirm", { name: r.name }),
      }),
    ]),
  },
]);

async function loadCreds() {
  try { creds.value = await listSshCredentials(props.addressId); } catch { /* 沒有就手動輸入 */ }
}
void loadCreds();

onBeforeUnmount(() => { try { ws?.close(); } catch { /* 已關閉 */ } });
</script>

<template>
  <div class="sftp-wrap">
    <n-alert v-if="errorMsg" type="error" :bordered="false" style="margin-bottom: 12px">
      {{ errorMsg }}
    </n-alert>

    <!-- 未連線：沿用 SSH 那套設定（已存憑證優先，否則當次輸入） -->
    <div v-if="!connected" class="sftp-conn">
      <n-space vertical size="large" style="max-width: 460px">
        <div>
          <label>{{ t("sftp.target") }}</label>
          <div class="mono">{{ host }}</div>
        </div>
        <div v-if="creds.length">
          <label>{{ t("sftp.saved_cred") }}</label>
          <n-select v-model:value="form.credential_id" clearable
                    :placeholder="t('sftp.saved_cred_ph')"
                    :options="creds.map((c) => ({ label: `${c.username}（${c.auth_type}）`, value: c.id }))" />
        </div>
        <template v-if="!form.credential_id">
          <div>
            <label>{{ t("sftp.username") }}</label>
            <n-input v-model:value="form.username" placeholder="root" />
          </div>
          <div>
            <label>{{ t("sftp.auth") }}</label>
            <n-select v-model:value="form.auth" :options="[
              { label: t('sftp.auth_password'), value: 'password' },
              { label: t('sftp.auth_key'), value: 'key' }]" />
          </div>
          <div v-if="form.auth === 'password'">
            <label>{{ t("sftp.password") }}</label>
            <n-input v-model:value="form.password" type="password" show-password-on="click" />
          </div>
          <template v-else>
            <div>
              <label>{{ t("sftp.private_key") }}</label>
              <n-input v-model:value="form.private_key" type="textarea" :rows="4"
                       placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
            </div>
            <div>
              <label>{{ t("sftp.passphrase") }}</label>
              <n-input v-model:value="form.passphrase" type="password" show-password-on="click" />
            </div>
          </template>
        </template>
        <div>
          <label>{{ t("sftp.port") }}</label>
          <n-input-number v-model:value="form.port" :min="1" :max="65535" />
        </div>
        <n-button type="primary" :loading="connecting" @click="connect">
          {{ t("sftp.connect") }}
        </n-button>
      </n-space>
    </div>

    <!-- 已連線：路徑列 + 檔案清單 -->
    <template v-else>
      <n-space align="center" style="margin-bottom: 10px">
        <n-button size="small" :disabled="cwd === '/'" @click="goUp">{{ t("sftp.up") }}</n-button>
        <n-input :value="cwd" style="width: 360px"
                 @update:value="(v: string) => (cwd = v)"
                 @keyup.enter="() => refresh()" />
        <n-button size="small" :loading="busy" @click="() => refresh()">
          <template #icon><n-icon><RefreshIcon /></n-icon></template>
          {{ t("common.refresh") }}
        </n-button>
        <n-button size="small" @click="doMkdir">
          <template #icon><n-icon><PlusIcon /></n-icon></template>
          {{ t("sftp.new_folder") }}
        </n-button>
        <n-button size="small" type="primary" @click="() => uploadInput?.click()">
          {{ t("sftp.upload") }}
        </n-button>
        <input ref="uploadInput" type="file" style="display:none" @change="onUpload" />
      </n-space>

      <!-- 截斷要明講：畫面上少幾千個檔案而不說，等於騙人 -->
      <n-alert v-if="truncated" type="warning" :bordered="false" style="margin-bottom: 8px">
        {{ t("sftp.truncated") }}
      </n-alert>

      <n-data-table :columns="cols" :data="entries" :loading="busy" size="small"
                    :bordered="true" :max-height="520" virtual-scroll />
    </template>

    <n-spin v-if="connecting" />
  </div>
</template>

<style scoped>
.sftp-wrap { padding: 4px; }
.sftp-conn label { display: block; font-size: 12px; opacity: .8; margin-bottom: 4px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
.sftp-dir { color: var(--primary-color, #18a058); cursor: pointer; font-weight: 600; }
.sftp-file { cursor: default; }
</style>
