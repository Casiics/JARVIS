#!/bin/sh
# One-shot initialisation of the workload CA (pki-init). Runs without any network.
# Unlike the step-ca image's own init, it never prints a password.
set -eu

export STEPPATH=/home/step
if [ ! -f "$STEPPATH/config/ca.json" ]; then
  step ca init --deployment-type standalone --name "Jarvis Workload CA" \
    --dns pki-ca --address :9000 --provisioner jarvis-issuer \
    --password-file /run/secrets/pki_ca_password \
    --provisioner-password-file /run/secrets/pki_provisioner_password >/dev/null
  # Workload certificates live 24 hours at most; renewal runs every 8 hours.
  step ca provisioner update jarvis-issuer \
    --x509-min-dur 5m --x509-default-dur 24h --x509-max-dur 24h >/dev/null
  echo "workload CA initialised"
else
  echo "workload CA already initialised"
fi
# The issuer reads the public root certificate; the keys under secrets/ stay owner-only.
chmod 0755 "$STEPPATH/certs"
chmod 0644 "$STEPPATH/certs/root_ca.crt" "$STEPPATH/certs/intermediate_ca.crt"
echo "root fingerprint $(step certificate fingerprint "$STEPPATH/certs/root_ca.crt")"
