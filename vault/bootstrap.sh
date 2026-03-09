#!/bin/sh
set -eu

export VAULT_ADDR="${VAULT_ADDR:-http://vault:8200}"
export VAULT_KMIP_MOUNT="${VAULT_KMIP_MOUNT:-kmip}"
export VAULT_KMIP_LISTEN_ADDR="${VAULT_KMIP_LISTEN_ADDR:-0.0.0.0:5696}"
export VAULT_KMIP_SCOPE="${VAULT_KMIP_SCOPE:-demo-scope}"
export VAULT_KMIP_ROLE="${VAULT_KMIP_ROLE:-demo-admin}"

SHARED_DIR="/shared"
INIT_JSON="${SHARED_DIR}/init.json"
ROOT_TOKEN_FILE="${SHARED_DIR}/root-token"

mkdir -p "${SHARED_DIR}"

echo "[bootstrap] Waiting for Vault HTTP API..."
until wget -qO- "${VAULT_ADDR}/v1/sys/health?standbyok=true&sealedcode=204&uninitcode=204" >/dev/null 2>&1; do
  sleep 2
done

echo "[bootstrap] Checking init state..."
INIT_STATE="$(wget -qO- "${VAULT_ADDR}/v1/sys/init" | tr -d '\n' | sed -n 's/.*"initialized":[[:space:]]*\(true\|false\).*/\1/p')"

if [ "${INIT_STATE}" = "false" ]; then
  echo "[bootstrap] Initializing Vault..."
  vault operator init -key-shares=3 -key-threshold=3 -format=json > "${INIT_JSON}"
else
  echo "[bootstrap] Vault already initialized."
fi

if [ ! -f "${INIT_JSON}" ]; then
  echo "[bootstrap] ERROR: ${INIT_JSON} not found."
  exit 1
fi

JSON_ONE_LINE="$(tr -d '\n' < "${INIT_JSON}")"

ROOT_TOKEN="$(printf '%s' "${JSON_ONE_LINE}" | sed -n 's/.*"root_token":[[:space:]]*"\([^"]*\)".*/\1/p')"
UNSEAL_1="$(printf '%s' "${JSON_ONE_LINE}" | sed -n 's/.*"unseal_keys_b64":[[:space:]]*\[[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*"\([^"]*\)".*/\1/p')"
UNSEAL_2="$(printf '%s' "${JSON_ONE_LINE}" | sed -n 's/.*"unseal_keys_b64":[[:space:]]*\[[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*"\([^"]*\)".*/\2/p')"
UNSEAL_3="$(printf '%s' "${JSON_ONE_LINE}" | sed -n 's/.*"unseal_keys_b64":[[:space:]]*\[[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*"\([^"]*\)".*/\3/p')"

if [ -z "${ROOT_TOKEN}" ] || [ -z "${UNSEAL_1}" ] || [ -z "${UNSEAL_2}" ] || [ -z "${UNSEAL_3}" ]; then
  echo "[bootstrap] ERROR: Failed to parse init.json"
  cat "${INIT_JSON}"
  exit 1
fi

printf '%s' "${ROOT_TOKEN}" > "${ROOT_TOKEN_FILE}"
export VAULT_TOKEN="${ROOT_TOKEN}"

echo "[bootstrap] Unsealing Vault..."
vault operator unseal "${UNSEAL_1}"
vault operator unseal "${UNSEAL_2}"
vault operator unseal "${UNSEAL_3}"

echo "[bootstrap] Waiting for Vault active state..."
until wget -qO- "${VAULT_ADDR}/v1/sys/health?standbyok=true" >/dev/null 2>&1; do
  sleep 2
done

echo "[bootstrap] Enabling KMIP mount if needed..."
if ! vault secrets list -format=json | grep -q "\"${VAULT_KMIP_MOUNT}/\""; then
  vault secrets enable -path="${VAULT_KMIP_MOUNT}" kmip
fi

echo "[bootstrap] Configuring KMIP listener..."
vault write "${VAULT_KMIP_MOUNT}/config" listen_addrs="${VAULT_KMIP_LISTEN_ADDR}"

echo "[bootstrap] Creating KMIP scope if needed..."
if ! vault list -format=json "${VAULT_KMIP_MOUNT}/scope" 2>/dev/null | grep -q "\"${VAULT_KMIP_SCOPE}\""; then
  vault write -f "${VAULT_KMIP_MOUNT}/scope/${VAULT_KMIP_SCOPE}"
fi

echo "[bootstrap] Creating KMIP role..."
vault write "${VAULT_KMIP_MOUNT}/scope/${VAULT_KMIP_SCOPE}/role/${VAULT_KMIP_ROLE}" operation_all=true

echo "[bootstrap] Bootstrap complete."
