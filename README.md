# CPA + CPA Manager Plus on Northflank

Single-container deployment of:

- CLIProxyAPI `v7.2.99`
- CPA Manager Plus `v1.11.7`
- Nginx on port `7860`

## Northflank settings

Create a **Combined Service** from this branch and use:

| Setting | Value |
| --- | --- |
| Build type | Dockerfile / BuildKit |
| Dockerfile | `/Dockerfile` |
| Build context | `/` |
| Public port | HTTP `7860` |
| Instances | `1` |
| Autoscaling | Off |
| Grace period | `60` seconds |
| Startup/readiness probe | HTTP `7860 /health` |
| Liveness probe | HTTP `7860 /healthz` |

Create a **Single Read/Write** persistent volume and mount it at `/data`.
The volume holds:

```text
/data/cpa/                 CPA configuration, credentials and plugins
/data/cpamp/usage.sqlite   Manager database
/data/cpamp/data.key       Manager encryption key
```

Northflank stops the old instance before attaching a Single Read/Write volume
to the replacement instance, so SQLite data survives restarts and deployments.
Keep the service at exactly one instance.

## Secrets

The shortest setup uses one Northflank secret:

```env
STACK_PASSWORD=<administrator-and-default-API-key>
```

For separate credentials, omit `STACK_PASSWORD` and set all three:

```env
CPA_MANAGER_ADMIN_KEY=<manager-login-key>
CPA_MANAGEMENT_KEY=<internal-CPA-management-key>
CPA_GATEWAY_API_KEY=<model-API-key>
```

`CPA_MANAGER_DATA_KEY` is optional for a new persistent volume. When omitted,
CPA Manager Plus creates `/data/cpamp/data.key` once and reuses it thereafter.

## Routes

- `/management.html` — CPA Manager Plus
- `/v1/*`, `/v1beta/*`, `/api/*` — CLIProxyAPI
- `/health` — Manager health
- `/healthz` — CPA health

## Migrating existing data

Stop the source service, then copy the verified files before starting this
service for the first time:

```text
usage.sqlite  -> /data/cpamp/usage.sqlite
data.key      -> /data/cpamp/data.key
cpa/*         -> /data/cpa/*
```

The database and `data.key` must be migrated together.
