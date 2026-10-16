#!/bin/sh
# Issues short-lived workload certificates (pki-issue). Each identity gets a SPIFFE ID
# spiffe://jarvis.home/<domain>/<service> and lands only in its own domain's volume
# (/certs/<domain>), so no domain can read another's key. Renewal: rerun every 8 hours.
set -eu
export STEPPATH=/tmp/step

ca=https://pki-ca:9000
root=/ca/certs/root_ca.crt  # the CA volume, read-only; its keys stay encrypted
while read -r domain service; do
  case "$domain" in ''|'#'*) continue ;; esac
  out="/certs/$domain"
  [ -d "$out" ] || { echo "no volume for domain $domain" >&2; exit 1; }
  umask 077
  step ca certificate "$service.$domain.jarvis.home" "$out/$service.crt.new" "$out/$service.key.new" \
    --san "spiffe://jarvis.home/$domain/$service" --san "$service" \
    --provisioner jarvis-issuer --provisioner-password-file /run/secrets/pki_provisioner_password \
    --ca-url "$ca" --root "$root" --not-after 24h --force >/dev/null
  mv "$out/$service.key.new" "$out/$service.key"
  mv "$out/$service.crt.new" "$out/$service.crt"
  cp "$root" "$out/root_ca.crt"
  echo "issued spiffe://jarvis.home/$domain/$service"
done < /etc/jarvis/identities
