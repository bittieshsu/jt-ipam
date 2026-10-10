-- 稽核表交給一個不能登入的資料庫角色（scripts/jt-ipam.sh harden-audit；安裝與升級會自動跑）。
--
-- 為什麼：0149／0201 的觸發器擋 UPDATE／DELETE／TRUNCATE，但表的擁有者可以停用觸發器。
-- 應用程式的資料庫帳號原本就是擁有者 —— 拿到它的密碼（backend.env）就能停用保護後改寫記錄。
-- 交給 jt_ipam_audit_owner 之後，應用程式帳號只剩 SELECT／INSERT：停用觸發器、改寫、刪除、
-- 清空都要表的擁有者或超級使用者。整張表被刪掉（資料庫擁有者仍做得到）會被錨定與外部轉送抓到。
--
-- 用法：sudo -u postgres psql -v ON_ERROR_STOP=1 -v app=jt_ipam -d jt_ipam -f audit-harden.sql
-- 重跑安全。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jt_ipam_audit_owner') THEN
        CREATE ROLE jt_ipam_audit_owner NOLOGIN;
    END IF;
END $$;

ALTER TABLE public.audit_logs OWNER TO jt_ipam_audit_owner;
ALTER FUNCTION public.audit_logs_append_only() OWNER TO jt_ipam_audit_owner;
ALTER FUNCTION public.audit_logs_no_truncate() OWNER TO jt_ipam_audit_owner;

REVOKE ALL ON public.audit_logs FROM :"app";
GRANT SELECT, INSERT ON public.audit_logs TO :"app";

SELECT pg_get_serial_sequence('public.audit_logs', 'id') AS audit_seq \gset
ALTER SEQUENCE :audit_seq OWNER TO jt_ipam_audit_owner;
REVOKE ALL ON SEQUENCE :audit_seq FROM :"app";
GRANT USAGE, SELECT ON SEQUENCE :audit_seq TO :"app";
