import { createApp } from "vue";
import { createPinia } from "pinia";

import App from "@/App.vue";
import { router } from "@/router";
import { i18n } from "@/i18n";
import { reportPageLoad, startPageDiag } from "@/utils/pageDiag";

// 頁面載入診斷（「切回分頁就整頁重新載入」要靠這個分辨是誰重載的）
startPageDiag();

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(i18n);
app.mount("#app");
void router.isReady().then(() => reportPageLoad());
