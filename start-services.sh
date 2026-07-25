#!/bin/sh
set -eu

umask 077

CPA_DATA_DIR="${CPA_DATA_DIR:-/data/cpa}"
CPA_CONFIG="${CPA_CONFIG:-${CPA_DATA_DIR}/config.yaml}"
CPA_MANAGEMENT_KEY_FILE="${CPA_DATA_DIR}/management.key"

mkdir -p \
  "${CPA_DATA_DIR}/auths" \
  "${CPA_DATA_DIR}/home" \
  "${CPA_DATA_DIR}/logs" \
  "${CPA_DATA_DIR}/plugins"

if [ -s "${CPA_MANAGEMENT_KEY_FILE}" ]; then
  effective_management_key="$(tr -d '\r\n' <"${CPA_MANAGEMENT_KEY_FILE}")"
elif [ -n "${CPA_MANAGEMENT_KEY:-}" ]; then
  effective_management_key="${CPA_MANAGEMENT_KEY}"
elif [ -n "${MANAGEMENT_PASSWORD:-}" ]; then
  effective_management_key="${MANAGEMENT_PASSWORD}"
elif [ -n "${CPA_MANAGER_ADMIN_KEY:-}" ]; then
  effective_management_key="${CPA_MANAGER_ADMIN_KEY}"
else
  echo "A CPA management key is required" >&2
  exit 1
fi

if [ ! -s "${CPA_MANAGEMENT_KEY_FILE}" ]; then
  key_tmp="${CPA_MANAGEMENT_KEY_FILE}.tmp"
  printf '%s\n' "${effective_management_key}" >"${key_tmp}"
  mv "${key_tmp}" "${CPA_MANAGEMENT_KEY_FILE}"
fi

export CPA_MANAGEMENT_KEY="${effective_management_key}"
export MANAGEMENT_PASSWORD="${effective_management_key}"

if [ ! -f "${CPA_CONFIG}" ]; then
  : "${CPA_GATEWAY_API_KEY:?CPA_GATEWAY_API_KEY is required on first startup}"
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
snapshot_pid=""
shutting_down=0

shutdown_services() {
  if [ "${shutting_down}" -eq 1 ]; then
    return
  fi
  shutting_down=1

  echo "Stopping nginx, snapshot worker, CPA Manager Plus and CLIProxyAPI..."
  if [ -n "${nginx_pid}" ]; then
    kill -QUIT "${nginx_pid}" 2>/dev/null || true
  fi
  if [ -n "${snapshot_pid}" ]; then
    kill -TERM "${snapshot_pid}" 2>/dev/null || true
  fi
  if [ -n "${cpamp_pid}" ]; then
    kill -TERM "${cpamp_pid}" 2>/dev/null || true
  fi
  if [ -n "${cpa_pid}" ]; then
    kill -TERM "${cpa_pid}" 2>/dev/null || true
  fi

  shutdown_attempts=0
  while [ "${shutdown_attempts}" -lt 100 ]; do
    any_running=0
    for service_pid in "${nginx_pid}" "${snapshot_pid}" "${cpamp_pid}" "${cpa_pid}"; do
      if [ -n "${service_pid}" ] && kill -0 "${service_pid}" 2>/dev/null; then
        any_running=1
      fi
    done
    if [ "${any_running}" -eq 0 ]; then
      break
    fi
    shutdown_attempts=$((shutdown_attempts + 1))
    sleep 0.1
  done

  for service_pid in "${nginx_pid}" "${snapshot_pid}" "${cpamp_pid}" "${cpa_pid}"; do
    if [ -n "${service_pid}" ] && kill -0 "${service_pid}" 2>/dev/null; then
      kill -KILL "${service_pid}" 2>/dev/null || true
    fi
  done

  if [ -n "${nginx_pid}" ]; then
    wait "${nginx_pid}" 2>/dev/null || true
  fi
  if [ -n "${snapshot_pid}" ]; then
    wait "${snapshot_pid}" 2>/dev/null || true
  fi
  if [ -n "${cpamp_pid}" ]; then
    wait "${cpamp_pid}" 2>/dev/null || true
  fi
  if [ -n "${cpa_pid}" ]; then
    wait "${cpa_pid}" 2>/dev/null || true
  fi

  if [ -f "${USAGE_DB_PATH}" ]; then
    echo "Uploading final consistent SQLite snapshot..."
    if ! /usr/local/bin/snapshot-manager.py backup --force; then
      echo "Final SQLite snapshot upload failed" >&2
      return 1
    fi
  fi
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

mkdir -p "$(dirname "${USAGE_DB_PATH}")"
/usr/local/bin/snapshot-manager.py restore

env -u HF_TOKEN /usr/local/bin/CLIProxyAPI -config "${CPA_CONFIG}" &
cpa_pid=$!

env -u HF_TOKEN /usr/local/bin/cpa-manager-plus &
cpamp_pid=$!

if ! wait_for_service "CLIProxyAPI" "http://127.0.0.1:8317/healthz" "${cpa_pid}"; then
  shutdown_services
  exit 1
fi

if ! wait_for_service "CPA Manager Plus" "http://127.0.0.1:18317/health" "${cpamp_pid}"; then
  shutdown_services
  exit 1
fi

/usr/local/bin/snapshot-manager.py watch \
  --interval "${CPAMP_SNAPSHOT_INTERVAL_SECONDS:-120}" &
snapshot_pid=$!

nginx -t
env -u HF_TOKEN nginx -g 'daemon off;' &
nginx_pid=$!

echo "Integrated CPA gateway and manager are ready on port 7860"

while kill -0 "${cpa_pid}" 2>/dev/null \
  && kill -0 "${cpamp_pid}" 2>/dev/null \
  && kill -0 "${snapshot_pid}" 2>/dev/null \
  && kill -0 "${nginx_pid}" 2>/dev/null; do
  sleep 2
done

echo "A managed service exited unexpectedly" >&2
shutdown_services
exit 1
