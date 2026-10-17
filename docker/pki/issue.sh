#!/bin/sh
# Issues short-lived workload certificates (pki-issue). Each identity gets a SPIFFE ID
# spiffe://jarvis.home/<domain>/<service> and lands only in its own domain's volume, in its
# own directory /certs/<domain>/<service>, which the consuming container mounts alone.
# Renewal: rerun every 8 hours.
#
# Layout per identity, so a reader never sees a new certificate with an old key:
#   .v<random>/{tls.crt,tls.key,root_ca.crt}   one issued version, files owned by the consumer
#   current -> .v<random>                       swapped with one rename
#   tls.crt, tls.key, root_ca.crt -> current/…  stable names for consumers
set -eu
export STEPPATH=/tmp/step

ca=https://pki-ca:9000
root=/ca/certs/root_ca.crt  # the CA volume, read-only; its keys stay encrypted
while read -r domain service uid dns; do
  case "$domain" in ''|'#'*) continue ;; esac
  case "$uid" in ''|*[!0-9]*) echo "identity $domain/$service has no uid" >&2; exit 1 ;; esac
  [ -d "/certs/$domain" ] || { echo "no volume for domain $domain" >&2; exit 1; }
  # Files from the flat layout before per-service directories.
  rm -f "/certs/$domain/$service.crt" "/certs/$domain/$service.key" "/certs/$domain/root_ca.crt"
  dir="/certs/$domain/$service"
  mkdir -p "$dir"
  chmod 0755 "$dir"
  umask 077
  new=$(mktemp -d "$dir/.vXXXXXX")
  set -- --san "spiffe://jarvis.home/$domain/$service"
  [ -z "$dns" ] || set -- "$@" --san "$dns"
  step ca certificate "$service.$domain.jarvis.home" "$new/tls.crt" "$new/tls.key" "$@" \
    --provisioner jarvis-issuer --provisioner-password-file /run/secrets/pki_provisioner_password \
    --ca-url "$ca" --root "$root" --not-after 24h --force >/dev/null
  cp "$root" "$new/root_ca.crt"
  # Modes before ownership: root without capabilities may change only its own files. The
  # directory stays root's, so the next run can remove the files without DAC_OVERRIDE.
  chmod 0444 "$new/tls.crt" "$new/root_ca.crt"
  chmod 0400 "$new/tls.key"
  chmod 0755 "$new"
  chown "$uid:$uid" "$new/tls.crt" "$new/tls.key" "$new/root_ca.crt"
  previous=$(readlink "$dir/current" 2>/dev/null || true)
  ln -s "${new##*/}" "$dir/current.new"
  mv -T "$dir/current.new" "$dir/current"
  for name in tls.crt tls.key root_ca.crt; do
    [ -L "$dir/$name" ] || ln -s "current/$name" "$dir/$name"
  done
  # Keep the version just replaced for readers that opened it a moment ago.
  for old in "$dir"/.v*; do
    case "${old##*/}" in "${new##*/}"|"$previous") ;; *) rm -rf "$old" ;; esac
  done
  echo "issued spiffe://jarvis.home/$domain/$service"
done < /etc/jarvis/identities
