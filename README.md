# CPA + CPA Manager Plus on Northflank Free

One container provides:

- CLIProxyAPI `v7.2.99`
- CPA Manager Plus `v1.11.7`
- Nginx on public port `7860`
- verified off-instance persistence in the private Hugging Face Bucket
  `04191bw88tk/cr-data`

## 一鍵更新 CPA 與 Plus

1. 到 GitHub 專案的 **Actions → Update CPA stack → Run workflow**。
2. `cpa_tag` 與 `plus_tag` 保持 `auto`，並勾選 `deploy`。
3. 按下 **Run workflow**；系統會分別選取 Docker Hub 最新的穩定
   `vX.Y.Z` 版本、固定映像 digest、更新兩個元件並部署到 Northflank。

若只想指定其中一個版本，在相應欄位填入完整 tag（例如 `v7.2.99`），
另一欄保留 `auto` 即可。更新不會清除 Bucket 中的既有資料。

The free service does **not** need a Northflank volume. Runtime state stays on
the container's local filesystem for SQLite compatibility, while immutable
generations are uploaded to the Bucket every 60 seconds and on graceful
shutdown. Startup tries generations newest-first and automatically falls back
when a checksum, archive manifest, `data.key`, required database schema, or
SQLite integrity check fails. Restore is fail-closed by default: Bucket API
authorization/network failures and missing prior state stop startup.

## Northflank service settings

Create one **Combined Service** from this repository/branch:

| Setting | Value |
| --- | --- |
| Build type | Dockerfile / BuildKit |
| Dockerfile | `/Dockerfile` |
| Build context | `/` |
| Public port | HTTP `7860` |
| Instances | exactly `1` |
| Autoscaling | Off |
| Grace period | `90` seconds |
| Startup/readiness probe | HTTP `7860 /health` |
| Liveness probe | HTTP `7860 /healthz` |

Do not create or mount a volume. Do not run two instances: each instance would
have an independent SQLite writer and generation timeline.

The 256 MB service does not keep Python resident. A lightweight shell timer
starts the Python backup process only for each snapshot and exits it afterward.

## Required Northflank secrets

Add these under **Environment → Secrets**:

```env
HF_TOKEN=<Hugging-Face-token-with-write-access-to-04191bw88tk/cr-data>
STACK_PASSWORD=<administrator-management-and-default-API-key>
```

`HF_TOKEN` is removed from the environments of CLIProxyAPI, CPA Manager Plus,
and Nginx. It is available only to the short-lived restore/backup process.

To use three different credentials, omit `STACK_PASSWORD` and add:

```env
CPA_MANAGER_ADMIN_KEY=<manager-login-key>
CPA_MANAGEMENT_KEY=<internal-CPA-management-key>
CPA_GATEWAY_API_KEY=<model-API-key>
```

CPA Manager Plus generates `data.key` on the original deployment and every
subsequent start restores that same file with the database. The initial
verified generation is published before Nginx begins accepting traffic.

## Persistence layout

Local application paths:

```text
/data/cpamp/usage.sqlite   Manager database
/data/cpamp/data.key       Manager encryption key
/data/cpa/config.yaml      CPA configuration
/data/cpa/management.key   CPA management-key continuity check
/data/cpa/auths/           CPA provider credentials
/data/cpa/home/            CPA runtime credential state
/data/cpa/plugins/         CPA plugins
```

Each Bucket object has this content-addressed form:

```text
northflank-state/state-<UTC timestamp>-<archive SHA-256>.tar.gz
```

The archive also contains a manifest with every file's size and SHA-256. A
SQLite online backup is used for live generations, followed by
`PRAGMA integrity_check`. Uploads are downloaded and verified before being
marked successful. Twelve generations are retained by default.

The startup importer also understands the previous HF Space layout:

```text
cpamp-snapshots/usage-*.sqlite
data.key
cpa/config.yaml
cpa/management.key
cpa/auths/**
cpa/home/**
cpa/plugins/**
```

After a legacy import, the service immediately publishes the first unified
generation.

## Optional environment tuning

```env
STATE_BUCKET=04191bw88tk/cr-data
STATE_SNAPSHOT_PREFIX=northflank-state
STATE_SNAPSHOT_INTERVAL_SECONDS=60
STATE_SNAPSHOT_KEEP=12
STATE_VERIFY_UPLOAD=true
STATE_REQUIRE_EXISTING=true
```

Keep `STATE_VERIFY_UPLOAD=true` for full upload checksum verification. Clean
deployments and restarts receive a final post-shutdown generation; an abrupt
container kill restores the newest already-verified periodic generation.
`STATE_REQUIRE_EXISTING=true` prevents an API outage or incomplete migration
from being mistaken for a new empty deployment. For a genuinely empty Bucket,
set it to `false` only for the first successful startup, then restore it to
`true`.

Restore also enforces bounded archive/member/file/manifest/path sizes, rejects
non-canonical remote and tar paths, extracts with fixed private permissions,
and requires the Manager database to contain both `settings` and
`usage_events`. Backups use an exclusive Linux `flock`.

## Routes

- `/management.html` — CPA Manager Plus
- `/v1/*`, `/v1beta/*`, `/api/*` — CLIProxyAPI
- `/health` — Manager health
- `/healthz` — CPA health
