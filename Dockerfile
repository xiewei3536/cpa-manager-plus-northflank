FROM seakee/cpa-manager-plus:v1.11.7@sha256:a4ae26a1160b61749aee4537d50edd763ff6d6ba3a1d5dfee7b71952e5e928e1

ENV HTTP_ADDR=0.0.0.0:7860 \
    USAGE_DATA_DIR=/data \
    USAGE_DB_PATH=/data/usage.sqlite \
    CPA_MANAGER_DATA_KEY_PATH=/data/data.key \
    USAGE_CORS_ORIGINS=*

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:7860/health || exit 1
