#!/bin/sh
set -eu

umask 077

CPA_DATA_DIR="${CPA_DATA_DIR:-/data/cpa}"
CPAMP_DATA_DIR="${USAGE_DATA_DIR:-/data/cpamp}"
CPA_CONFIG="${CPA_CONFIG:-${CPA_DATA_DIR}/config.yaml}"
USAGE_DB_PATH="${USAGE_DB_PATH:-${CPAMP_DATA_DIR}/usage.sqlite}"
CPA_MANAGER_DATA_KEY_PATH="${CPA_MANAGER_DATA_KEY_PATH:-${CPAMP_DATA_DIR}/data.key}"
CPA_MANAGEMENT_KEY_FILE="${CPA_DATA_DIR}/management.key"

CPA_MANAGER_ADMIN_KEY="${CPA_MANAGER_ADMIN_KEY:-${STACK_PASSWORD:-}}"
CPA_MANAGEMENT_KEY="${CPA_MANAGEMENT_KEY:-${STACK_PASSWORD:-}}"
CPA_GATEWAY_API_KEY="${CPA_GATEWAY_API_KEY:-${STACK_PASSWORD:-}}"

: "${HF_TOKEN:?HF_TOKEN with write access to STATE_BUCKET is required}"
: "${STATE_BUCKET:?STATE_BUCKET is required}"
: "${CPA_MANAGER_ADMIN_KEY:?CPA_MANAGER_ADMIN_KEY or STACK_PASSWORD is required}"
: "${CPA_MANAGEMENT_KEY:?CPA_MANAGEMENT_KEY or STACK_PASSWORD is required}"
: "${CPA_GATEWAY_API_KEY:?CPA_GATEWAY_API_KEY or STACK_PASSWORD is required}"

USAGE_DATA_DIR="${CPAMP_DATA_DIR}"
export CPA_DATA_DIR CPAMP_DATA_DIR USAGE_DATA_DIR CPA_CONFIG USAGE_DB_PATH
export CPA_MANAGER_DATA_KEY_PATH CPA_MANAGER_ADMIN_KEY CPA_MANAGEMENT_KEY
export CPA_GATEWAY_API_KEY
export MANAGEMENT_PASSWORD="${CPA_MANAGEMENT_KEY}"

mkdir -p "$(dirname "${USAGE_DB_PATH}")" "${CPA_DATA_DIR}"

echo "Restoring the newest verified state generation from ${STATE_BUCKET}..."
/usr/local/bin/state-manager.py restore

if [ -s "${CPA_MANAGEMENT_KEY_FILE}" ]; then
  persisted_management_key="$(tr -d '\r\n' <"${CPA_MANAGEMENT_KEY_FILE}")"
  if [ "${persisted_management_key}" != "${CPA_MANAGEMENT_KEY}" ]; then
    echo "CPA_MANAGEMENT_KEY does not match restored management.key" >&2
    exit 1
  fi
else
  key_tmp="${CPA_MANAGEMENT_KEY_FILE}.tmp"
  printf '%s\n' "${CPA_MANAGEMENT_KEY}" >"${key_tmp}"
  mv "${key_tmp}" "${CPA_MANAGEMENT_KEY_FILE}"
fi

mkdir -p \
  "${CPA_DATA_DIR}/auths" \
  "${CPA_DATA_DIR}/home" \
  "${CPA_DATA_DIR}/logs" \
  "${CPA_DATA_DIR}/plugins" \
  "${CPAMP_DATA_DIR}"

if [ ! -f "${CPA_CONFIG}" ]; then
  config_tmp="${CPA_CONFIG}.tmp"
  cat >"${config_tmp}" <<EOF
host: "127.0.0.1"
port: 8317

tls:
  enable: false
  cert: ""
  key: ""

remote-management:
  allow-remote: false
  secret-key: ""
  disable-control-panel: true

auth-dir: "${CPA_DATA_DIR}/auths"

api-keys:
  - "${CPA_GATEWAY_API_KEY}"

debug: false
commercial-mode: false
logging-to-file: false
logs-max-total-size-mb: 256
usage-statistics-enabled: true
redis-usage-queue-retention-seconds: 3600

plugins:
  enabled: false
  dir: "${CPA_DATA_DIR}/plugins"

routing:
  strategy: "round-robin"
EOF
  mv "${config_tmp}" "${CPA_CONFIG}"
fi

cpa_pid=""
cpamp_pid=""
nginx_pid=""
state_pid=""
shutting_down=0
startup_ready=0

wait_for_pids() {
  maximum_attempts="$1"
  shift
  attempts=0
  while [ "${attempts}" -lt "${maximum_attempts}" ]; do
    any_running=0
    for service_pid in "$@"; do
      if [ -n "${service_pid}" ] && kill -0 "${service_pid}" 2>/dev/null; then
        any_running=1
      fi
    done
    if [ "${any_running}" -eq 0 ]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.1
  done
  return 1
}

reap_pid() {
  service_pid="$1"
  if [ -n "${service_pid}" ]; then
    wait "${service_pid}" 2>/dev/null || true
  fi
}

shutdown_services() {
  if [ "${shutting_down}" -eq 1 ]; then
    return 0
  fi
  shutting_down=1
  trap '' INT TERM
  backup_status=0

  echo "Stopping public traffic and the periodic state worker..."
  if [ -n "${nginx_pid}" ]; then
    kill -QUIT "${nginx_pid}" 2>/dev/null || true
  fi
  if [ -n "${state_pid}" ]; then
    kill -TERM "${state_pid}" 2>/dev/null || true
  fi
  wait_for_pids 150 "${nginx_pid}" "${state_pid}" || true

  for service_pid in "${nginx_pid}" "${state_pid}"; do
    if [ -n "${service_pid}" ] && kill -0 "${service_pid}" 2>/dev/null; then
      kill -KILL "${service_pid}" 2>/dev/null || true
    fi
  done
  reap_pid "${nginx_pid}"
  reap_pid "${state_pid}"

  echo "Stopping CPA Manager Plus and CLIProxyAPI..."
  if [ -n "${cpamp_pid}" ]; then
    kill -TERM "${cpamp_pid}" 2>/dev/null || true
  fi
  if [ -n "${cpa_pid}" ]; then
    kill -TERM "${cpa_pid}" 2>/dev/null || true
  fi
  wait_for_pids 200 "${cpamp_pid}" "${cpa_pid}" || true

  for service_pid in "${cpamp_pid}" "${cpa_pid}"; do
    if [ -n "${service_pid}" ] && kill -0 "${service_pid}" 2>/dev/null; then
      kill -KILL "${service_pid}" 2>/dev/null || true
    fi
  done
  reap_pid "${cpamp_pid}"
  reap_pid "${cpa_pid}"

  if [ "${startup_ready}" -eq 1 ] \
    && [ -s "${USAGE_DB_PATH}" ] \
    && [ -s "${CPA_MANAGER_DATA_KEY_PATH}" ] \
    && [ -s "${CPA_CONFIG}" ]; then
    echo "Uploading the final consistent state generation..."
    if ! /usr/local/bin/state-manager.py backup --force; then
      echo "Final state generation upload failed" >&2
      backup_status=1
    fi
  else
    echo "Final state generation skipped because startup never reached ready state"
  fi
  return "${backup_status}"
}

on_signal() {
  if shutdown_services; then
    exit 0
  fi
  exit 1
}

trap 'on_signal' INT TERM

wait_for_service() {
  service_name="$1"
  service_url="$2"
  service_pid="$3"
  attempts=0

  while [ "${attempts}" -lt 90 ]; do
    if ! kill -0 "${service_pid}" 2>/dev/null; then
      echo "${service_name} exited before becoming ready" >&2
      return 1
    fi
    if curl -fsS --max-time 2 "${service_url}" >/dev/null 2>&1; then
      echo "${service_name} is ready"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done

  echo "Timed out waiting for ${service_name}" >&2
  return 1
}

periodic_backup_loop() {
  interval="$1"
  worker_child=""
  worker_stopping=0

  stop_worker() {
    worker_stopping=1
    if [ -n "${worker_child}" ]; then
      kill -TERM "${worker_child}" 2>/dev/null || true
      wait "${worker_child}" 2>/dev/null || true
      worker_child=""
    fi
  }

  trap 'stop_worker' INT TERM
  echo "[state] lightweight periodic worker started (interval=${interval}s)"
  while [ "${worker_stopping}" -eq 0 ]; do
    sleep "${interval}" &
    worker_child=$!
    wait "${worker_child}" 2>/dev/null || true
    worker_child=""
    if [ "${worker_stopping}" -ne 0 ]; then
      break
    fi

    /usr/local/bin/state-manager.py backup &
    worker_child=$!
    if ! wait "${worker_child}"; then
      echo "[state] periodic backup failed; it will retry next interval" >&2
    fi
    worker_child=""
  done
  echo "[state] lightweight periodic worker stopped"
}

env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN \
  /usr/local/bin/CLIProxyAPI -config "${CPA_CONFIG}" &
cpa_pid=$!

env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN \
  /usr/local/bin/cpa-manager-plus &
cpamp_pid=$!

if ! wait_for_service "CLIProxyAPI" "http://127.0.0.1:8317/healthz" "${cpa_pid}"; then
  shutdown_services || true
  exit 1
fi

if ! wait_for_service "CPA Manager Plus" "http://127.0.0.1:18317/health" "${cpamp_pid}"; then
  shutdown_services || true
  exit 1
fi

echo "Publishing an initial verified state generation..."
if ! /usr/local/bin/state-manager.py backup --force; then
  shutdown_services || true
  exit 1
fi

periodic_backup_loop "${STATE_SNAPSHOT_INTERVAL_SECONDS:-60}" &
state_pid=$!

if ! nginx -t; then
  shutdown_services || true
  exit 1
fi
env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN nginx -g 'daemon off;' &
nginx_pid=$!

if ! wait_for_service "Nginx" "http://127.0.0.1:7860/health" "${nginx_pid}"; then
  shutdown_services || true
  exit 1
fi
startup_ready=1

echo "Integrated CPA gateway and manager are ready on port 7860"

while kill -0 "${cpa_pid}" 2>/dev/null \
  && kill -0 "${cpamp_pid}" 2>/dev/null \
  && kill -0 "${state_pid}" 2>/dev/null \
  && kill -0 "${nginx_pid}" 2>/dev/null; do
  sleep 2
done

echo "A managed service exited unexpectedly" >&2
shutdown_services || true
exit 1
