#!/bin/bash
# 可丟棄的 Kerberos 測試靶：Samba AD 網域控制站（領域 CORP.TEST），給 Hyper-V 的 WinRM Kerberos 驗證用
# （backend/app/services/winrm_kerberos.py）。只在 docker 內部網路，不綁主機的連接埠。
#
#   bash scripts/kerberos-test-kdc/run.sh          # 建映像、起容器、建帳號與 HTTP/hv1.corp.test 服務名稱
#   docker rm -f jtipam-samba-dc                   # 用完收掉
#
# 起好之後：
#   JTIPAM_TEST_KDC=<容器 IP> JTIPAM_TEST_KRB_USER=svc-ipam@CORP.TEST JTIPAM_TEST_KRB_PASS='Svc-Ipam-2026!' \
#   JTIPAM_TEST_KRB_SPN_HOST=hv1.corp.test pytest tests/test_hyperv_kerberos.py
#
# 注意：samba-tool 建的電腦帳號預設沒有 AES 金鑰（真的 AD 有），所以下面要補 msDS-SupportedEncryptionTypes 並重設密碼，
# 否則拿 HTTP/<主機> 服務票時會是「KDC has no support for encryption type」。
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t jtipam-samba-dc . >/dev/null
docker rm -f jtipam-samba-dc >/dev/null 2>&1 || true
docker run -d --name jtipam-samba-dc --hostname dc1.corp.test --privileged jtipam-samba-dc >/dev/null
for _ in $(seq 1 60); do
  docker exec jtipam-samba-dc bash -c 'samba-tool user list >/dev/null 2>&1' && break
  sleep 3
done
docker exec jtipam-samba-dc bash -c "
  samba-tool user create svc-ipam 'Svc-Ipam-2026!' >/dev/null
  samba-tool computer create hv1 >/dev/null
  samba-tool spn add HTTP/hv1.corp.test 'hv1\$'
  apt-get update -qq >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ldb-tools >/dev/null 2>&1
  printf 'dn: CN=hv1,CN=Computers,DC=corp,DC=test\nchangetype: modify\nreplace: msDS-SupportedEncryptionTypes\nmsDS-SupportedEncryptionTypes: 24\n' > /tmp/enc.ldif
  ldbmodify -H /var/lib/samba/private/sam.ldb /tmp/enc.ldif >/dev/null
  samba-tool user setpassword 'hv1\$' --newpassword='Hv1-Machine-Pass-2026!' >/dev/null
"
echo "KDC: $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' jtipam-samba-dc)  realm CORP.TEST  user svc-ipam@CORP.TEST"
