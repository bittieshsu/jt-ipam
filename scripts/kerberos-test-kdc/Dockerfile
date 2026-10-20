FROM debian:12
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq samba krb5-user winbind smbclient >/dev/null && rm -rf /var/lib/apt/lists/*
COPY start.sh /start.sh
CMD ["/start.sh"]
