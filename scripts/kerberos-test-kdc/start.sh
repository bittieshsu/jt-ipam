#!/bin/bash
set -e
if [ ! -f /var/lib/samba/private/sam.ldb ]; then
  rm -f /etc/samba/smb.conf
  samba-tool domain provision --realm=CORP.TEST --domain=CORP --server-role=dc --dns-backend=SAMBA_INTERNAL \
    --adminpass='Adm1n-Passw0rd!' --use-rfc2307 --host-name=dc1 >/tmp/provision.log 2>&1
  cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
fi
exec samba -i -M single
