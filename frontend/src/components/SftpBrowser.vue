<script setup lang="ts">
/**
 * SFTP 檔案瀏覽器：先換 ticket → 開 WebSocket → 後端橋接 asyncssh 的 SFTP。
 *
 * 開關與 SSH 各自獨立（`sftp_enabled`），但**授權模型刻意完全相同**，憑證也共用同一個
 * 個人加密金庫 —— 能讀寫遠端檔案的人，實質能力與能開 shell 的人同一級。
 *
 * 版面刻意與 SshTerminal 一致（卡片式連線表單 → 狀態列 + 內容區）：同一套操作在不同
 * 協定間長得不一樣，使用者得重新學一次。
 *
 * 下載採「收完再存檔」：這個功能是給設定檔、憑證、log 片段用的，後端已把單檔上限訂在
 * 100 MB，收在記憶體再落地最單純。真要搬大檔請用 scp/rsync —— 把工具用在它擅長的地方，
 * 比在瀏覽器裡硬做串流落地實在。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { sortEntries, type SortKey, type SortOrder } from "@/utils/sftpSort";
import { getPreferences, updatePreferences } from "@/api/preferences";
import { useI18n } from "vue-i18n";
import {
  NAlert, NButton, NCard, NDataTable, NForm, NFormItem, NIcon, NInput,
  NInputNumber, NPopconfirm, NRadio, NRadioGroup, NSelect, NSpace, NSpin, NSwitch,
  NTooltip,
  NTag, NModal, NInputGroup, useMessage,
} from "naive-ui";
import type { DataTableColumns } from "naive-ui";
import { h } from "vue";
import {
  buildSshWsUrl, createSshCredential, deleteSshCredential, listSshCredentials,
  requestSftpTicket, type SftpEntry, type SshCredential,
} from "@/api/ssh";
import { fmtDateTime } from "@/utils/datetime";
import { useTablePagination } from "@/composables/useTablePagination";
import {
  RefreshIcon, FilesIcon, CancelIcon, DeleteIcon, EditIcon,
  DownloadIcon, UploadIcon, NewFolderIcon, FilterIcon, SortAscIcon, MoveIcon, UpLevelIcon,
} from "@/icons";

const props = defineProps<{
  addressId: string; host: string;
  hostname?: string | null; deviceName?: string | null; fullHeight?: boolean;
}>();
const { t } = useI18n();
const msg = useMessage();

/** 連線階段 —— 與 SSH／RDP／VNC 主控台同一組狀態名，狀態列也才能共用同一套樣式。 */
const phase = ref<"form" | "connecting" | "connected" | "error" | "closed">("form");
const connecting = computed(() => phase.value === "connecting");
/** 是否已經在看檔案清單（連上了、或連上後才斷線）—— 其餘階段都停留在連線卡片上。 */
const onFileList = computed(() => phase.value === "connected" || phase.value === "closed");
const errorMsg = ref("");
const cwd = ref("/");
const entries = ref<SftpEntry[]>([]);
const truncated = ref(false);
const busy = ref(false);

// ── 連線設定（與 SSH 相同：已存憑證，或當次輸入；也可以順手存起來）
const creds = ref<SshCredential[]>([]);
const remember = ref(false);
const rememberLabel = ref("");
const form = ref({
  credential_id: null as string | null,
  username: "", port: 22, auth: "password" as "password" | "key",
  password: "", private_key: "", passphrase: "",
});
const credOptions = computed(() => creds.value.map((c) => ({
  // 標籤格式與 SSH 主控台一致
  label: `${c.label}（${c.username}・${c.auth_type === "key" ? t("ssh.auth_key") : t("ssh.auth_password")}）`,
  value: c.id,
})));

async function delSelectedCred() {
  const id = form.value.credential_id;
  if (!id) return;
  try {
    await deleteSshCredential(id);
    form.value.credential_id = null;
    await loadCreds();
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? String(e)); }
}

let ws: WebSocket | null = null;
/** 是否曾經真的連上過。
 *
 * 不能用 phase 判斷「斷線時是不是已經連上」：使用者按「中斷連線」時，我們自己會先把
 * phase 設成 closed，接著 onclose 讀到的就不是 connected → 誤判成「還沒連上就斷了」，
 * 於是跳回連線表單並顯示「連線在建立完成前就被關閉（代碼 1005）」—— 明明是使用者
 * 自己按的，卻看起來像連線失敗。 */
let everConnected = false;
/** 下載中的檔案：收到 file_begin 後開始累積二進位框，file_end 才落地。 */
let incoming: { name: string; size: number; chunks: Uint8Array[]; got: number } | null = null;
/** 等待中的請求：讓 send 之後可以 await 到結果。 */
let pending: { resolve: (v: any) => void; reject: (e: Error) => void } | null = null;
/** 這次的 list 只是「對話框在瀏覽目錄」，不要動主畫面的清單。 */
let browsingOnly = false;
/** 這次上傳是否已被伺服器中止（收到 error）—— 送出迴圈看到就停手。 */
let uploadAborted = false;

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
  if (!form.value.credential_id && !form.value.username.trim()) {
    errorMsg.value = t("ssh.err_username"); return;
  }
  phase.value = "connecting";

  // 勾了「記住」→ 先存進金庫，之後就以 reference 連線（與 SSH 同一個金庫、同一套作法）
  if (!form.value.credential_id && remember.value) {
    try {
      const saved = await createSshCredential({
        label: rememberLabel.value.trim() || `${form.value.username.trim()}@${props.host}`,
        username: form.value.username.trim(),
        auth_type: form.value.auth,
        target_ip_id: props.addressId,
        password: form.value.auth === "password" ? form.value.password : undefined,
        private_key: form.value.auth === "key" ? form.value.private_key : undefined,
        passphrase: form.value.auth === "key" ? form.value.passphrase : undefined,
      });
      form.value.credential_id = saved.id;
      remember.value = false;
      void loadCreds();
    } catch (e: any) {
      phase.value = "error";
      errorMsg.value = e?.response?.data?.detail || t("ssh.err_save_cred");
      return;
    }
  }

  try {
    everConnected = false;
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
          everConnected = true;
          phase.value = "connected";
          cwd.value = m.cwd || "/";
          void refresh();
          break;
        case "list":
          // 移動對話框也用同一個 list 取目錄 —— 那時候不能動主畫面的狀態，
          // 否則「已勾選的那幾列」會被換掉，按下移動時就什麼都不剩了（實際踩過）。
          if (!browsingOnly) {
            cwd.value = m.path;
            entries.value = m.entries ?? [];
            truncated.value = !!m.truncated;
          }
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
        case "error": {
          uploadAborted = true;          // 正在上傳的話，讓送出迴圈停下來
          errorMsg.value = m.message ?? "";
          // 連線階段失敗要退回表單，否則使用者卡在一片空白、無從重試
          if (phase.value !== "connected") phase.value = "error";
          // 帶上 code：有些失敗是可以補救的（例如資料夾不是空的 → 問要不要連內容一起刪），
          // 只丟字串的話呼叫端只能把它當成一般錯誤顯示
          const err = new Error(m.message) as Error & { code?: string };
          err.code = m.code;
          pending?.reject(err); pending = null;
          break;
        }
      }
    };

    ws.onclose = (ev) => {
      const wasConnected = everConnected;
      phase.value = wasConnected ? "closed" : "error";
      // 還沒連上就被關掉，而且後端也沒送 error —— 這時什麼都不說，畫面看起來像
      // 「按了沒反應」。實際遇過的原因是反向代理沒轉發 WebSocket 升級標頭
      // （nginx 的 location 少列 sftp），瀏覽器只看得到連線被關閉。
      if (!wasConnected && !errorMsg.value) {
        errorMsg.value = t("sftp.err_ws_closed", { code: ev.code || 0 });
      }
      pending?.reject(new Error(t("sftp.disconnected"))); pending = null;
    };
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail ?? String(e);
    phase.value = "error";
  }
}

function disconnect() {
  try { ws?.close(); } catch { /* noop */ }
  phase.value = "closed";
}

/** 回到連線表單重連（沿用已選的憑證，不必重打帳密）。 */
function reconnect() {
  entries.value = [];
  errorMsg.value = "";
  phase.value = "form";
}

async function refresh(path?: string) {
  busy.value = true;
  checkedKeys.value = [];      // 換目錄還留著上一層的勾選 → 會刪錯東西
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
/** 拖曳中（游標在面板上且帶著檔案）—— 用來顯示「放開以上傳」的提示。 */
const dragging = ref(false);
let dragDepth = 0;          // dragenter/leave 會在子元素間跳動，用計數才不會閃爍
/** 多檔上傳的進度：第幾個 / 共幾個。 */
const uploadProgress = ref<{ done: number; total: number; name: string } | null>(null);

/** 送一個檔案到目前目錄。失敗直接往外拋，由呼叫端決定要不要繼續其他檔案。 */
async function putOneFile(file: File) {
  if (!ws) throw new Error(t("sftp.disconnected"));
  const path = `${cwd.value.replace(/\/+$/, "")}/${file.name}`;
  await request({ type: "put", path, size: file.size });
  // put_ready 之後才開始送二進位框，一次 256 KiB
  const CHUNK = 256 * 1024;
  // 送出緩衝的上限。沒有這個限制時，大檔會被整個塞進瀏覽器的 WebSocket 送出緩衝
  // （伺服器來不及消化），Chrome 會直接把連線切掉 —— 實機上拖兩個檔案就重現：
  // 遠端留下 0 位元組的檔案、畫面顯示「連線已中斷」。
  const HIGH_WATER = 4 * 1024 * 1024;
  // 上傳被伺服器中止時要立刻停送。否則剩下的資料框會繼續往連線裡塞，
  // 對方已經回到「等下一個指令」的狀態，那些框就成了雜訊。
  uploadAborted = false;
  const done = new Promise((resolve, reject) => { pending = { resolve, reject }; });
  for (let off = 0; off < file.size; off += CHUNK) {
    const buf = await file.slice(off, off + CHUNK).arrayBuffer();
    // 等緩衝消化到水位以下再繼續送；中途斷線就別再送了
    while (ws.bufferedAmount > HIGH_WATER) {
      if (ws.readyState !== WebSocket.OPEN) throw new Error(t("sftp.disconnected"));
      await new Promise((r) => setTimeout(r, 20));
    }
    if (uploadAborted) throw new Error(t("sftp.disconnected"));
    if (ws.readyState !== WebSocket.OPEN) throw new Error(t("sftp.disconnected"));
    ws.send(buf);
  }
  await done;
}

/** 上傳一批檔案：逐個送（同一條連線，並行只會互相排隊）。 */
async function uploadFiles(files: File[]) {
  if (!files.length || !ws) return;
  busy.value = true;
  const failed: string[] = [];
  try {
    for (const [i, file] of files.entries()) {
      uploadProgress.value = { done: i, total: files.length, name: file.name };
      // 一個檔案失敗不該讓其餘的都不傳；最後一次講清楚是哪幾個
      try { await putOneFile(file); } catch (e: any) {
        failed.push(`${file.name}（${e?.message ?? String(e)}）`);
      }
    }
    const ok = files.length - failed.length;
    if (failed.length) msg.error(t("sftp.upload_partial_fail", { ok, names: failed.join("、") }));
    else if (files.length === 1) msg.success(t("sftp.uploaded", { name: files[0].name }));
    else msg.success(t("sftp.uploaded_many", { n: files.length }));
    await refresh();
  } finally {
    uploadProgress.value = null;
    busy.value = false;
    if (uploadInput.value) uploadInput.value.value = "";
  }
}

async function onUpload(ev: Event) {
  const list = (ev.target as HTMLInputElement).files;
  await uploadFiles(list ? Array.from(list) : []);
}

// ── 拖曳上傳
function onDragEnter(ev: DragEvent) {
  if (!ev.dataTransfer?.types?.includes("Files")) return;   // 拖文字進來不該亮起來
  dragDepth += 1;
  dragging.value = true;
}
function onDragOver(ev: DragEvent) {
  if (!ev.dataTransfer?.types?.includes("Files")) return;
  ev.preventDefault();                       // 不攔的話瀏覽器會直接開啟那個檔案
  ev.dataTransfer.dropEffect = "copy";
}
function onDragLeave() {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dragging.value = false;
}
async function onDrop(ev: DragEvent) {
  ev.preventDefault();
  dragDepth = 0;
  dragging.value = false;
  if (phase.value !== "connected") return;
  const dt = ev.dataTransfer;
  if (!dt) return;
  // 資料夾拖進來時 webkitGetAsEntry().isDirectory 為真 —— 目前只收檔案，要講出來
  const items = Array.from(dt.items ?? []);
  const dirs = items.filter((it) => (it as any).webkitGetAsEntry?.()?.isDirectory).length;
  const files = Array.from(dt.files ?? []).filter((f) => f.size > 0 || !dirs);
  if (dirs) msg.warning(t("sftp.drop_dirs_skipped", { n: dirs }));
  await uploadFiles(files);
}

// ── 名稱輸入對話框（新增資料夾 / 重新命名）
// 用應用程式自己的對話框，不用 window.prompt：瀏覽器原生對話框長得跟系統警告一樣、
// 不受主題影響、也沒辦法做驗證與說明。
const nameDlg = ref<{ mode: "mkdir" | "rename"; value: string; row: SftpEntry | null } | null>(null);
const nameDlgTitle = computed(() =>
  nameDlg.value?.mode === "mkdir" ? t("sftp.new_folder") : t("sftp.rename"));
const nameDlgShow = computed({
  get: () => nameDlg.value !== null,
  set: (v: boolean) => { if (!v) nameDlg.value = null; },
});

function doMkdir() {
  nameDlg.value = { mode: "mkdir", value: "", row: null };
}

function doRename(row: SftpEntry) {
  nameDlg.value = { mode: "rename", value: row.name, row };
}

async function submitNameDlg() {
  const d = nameDlg.value;
  if (!d) return;
  const name = d.value.trim();
  if (!name) return;
  // 名稱不能含 /：那會變成搬移到別的目錄，是另一件事（要搬請用「移動」）
  if (name.includes("/")) { msg.error(t("sftp.rename_no_slash")); return; }
  if (d.mode === "rename" && d.row && name === d.row.name) { nameDlg.value = null; return; }
  busy.value = true;
  try {
    const dir = cwd.value.replace(/\/+$/, "");
    if (d.mode === "mkdir") await request({ type: "mkdir", path: `${dir}/${name}` });
    else await request({ type: "rename", path: d.row!.path, to: `${dir}/${name}` });
    nameDlg.value = null;
    await refresh();
  } catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

async function doDelete(row: SftpEntry) {
  busy.value = true;
  try {
    await request({ type: "delete", path: row.path, is_dir: row.is_dir });
    await refresh();
  } catch (e: any) {
    // 資料夾有內容：SFTP 不會刪有東西的資料夾。與其只說失敗，不如把後端查到的
    // 項目數講出來，並讓使用者明確決定要不要連內容一起刪（這是破壞性的，要問過）
    if (e?.code === "dir_not_empty") {
      errorMsg.value = "";
      confirmRecursive.value = { path: row.path, detail: e.message ?? "" };
    } else {
      msg.error(e?.message ?? String(e));
    }
  } finally { busy.value = false; }
}

/** 待確認的「連同內容刪除」；null＝沒有待確認的 */
const confirmRecursive = ref<{ path: string; detail: string } | null>(null);

async function doDeleteRecursive() {
  const target = confirmRecursive.value;
  confirmRecursive.value = null;
  if (!target) return;
  busy.value = true;
  try {
    const r = await request({ type: "delete", path: target.path, is_dir: true,
                              recursive: true });
    msg.success(t("sftp.deleted_recursive", { n: r?.removed ?? 0 }));
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

/** 小按鈕：icon + 文字（表格操作欄用）。 */
function actionBtn(icon: any, label: string, onClick: () => void, type?: "error") {
  return h(NButton, { size: "tiny", secondary: true, type, onClick }, {
    icon: () => h(NIcon, null, { default: () => h(icon) }),
    default: () => label,
  });
}

const cols = computed<DataTableColumns<SftpEntry>>(() => [
  { type: "selection" },
  {
    title: t("sftp.col_name"), key: "name", minWidth: 240,
    render: (r) => h("a", {
      class: ["sftp-name", r.is_dir ? "sftp-dir" : "sftp-file"],
      // 行內樣式而非 scoped CSS：這些元素是 render function 產生的，不帶 data-v 標記，
      // scoped 樣式套不到它們（量到檔案名稱比資料夾少縮排 16px 就是這個原因）
      style: "display:inline-flex;align-items:center;gap:6px",
      onClick: () => enter(r),
    }, [
      // 檔案沒有 icon，仍要佔掉與資料夾完全相同的寬度，否則兩種列的名稱對不齊。
      // 用固定尺寸的 icon 元件而不是 emoji —— emoji 寬度由字型決定，量到過差 17px。
      h("span", { style: "width:16px;height:16px;flex:none;display:inline-flex;"
                       + "align-items:center;justify-content:center;overflow:hidden" },
        r.is_dir ? [h(NIcon, { size: 16 }, { default: () => h(FilesIcon) })] : []),
      h("span", null, `${r.name}${r.is_link ? " ↗" : ""}`),
    ]),
    sorter: true,
    sortOrder: sortKey.value === "name" ? sortOrder.value : false,
  },
  { title: t("sftp.col_size"), key: "size", width: 110,
    render: (r) => (r.is_dir ? "—" : fmtSize(r.size)),
    sorter: true, sortOrder: sortKey.value === "size" ? sortOrder.value : false },
  { title: t("sftp.col_mtime"), key: "mtime", width: 170,
    render: (r) => (r.mtime ? fmtDateTime(new Date(r.mtime * 1000).toISOString()) : "—"),
    sorter: true, sortOrder: sortKey.value === "mtime" ? sortOrder.value : false },
  { title: t("sftp.col_mode"), key: "mode", width: 120,
    // 權限位元要等寬才對得齊（drwxr-xr-x 每個位置都有意義，錯位就得一個字一個字數）。
    // 這裡走 inline style 而非 class：render function 產生的節點在表格內部，拿不到本元件的
    // scoped 樣式作用域 —— 之前掛 class="mono" 完全沒生效就是這個原因。
    render: (r) => h("span", {
      style: "font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: nowrap;",
    }, r.mode ?? "—") },
  {
    title: t("common.actions"), key: "actions", width: 230,
    render: (r) => h(NSpace, { size: 4, wrap: false }, () => [
      r.is_dir ? null : actionBtn(DownloadIcon, t("sftp.download"), () => download(r)),
      actionBtn(EditIcon, t("sftp.rename"), () => doRename(r)),
      h(NPopconfirm, { onPositiveClick: () => doDelete(r) }, {
        trigger: () => h(NButton, { size: "tiny", secondary: true, type: "error" }, {
          icon: () => h(NIcon, null, { default: () => h(DeleteIcon) }),
          default: () => t("common.delete"),
        }),
        default: () => t("sftp.delete_confirm", { name: r.name }),
      }),
    ]),
  },
]);

// ── 排序：自己做，不交給表格的 sorter
// 因為「資料夾優先」必須在升冪／降冪**之外**成立 —— 寫進比較函式的話，切成降冪
// 會連分組一起反轉，資料夾跑到最後面。表格的 sorter 拿不到方向，所以改成受控排序。
const sortKey = ref<SortKey>("name");
const sortOrder = ref<SortOrder>("ascend");
const dirsFirst = ref(true);          // 預設與檔案總管一致

const sortModeOptions = computed(() => [
  { label: t("sftp.sort_dirs_first"), value: "dirs" },
  { label: t("sftp.sort_mixed"), value: "mixed" },
]);

/** 選中值前面掛排序 icon —— 工具列上其他控制項都是「icon + 文字」，少一個看得出來。 */
function renderSortTag({ option }: { option: { label?: unknown } }) {
  return h("span", { style: "display:flex;align-items:center;gap:6px;" }, [
    h(NIcon, null, { default: () => h(SortAscIcon) }),
    String(option?.label ?? ""),
  ]);
}

function onSorterChange(s: { columnKey?: string | number; order?: SortOrder } | null) {
  if (!s || !s.order) { sortOrder.value = false; return; }
  sortKey.value = String(s.columnKey ?? "name") as SortKey;
  sortOrder.value = s.order;
}

async function setDirsFirst(v: boolean) {
  dirsFirst.value = v;
  // 存進使用者偏好（跨裝置），與每頁筆數同一個機制；失敗不影響當下操作
  try { await updatePreferences({ sftp_sort_dirs_first: v }); } catch { /* 靜默 */ }
}

onMounted(async () => {
  try {
    const p = await getPreferences();
    if (typeof p.sftp_sort_dirs_first === "boolean") dirsFirst.value = p.sftp_sort_dirs_first;
  } catch { /* 讀不到就用預設 */ }
});

// ── 篩選：只篩目前這一頁的清單（遠端不重撈，因為列出來的就是全部了）
const filterText = ref("");
const shownEntries = computed(() => {
  const q = filterText.value.trim().toLowerCase();
  const base = q
    ? entries.value.filter((e) => e.name.toLowerCase().includes(q))
    : entries.value;
  return sortEntries(base, {
    key: sortKey.value, order: sortOrder.value, dirsFirst: dirsFirst.value,
  });
});

// 目錄很大時分頁顯示 —— 原本只能截斷後說「只顯示一部分」，現在整份都拿得到、翻頁看
const pagination = useTablePagination();

// ── 批次作業
const checkedKeys = ref<string[]>([]);
const checkedRows = computed(() =>
  entries.value.filter((e) => checkedKeys.value.includes(e.path)));

/** 每次換目錄或重新整理都清空勾選 —— 留著上一個目錄的選取會刪錯東西。 */
function clearSelection() { checkedKeys.value = []; }

async function batchDownload() {
  const files = checkedRows.value.filter((r) => !r.is_dir);
  const dirs = checkedRows.value.length - files.length;
  if (!files.length) { msg.warning(t("sftp.batch_no_files")); return; }
  busy.value = true;
  try {
    // 逐個下載：每個檔案都是獨立的一次傳輸，同時進行只會互相排隊
    for (const f of files) await request({ type: "get", path: f.path });
    // 資料夾不能當檔案下載 —— 要講出來，不能安靜地少傳幾個
    msg.success(dirs
      ? t("sftp.batch_downloaded_skipped_dirs", { n: files.length, dirs })
      : t("sftp.batch_downloaded", { n: files.length }));
  } catch (e: any) { msg.error(e?.message ?? String(e)); }
  finally { busy.value = false; }
}

async function batchDelete() {
  const rows = checkedRows.value;
  if (!rows.length) return;
  busy.value = true;
  const failed: string[] = [];
  try {
    const nonEmpty: string[] = [];
    for (const r of rows) {
      try { await request({ type: "delete", path: r.path, is_dir: r.is_dir }); }
      catch (e: any) {
        // 有內容的資料夾另外列：那不是「刪不掉」，而是要先決定要不要連內容刪，
        // 混在一般失敗裡會讓人以為壞了
        if (e?.code === "dir_not_empty") nonEmpty.push(r.name);
        else failed.push(r.name);           // 一個失敗不該讓其他的也不做
      }
    }
    // 部分失敗要說清楚是哪幾個，否則使用者以為全刪了
    if (nonEmpty.length) {
      msg.warning(t("sftp.batch_dirs_not_empty", { names: nonEmpty.join("、") }),
                  { duration: 8000 });
    }
    if (failed.length) msg.error(t("sftp.batch_partial_fail", { names: failed.join("、") }));
    else if (!nonEmpty.length) msg.success(t("sftp.batch_deleted", { n: rows.length }));
  } finally {
    clearSelection();
    await refresh();
    busy.value = false;
  }
}

// ── 移動對話框：可直接打路徑，也可以點目錄一層層瀏覽
const moveDlg = ref(false);
const moveDest = ref("/");            // 目的地（輸入框與瀏覽同步）
const moveDirs = ref<SftpEntry[]>([]); // 目前瀏覽層的子目錄
const moveLoading = ref(false);

/** 按下「移動」當下要搬的項目 —— 快照起來，不受之後瀏覽目錄影響。 */
const movingRows = ref<SftpEntry[]>([]);

function openMoveDlg() {
  if (!checkedRows.value.length) return;
  movingRows.value = [...checkedRows.value];
  moveDest.value = cwd.value;
  moveDlg.value = true;
  void browseMove(cwd.value);
}

/** 列出某層的子目錄給對話框點選（只顯示目錄 —— 檔案不能當搬移目的地）。 */
async function browseMove(path: string) {
  moveLoading.value = true;
  browsingOnly = true;
  try {
    const m = await request({ type: "list", path });
    moveDest.value = m.path;
    moveDirs.value = (m.entries ?? []).filter((e: SftpEntry) => e.is_dir);
  } catch (e: any) {
    msg.error(e?.message ?? String(e));
  } finally {
    browsingOnly = false;
    moveLoading.value = false;
  }
}

function moveUp() {
  const p = moveDest.value.replace(/\/+$/, "");
  void browseMove(p.slice(0, p.lastIndexOf("/")) || "/");
}

async function confirmMove() {
  const dir = moveDest.value.trim().replace(/\/+$/, "") || "/";
  moveDlg.value = false;
  await batchMove(dir);
  await refresh();          // 對話框瀏覽過別的目錄，回到原本這一層
}

async function batchMove(dest: string) {
  const rows = movingRows.value.length ? movingRows.value : checkedRows.value;
  if (!rows.length) return;
  const dir = dest.replace(/\/+$/, "") || "/";
  busy.value = true;
  const failed: string[] = [];
  try {
    for (const r of rows) {
      try { await request({ type: "rename", path: r.path, to: `${dir}/${r.name}` }); }
      catch { failed.push(r.name); }
    }
    if (failed.length) msg.error(t("sftp.batch_partial_fail", { names: failed.join("、") }));
    else msg.success(t("sftp.batch_moved", { n: rows.length, dir }));
  } finally {
    clearSelection();
    await refresh();
    busy.value = false;
  }
}

async function loadCreds() {
  try {
    creds.value = await listSshCredentials(props.addressId);
    // 有已存帳密就預設選最近一筆（與 SSH 主控台一致）：不必再輸入，直接按連線；
    // 要改用其他帳密把下拉清空即可，手動欄位就會回來。
    if (!form.value.credential_id && creds.value.length) {
      form.value.credential_id = creds.value[0].id;
    }
  } catch { /* 沒有就手動輸入 */ }
}
void loadCreds();

onBeforeUnmount(() => { try { ws?.close(); } catch { /* 已關閉 */ } });
</script>

<template>
  <div class="sftp-wrap"
       :class="{ 'sftp-full': fullHeight, 'sftp-center': fullHeight && !onFileList }">
    <!-- 連線設定表單（版面與 SSH 終端機一致：卡片 + 左標籤表單 + 說明 + 右下連線鈕）
         連線中也留在這裡：按下連線後把卡片挪走會讓畫面整個跳一下，而失敗時又跳回來 -->
    <div v-if="!onFileList" class="sftp-form">
      <n-card size="small" :bordered="true">
        <template #header>
          <span style="display:flex;align-items:center;gap:8px">
            <n-icon :component="FilesIcon" :size="18" />
            <span>{{ t("sftp.connect_to", { ip: host }) }}</span>
          </span>
        </template>

        <n-alert v-if="errorMsg" type="error" :bordered="false" style="margin-bottom:12px">
          {{ errorMsg }}
        </n-alert>

        <!-- 已存帳密（個人保管）：選一筆即以 reference 連線 -->
        <div v-if="credOptions.length" class="sftp-saved-row">
          <span class="sftp-saved-label">{{ t("ssh.saved_cred") }}</span>
          <n-select v-model:value="form.credential_id" :options="credOptions" clearable size="small"
                    :placeholder="t('ssh.saved_cred_ph')" style="flex:1" />
          <n-popconfirm v-if="form.credential_id" @positive-click="delSelectedCred">
            <template #trigger>
              <n-button quaternary type="error" size="small">
                <template #icon><n-icon :component="DeleteIcon" /></template>
              </n-button>
            </template>
            {{ t("ssh.saved_cred_del_confirm") }}
          </n-popconfirm>
        </div>

        <n-form label-placement="left" :label-width="92" size="small">
          <!-- 手動輸入（未選已存帳密時才顯示）-->
          <template v-if="!form.credential_id">
            <n-form-item :label="t('ssh.auth_method')">
              <n-radio-group v-model:value="form.auth">
                <n-radio value="password">{{ t("ssh.auth_password") }}</n-radio>
                <n-radio value="key">{{ t("ssh.auth_key") }}</n-radio>
              </n-radio-group>
            </n-form-item>
            <n-form-item :label="t('ssh.username')">
              <n-input v-model:value="form.username" placeholder="root" autofocus
                       @keyup.enter="connect" />
            </n-form-item>
            <n-form-item v-if="form.auth === 'password'" :label="t('ssh.password')">
              <n-input v-model:value="form.password" type="password" show-password-on="click"
                       @keyup.enter="connect" />
            </n-form-item>
            <template v-else>
              <n-form-item :label="t('ssh.private_key')">
                <n-input v-model:value="form.private_key" type="textarea"
                         :autosize="{ minRows: 4, maxRows: 8 }"
                         placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
              </n-form-item>
              <n-form-item :label="t('ssh.passphrase')">
                <n-input v-model:value="form.passphrase" type="password" show-password-on="click" />
              </n-form-item>
            </template>
          </template>

          <n-form-item :label="t('ssh.port')">
            <n-input-number v-model:value="form.port" :min="1" :max="65535" style="width:140px" />
          </n-form-item>

          <!-- 記住此帳密（僅手動模式）-->
          <n-form-item v-if="!form.credential_id" :label="t('ssh.remember')">
            <n-space vertical :size="4" style="width:100%">
              <n-switch v-model:value="remember" />
              <n-input v-if="remember" v-model:value="rememberLabel" size="small"
                       :placeholder="t('ssh.remember_label_ph')" />
            </n-space>
          </n-form-item>

          <n-alert :show-icon="false" type="info" style="margin-bottom:10px">
            {{ form.credential_id ? t("ssh.use_saved_hint") : (remember ? t("ssh.store_hint") : t("ssh.no_store_hint")) }}
          </n-alert>
          <n-space justify="end">
            <n-button type="primary" :loading="connecting" @click="connect">
              <template #icon><n-icon :component="FilesIcon" /></template>
              {{ t("sftp.connect") }}
            </n-button>
          </n-space>
        </n-form>
      </n-card>
    </div>

    <!-- 已連線／已中斷：狀態列 + 檔案清單（狀態列與 SSH 主控台同一套） -->
    <div v-else class="sftp-area" :class="{ 'sftp-full': fullHeight }">
      <div class="sftp-toolbar">
        <span class="sftp-status" :data-state="phase">
          <n-spin v-if="phase === 'connecting'" :size="12" />
          <span v-else class="sftp-dot" />
          <span>{{ t(`ssh.state_${phase}`) }}</span>
          <span class="sftp-ip">{{ host }}</span>
          <n-tag v-if="hostname" size="small" :bordered="false" round>{{ hostname }}</n-tag>
          <span class="conn-proto conn-proto--sftp">SFTP</span>
          <n-tag v-if="deviceName" size="small" type="info" :bordered="false" round>{{ deviceName }}</n-tag>
        </span>
        <n-space :size="8" align="center">
          <n-button v-if="phase === 'connected'" size="tiny" type="error" ghost @click="disconnect">
            <template #icon><n-icon :component="CancelIcon" /></template>{{ t("ssh.disconnect") }}
          </n-button>
          <n-button v-else size="tiny" type="primary" ghost @click="reconnect">
            {{ t("ssh.reconnect") }}
          </n-button>
        </n-space>
      </div>

      <!-- 檔案操作區：路徑、操作與清單包在同一個框裡（狀態列刻意留在框外，
           與 SSH 主控台一致：那一列講的是連線，不是檔案）。 -->
      <div class="sftp-panel"
           :class="{ 'sftp-full': fullHeight, 'sftp-dropping': dragging,
                     'sftp-offline': phase !== 'connected' }"
           @dragenter="onDragEnter" @dragover="onDragOver"
           @dragleave="onDragLeave" @drop="onDrop">
        <!-- 拖曳中：整塊面板都是放置區，並講明會放到哪個目錄 -->
        <div v-if="dragging" class="sftp-dropzone">
          <n-icon :size="28"><UploadIcon /></n-icon>
          <span>{{ t("sftp.drop_here", { dir: cwd }) }}</span>
        </div>
        <!-- 連線中斷：整塊面板一次遮起來，不逐塊套樣式。
             逐塊套的話總會漏掉一塊（分頁列、錯誤列都曾經還亮著、還能點），
             而且「能點但送不出去」比「明顯不能點」更難懂。 -->
        <div v-if="phase !== 'connected'" class="sftp-offline-mask">
          <div class="sftp-offline-box">
            <n-icon :size="24"><CancelIcon /></n-icon>
            <span class="sftp-offline-title">{{ t("sftp.offline_title") }}</span>
            <span class="sftp-offline-hint">{{ t("sftp.offline_hint") }}</span>
            <n-button size="small" type="primary" @click="reconnect">
              {{ t("ssh.reconnect") }}
            </n-button>
          </div>
        </div>
      <n-alert v-if="errorMsg" type="error" :bordered="false" style="margin-bottom:8px">
        {{ errorMsg }}
      </n-alert>

      <!-- 路徑列與操作 -->
      <n-space align="center" class="sftp-pathbar">
        <n-button size="small" :disabled="cwd === '/'" @click="goUp">
          <template #icon><n-icon><UpLevelIcon /></n-icon></template>
          {{ t("sftp.up") }}
        </n-button>
        <n-input :value="cwd" class="mono" style="width: 320px"
                 @update:value="(v: string) => (cwd = v)"
                 @keyup.enter="() => refresh()" />
        <n-button size="small" :loading="busy" @click="() => refresh()">
          <template #icon><n-icon><RefreshIcon /></n-icon></template>
          {{ t("common.refresh") }}
        </n-button>
        <n-button size="small" @click="doMkdir">
          <template #icon><n-icon><NewFolderIcon /></n-icon></template>
          {{ t("sftp.new_folder") }}
        </n-button>
        <n-button size="small" type="primary" @click="() => uploadInput?.click()">
          <template #icon><n-icon><UploadIcon /></n-icon></template>
          {{ t("sftp.upload") }}
        </n-button>
        <input ref="uploadInput" type="file" multiple style="display:none" @change="onUpload" />
        <!-- 篩選只作用在目前這個目錄的清單（列出來的就是全部，不必回遠端重撈） -->
        <n-input v-model:value="filterText" clearable style="width: 200px"
                 :placeholder="t('sftp.filter_ph')">
          <template #prefix><n-icon><FilterIcon /></n-icon></template>
        </n-input>
        <!-- 排序方式：兩派都有道理（檔案總管 vs ls），所以讓使用者自己選；存進偏好跨裝置 -->
        <n-tooltip trigger="hover">
          <template #trigger>
            <!-- 尺寸與 icon 都比照旁邊的篩選框：工具列上少一個 icon、矮一截都看得出來 -->
            <n-select :value="dirsFirst ? 'dirs' : 'mixed'" style="width: 190px"
                      :options="sortModeOptions" :render-tag="renderSortTag"
                      @update:value="(v: string) => setDirsFirst(v === 'dirs')" />
          </template>
          {{ t("sftp.sort_mode_hint") }}
        </n-tooltip>
      </n-space>

      <!-- 勾選後才出現的批次列：沒選東西時不佔版面，也不會讓人誤按 -->
      <n-space v-if="checkedKeys.length" align="center" class="sftp-batchbar">
        <span class="sftp-batch-count">{{ t("sftp.batch_selected", { n: checkedKeys.length }) }}</span>
        <n-button size="small" :loading="busy" @click="batchDownload">
          <template #icon><n-icon><DownloadIcon /></n-icon></template>
          {{ t("sftp.batch_download") }}
        </n-button>
        <n-button size="small" :loading="busy" @click="openMoveDlg">
          <template #icon><n-icon><MoveIcon /></n-icon></template>
          {{ t("sftp.batch_move") }}
        </n-button>
        <n-popconfirm @positive-click="batchDelete">
          <template #trigger>
            <n-button size="small" type="error" secondary :loading="busy">
              <template #icon><n-icon><DeleteIcon /></n-icon></template>
              {{ t("sftp.batch_delete") }}
            </n-button>
          </template>
          {{ t("sftp.batch_delete_confirm", { n: checkedKeys.length }) }}
        </n-popconfirm>
        <n-button size="small" quaternary @click="clearSelection">
          <template #icon><n-icon><CancelIcon /></n-icon></template>
          {{ t("sftp.batch_clear") }}
        </n-button>
      </n-space>

      <!-- 多檔上傳時講出進度：不然畫面只是卡著，不知道還有幾個 -->
      <div v-if="uploadProgress && uploadProgress.total > 1" class="sftp-filter-note">
        {{ t("sftp.uploading_progress", {
          done: uploadProgress.done + 1, total: uploadProgress.total, name: uploadProgress.name }) }}
      </div>

      <!-- 截斷要明講：畫面上少幾千個檔案而不說，等於騙人 -->
      <n-alert v-if="truncated" type="warning" :bordered="false" style="margin-bottom: 8px">
        {{ t("sftp.truncated") }}
      </n-alert>
      <!-- 篩選掉了多少也要說，否則會以為目錄裡就只有這幾個 -->
      <div v-if="filterText.trim()" class="sftp-filter-note">
        {{ t("sftp.filter_note", { shown: shownEntries.length, total: entries.length }) }}
      </div>

      <!-- 名稱輸入（新增資料夾 / 重新命名）：應用程式自己的對話框 -->
      <n-modal v-model:show="nameDlgShow" preset="card" style="width: 420px; max-width: 92vw"
               :title="nameDlgTitle">
        <n-input v-if="nameDlg" v-model:value="nameDlg.value" autofocus
                 :placeholder="t('sftp.name_ph')" @keyup.enter="submitNameDlg" />
        <template #footer>
          <n-space justify="end">
            <n-button @click="nameDlg = null">{{ t("common.cancel") }}</n-button>
            <n-button type="primary" :loading="busy" @click="submitNameDlg">
              {{ t("common.confirm") }}
            </n-button>
          </n-space>
        </template>
      </n-modal>

      <!-- 資料夾有內容：刪掉整棵樹是破壞性的，要明確問過，並且把數量講出來 -->
      <n-modal :show="!!confirmRecursive" preset="card" style="width: 460px; max-width: 92vw"
               :title="t('sftp.delete_dir_title')" @update:show="(v: boolean) => {
                 if (!v) confirmRecursive = null;
               }">
        <n-space vertical size="small">
          <span>{{ confirmRecursive?.detail }}</span>
          <n-alert type="warning" :bordered="false" :show-icon="true">
            {{ t("sftp.delete_dir_warning") }}
          </n-alert>
        </n-space>
        <template #footer>
          <n-space justify="end">
            <n-button @click="confirmRecursive = null">{{ t("common.cancel") }}</n-button>
            <n-button type="error" :loading="busy" @click="doDeleteRecursive">
              {{ t("sftp.delete_dir_confirm") }}
            </n-button>
          </n-space>
        </template>
      </n-modal>

      <!-- 移動：可直接打路徑，也可以點目錄一層層瀏覽 -->
      <n-modal v-model:show="moveDlg" preset="card" style="width: 560px; max-width: 94vw"
               :title="t('sftp.move_title', { n: checkedKeys.length })">
        <n-space vertical :size="10" style="width:100%">
          <n-input-group>
            <n-input v-model:value="moveDest" class="mono" :placeholder="t('sftp.move_path_ph')"
                     @keyup.enter="browseMove(moveDest)" />
            <n-button :loading="moveLoading" @click="browseMove(moveDest)">
              {{ t("sftp.move_go") }}
            </n-button>
          </n-input-group>
          <div class="move-tree">
            <div class="move-row" @click="moveUp">
              <n-icon :size="16"><UpLevelIcon /></n-icon><span>{{ t("sftp.up") }}</span>
            </div>
            <div v-for="d in moveDirs" :key="d.path" class="move-row" @click="browseMove(d.path)">
              <n-icon :size="16" color="#18a058"><FilesIcon /></n-icon><span>{{ d.name }}</span>
            </div>
            <!-- 空目錄也要講出來，不然會以為是壞掉了 -->
            <div v-if="!moveDirs.length && !moveLoading" class="move-empty">
              {{ t("sftp.move_no_subdirs") }}
            </div>
          </div>
        </n-space>
        <template #footer>
          <n-space justify="end">
            <n-button @click="moveDlg = false">{{ t("common.cancel") }}</n-button>
            <n-button type="primary" :loading="busy" @click="confirmMove">
              {{ t("sftp.move_here", { dir: moveDest }) }}
            </n-button>
          </n-space>
        </template>
      </n-modal>

      <div class="sftp-table" :class="{ 'sftp-full': fullHeight }">
        <n-data-table :columns="cols" :data="shownEntries" :loading="busy" size="small"
                      :row-key="(r: SftpEntry) => r.path"
                      :checked-row-keys="checkedKeys"
                      :pagination="pagination"
                      :bordered="false" flex-height style="height:100%" virtual-scroll
                      @update:checked-row-keys="(k: any) => (checkedKeys = k as string[])"
                      @update:sorter="onSorterChange" />
      </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 版面與 SshTerminal 對齊：全頁模式時表單置中、內容區填滿剩餘高度 */
.sftp-wrap { width: 100%; }
.sftp-wrap.sftp-full { height: 100%; display: flex; flex-direction: column; }
.sftp-wrap.sftp-center { justify-content: center; align-items: center; }
.sftp-wrap.sftp-center .sftp-form { width: 560px; max-width: 92vw; }
.sftp-form { max-width: 560px; }
.sftp-area { display: flex; flex-direction: column; }
/* 檔案操作區的外框：與 SSH 終端機那個深色框同一個角色 —— 把「這一塊是遠端主機的內容」
   框起來，狀態列留在框外。 */
.sftp-panel { border: 1px solid rgba(128, 128, 128, .28); border-radius: 8px;
  padding: 10px; background: #fff; box-shadow: 0 1px 3px rgba(0, 0, 0, .06);
  display: flex; flex-direction: column; min-height: 0; }
.sftp-panel.sftp-full { flex: 1; }
/* 拖曳中：整塊面板變成放置區 */
.sftp-panel { position: relative; }
.sftp-panel.sftp-dropping { border-color: #18a058; border-style: dashed; }
.sftp-dropzone { position: absolute; inset: 0; z-index: 5; display: flex; gap: 10px;
  flex-direction: column; align-items: center; justify-content: center; pointer-events: none;
  background: rgba(24, 160, 88, .10); color: #18a058; font-weight: 600; border-radius: 8px; }
html[data-theme="dark"] .sftp-panel { background: #10161f; border-color: rgba(200, 210, 230, .18);
  box-shadow: none; }
.sftp-area.sftp-full { flex: 1; min-height: 0; }
.sftp-table { height: 420px; }
.sftp-table.sftp-full { flex: 1; height: auto; min-height: 0; }
.sftp-pathbar { margin-bottom: 10px; }

/* 狀態列 —— 與 SSH 主控台同一套（同樣的圓角膠囊、同樣的狀態配色） */
.sftp-toolbar { display: flex; justify-content: space-between; align-items: center;
  padding: 4px 2px; gap: 8px; margin-bottom: 8px; }
.sftp-status { font-size: 13px; display: inline-flex; align-items: center; gap: 7px;
  padding: 3px 11px; border-radius: 999px; font-weight: 500;
  background: rgba(128, 128, 128, .12); color: #888; }
.sftp-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }
.sftp-ip { opacity: .7; font-variant-numeric: tabular-nums; }
.sftp-status[data-state="connected"] { color: #18a058; background: rgba(24, 160, 88, .14); }
.sftp-status[data-state="connected"] .sftp-dot { animation: sftp-pulse 1.8s infinite; }
.sftp-status[data-state="connecting"] { color: #d99812; background: rgba(217, 152, 18, .14); }
.sftp-status[data-state="error"] { color: #d03050; background: rgba(208, 48, 80, .14); }
.sftp-status[data-state="closed"] { color: #888; background: rgba(128, 128, 128, .14); }
@keyframes sftp-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(24, 160, 88, .5); }
  70%  { box-shadow: 0 0 0 6px rgba(24, 160, 88, 0); }
  100% { box-shadow: 0 0 0 0 rgba(24, 160, 88, 0); }
}
/* 協定標籤（與 SSH 的 conn-proto 同一套，換個色） */
.conn-proto { font-weight: 700; font-size: 11px; letter-spacing: .4px; line-height: 1;
  padding: 2px 7px; border-radius: 999px; }
.conn-proto--sftp { color: #2080f0; background: rgba(32,128,240,.16); }
/* 已中斷：反灰並停用互動，讓使用者一眼看出連線沒了（與 SSH 相同處理） */
.term-dim { filter: grayscale(1) brightness(.55); pointer-events: none; transition: filter .25s; }
/* 連線中斷：面板整片罩住。用 ::after 疊一層半透明底，內容維持在下面看得到
   （知道原本有什麼），但整片不可互動 —— 逐塊 disabled 一定會漏掉某一塊。 */
.sftp-panel.sftp-offline { position: relative; }
.sftp-panel.sftp-offline > *:not(.sftp-offline-mask) {
  filter: grayscale(1) brightness(.6);
  pointer-events: none;
  user-select: none;
}
.sftp-offline-mask {
  position: absolute; inset: 0; z-index: 3;
  display: flex; align-items: center; justify-content: center;
  background: rgba(128, 128, 128, .18);
  backdrop-filter: blur(1.5px);
  border-radius: inherit;
}
.sftp-offline-box {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 20px 26px; border-radius: 10px;
  background: var(--jt-card-bg, #fff);
  box-shadow: 0 6px 24px rgba(0, 0, 0, .18);
  max-width: 420px; text-align: center;
}
.sftp-offline-title { font-weight: 600; }
.sftp-offline-hint { font-size: 12.5px; opacity: .75; line-height: 1.5; }
:deep(.n-card > .n-card-header) { display: flex; align-items: center; padding-top: 12px; padding-bottom: 12px; }
.sftp-saved-row { display: flex; align-items: center; margin-bottom: 18px; }
.sftp-saved-label { width: 92px; flex: none; box-sizing: border-box; text-align: right;
  padding-right: 12px; font-size: 14px; }
.sftp-saved-row :deep(.n-button) { margin-left: 6px; }
.mono :deep(input) { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
/* 表格內的名稱由 render function 產生（不帶 data-v），所以要用 :deep 才套得到；
   對齊本身走行內樣式，見 cols 的 render。 */
:deep(.sftp-dir) { color: var(--primary-color, #18a058); cursor: pointer; font-weight: 600; }
:deep(.sftp-file) { cursor: default; }
.sftp-batchbar { margin-bottom: 10px; padding: 6px 10px; border-radius: 6px;
  background: rgba(32, 128, 240, .08); }
.sftp-batch-count { font-size: 13px; font-weight: 500; }
.sftp-filter-note { font-size: 12px; opacity: .7; margin-bottom: 6px; }
</style>
