-- 升級跑資料庫遷移之前暫時把稽核表還給應用程式帳號（遷移以應用程式帳號執行，可能要改這張表），
-- 遷移完成後升級腳本立刻再跑 audit-harden.sql。重跑安全；從沒 harden 過、或舊版還沒有觸發器函式也安全。
--
-- 用法：sudo -u postgres psql -v ON_ERROR_STOP=1 -v app=jt_ipam -d jt_ipam -f audit-unharden.sql
SELECT pg_get_serial_sequence('public.audit_logs', 'id') AS audit_seq \gset
ALTER TABLE public.audit_logs OWNER TO :"app";
ALTER SEQUENCE :audit_seq OWNER TO :"app";
SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'audit_logs_append_only') AS has_ao,
       EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'audit_logs_no_truncate') AS has_nt \gset
\if :has_ao
ALTER FUNCTION public.audit_logs_append_only() OWNER TO :"app";
\endif
\if :has_nt
ALTER FUNCTION public.audit_logs_no_truncate() OWNER TO :"app";
\endif
