FROM eceasy/cli-proxy-api:v7.2.99@sha256:7e828ffc1c56ff9fc9fb5e1cdeb802f902c1449c7a43b702c13a7b9bf26fca28 AS cpa

FROM alpine:3.21 AS cpamp-src
ARG CPAMP_COMMIT=f17729bb488c52d3405dbe08b86be21fad9d2802
RUN apk add --no-cache ca-certificates git \
    && git clone --filter=blob:none https://github.com/seakee/CPA-Manager-Plus.git /src \
    && git -C /src checkout --detach "${CPAMP_COMMIT}" \
    && test "$(git -C /src rev-parse HEAD)" = "${CPAMP_COMMIT}"

FROM node:22-alpine AS cpamp-web
WORKDIR /src
COPY --from=cpamp-src /src/package.json /src/package-lock.json ./
COPY --from=cpamp-src /src/apps/web/package.json ./apps/web/package.json
RUN npm ci
COPY --from=cpamp-src /src/apps/web ./apps/web
RUN VERSION=v1.11.7 npm --workspace apps/web run build \
    && test "$(wc -c < apps/web/dist/index.html)" -gt 1000000

FROM golang:1.24-alpine AS cpamp
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src/apps/manager-server
COPY --from=cpamp-src /src/apps/manager-server ./
RUN sed -i 's/pragma journal_mode = WAL/pragma journal_mode = DELETE/' internal/repository/sqlite/migrate.go \
    && sed -i 's/defaultMaxOpenConns    = 4/defaultMaxOpenConns    = 1/' internal/repository/sqlite/options.go \
    && sed -i 's/defaultMaxIdleConns    = 2/defaultMaxIdleConns    = 1/' internal/repository/sqlite/options.go \
    && grep -Fq 'pragma journal_mode = DELETE' internal/repository/sqlite/migrate.go \
    && ! grep -Fq 'pragma journal_mode = WAL' internal/repository/sqlite/migrate.go
COPY --from=cpamp-web /src/apps/web/dist/index.html internal/httpapi/web/management.html
RUN go mod download \
    && go test ./internal/repository/sqlite \
    && CGO_ENABLED=0 GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH:-amd64}" \
       go build -buildvcs=false -trimpath -o /out/cpa-manager-plus ./cmd/cpa-manager-plus

FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl nginx python3 python3-pip tini tzdata \
    && pip3 install --break-system-packages --no-cache-dir huggingface_hub==1.24.0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=cpa /CLIProxyAPI/CLIProxyAPI /usr/local/bin/CLIProxyAPI
COPY --from=cpamp /out/cpa-manager-plus /usr/local/bin/cpa-manager-plus
COPY nginx.conf /etc/nginx/nginx.conf
COPY snapshot-manager.py /usr/local/bin/snapshot-manager.py
COPY start-services.sh /usr/local/bin/start-services.sh

RUN chmod 0755 /usr/local/bin/CLIProxyAPI \
    /usr/local/bin/cpa-manager-plus \
    /usr/local/bin/snapshot-manager.py \
    /usr/local/bin/start-services.sh

ENV HTTP_ADDR=127.0.0.1:18317 \
    USAGE_DATA_DIR=/var/lib/cpamp \
    USAGE_DB_PATH=/var/lib/cpamp/usage.sqlite \
    CPA_MANAGER_DATA_KEY_PATH=/data/data.key \
    CPA_UPSTREAM_URL=http://127.0.0.1:8317 \
    CPAMP_SNAPSHOT_BUCKET=04191bw88tk/cr-data \
    CPAMP_SNAPSHOT_PREFIX=cpamp-snapshots \
    CPAMP_SNAPSHOT_INTERVAL_SECONDS=120 \
    CPAMP_SNAPSHOT_KEEP=8 \
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
