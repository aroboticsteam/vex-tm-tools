#!/bin/sh
# Self-signed wildcard certificate (BASE + *.BASE) for Traefik.
# Regenerated only when BASE_DOMAIN or EXTRA_CA_FILE changes.
set -eu
host="${BASE_DOMAIN:-vex.localhost}"
marker="$host:$(cksum < /extra-ca.pem)"
if [ -f /certs/cert.pem ] && [ "$(cat /certs/host 2>/dev/null)" = "$marker" ]; then
  echo "certificate for $host already present"; exit 0
fi
apk add --no-cache openssl ca-certificates >/dev/null
san="DNS:localhost,IP:127.0.0.1,IP:::1"
if echo "$host" | grep -Eq '^[0-9.]+$|:'; then san="$san,IP:$host"; else san="$san,DNS:$host,DNS:*.$host"; fi
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /certs/key.pem -out /certs/cert.pem \
  -subj "/O=TMTools/CN=$host" -addext "subjectAltName=$san"
# CA bundle for the app/lego/cloudflared (SSL_CERT_FILE): system roots + our cert
# (+ EXTRA_CA_FILE), so they trust the self-signed fallback without losing public CAs.
cat /etc/ssl/certs/ca-certificates.crt /certs/cert.pem > /certs/ca-bundle.pem
if [ -s /extra-ca.pem ]; then cat /extra-ca.pem >> /certs/ca-bundle.pem; echo "added EXTRA_CA_FILE to the CA bundle"; fi
echo "$marker" > /certs/host
chmod 644 /certs/*.pem
echo "generated self-signed certificate for $san"
