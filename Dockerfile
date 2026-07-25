FROM eceasy/cli-proxy-api:v7.2.99@sha256:7e828ffc1c56ff9fc9fb5e1cdeb802f902c1449c7a43b702c13a7b9bf26fca28 AS cpa
FROM seakee/cpa-manager-plus:v1.11.7@sha256:a4ae26a1160b61749aee4537d50edd763ff6d6ba3a1d5dfee7b71952e5e928e1 AS cpamp

FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl nginx tini tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=cpa /CLIProxyAPI/CLIProxyAPI /usr/local/bin/CLIProxyAPI
COPY --from=cpamp /usr/local/bin/cpa-manager-plus /usr/local/bin/cpa-manager-plus
COPY nginx.conf /etc/nginx/nginx.conf
COPY start-services.sh /usr/local/bin/start-services.sh

RUN chmod 0755 /usr/local/bin/CLIProxyAPI \
    /usr/local/bin/cpa-manager-plus \
    /usr/local/bin/start-services.sh

ENV HTTP_ADDR=127.0.0.1:18317 \
    USAGE_DATA_DIR=/data \
    USAGE_DB_PATH=/data/usage.sqlite \
    CPA_MANAGER_DATA_KEY_PATH=/data/data.key \
    CPA_UPSTREAM_URL=http://127.0.0.1:8317 \
    USAGE_COLLECTOR_MODE=auto \
    USAGE_RESP_QUEUE=usage \
    USAGE_RESP_POP_SIDE=right \
    USAGE_BATCH_SIZE=100 \
    USAGE_POLL_INTERVAL_MS=500 \
    USAGE_QUERY_LIMIT=50000 \
    USAGE_CORS_ORIGINS=* \
    HOME=/data/cpa/home \
    TZ=Asia/Taipei

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8317/healthz >/dev/null \
    && curl -fsS http://127.0.0.1:18317/health >/dev/null \
    && curl -fsS http://127.0.0.1:7860/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-services.sh"]
