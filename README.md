---
title: hg live log viewer
emoji: 📟
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
fullWidth: true
header: mini
---

# hg Docker Space

這個版本已調整成：

- 可放進 GitHub 私有倉庫
- push 後自動建構 Docker 映像
- 可推送到 GitHub Container Registry（GHCR）
- 之後可用 Hugging Face Docker Space 部署
- 打開公開網址後直接顯示背景 worker 的即時終端日誌
- `config.space.json` 預設不含真實敏感值，適合公開 Docker / 公開 repo

## 專案結構

- `app.py`：網頁入口，會啟動背景 worker，並把終端日誌顯示在首頁
- `auto_pool_maintainer.py`：預設長跑任務
- `config.space.json`：適合放進公開倉庫的安全模板設定
- `Dockerfile`：給 Hugging Face Docker Space / GitHub Actions 建構映像
- `.github/workflows/docker-publish.yml`：push 到 `main` 後自動建構並推送 GHCR

## 預設背景命令

容器啟動後，預設會跑：

```bash
python -u auto_pool_maintainer.py --loop --config runtime/config.generated.json --log-dir logs
```

如果你想改成別的命令，可以在環境變數設定：

```bash
SPACE_RUN_COMMAND={python} -u register.py 5 --config {config} --log-dir {log_dir}
```

## 設定檔載入順序

`app.py` 會依序尋找：

1. `SPACE_BASE_CONFIG_PATH` 指定的檔案
2. 在 Hugging Face Space / Docker 環境優先使用 `config.space.json`
3. 其他情況 fallback 到 `config.json`

之後再把所有 `APP_CFG__...` 環境變數套入，生成：

```text
runtime/config.generated.json
```

## 公開 Docker 的安全做法

現在 repo 裡的 `config.space.json` 是**安全模板**，不含真實：

- CPA `base_url`
- CPA token / 密碼類資訊
- mail API key
- 其他敏感 endpoint / key

真正要跑時，請在 Hugging Face Secrets、Docker runtime env、或其他部署平台的 secret manager 裡填入。

## 環境變數覆寫規則

前綴使用 `APP_CFG__`，雙底線 `__` 代表 JSON 巢狀層級。

例如：

- `APP_CFG__clean__base_url=https://example.com`
- `APP_CFG__clean__token=your-token`
- `APP_CFG__mail__provider=mailfree`
- `APP_CFG__mailfree__api_base=https://example.com`
- `APP_CFG__mailfree__api_key=your-api-key`
- `APP_CFG__mailfree__domains=["a.com","b.com"]`
- `APP_CFG__maintainer__min_candidates=5`
- `APP_CFG__run__proxy=http://127.0.0.1:7890`

### 建議至少要填的敏感環境變數

- `APP_CFG__clean__base_url`
- `APP_CFG__clean__token`
- `APP_CFG__gmailtmp__api_base`
- `APP_CFG__gmailtmp__api_key`
- `APP_CFG__mailfree__api_base`
- `APP_CFG__mailfree__api_key`
- `APP_CFG__mailfree__domains`

規則：

- 純字串可以直接填
- 數字 / 布林 / 陣列建議用 JSON 字面值
- 陣列請直接寫 JSON，例如 `["a.com","b.com"]`

## 其他可用環境變數

- `SPACE_RUN_COMMAND`：覆蓋預設背景命令
- `SPACE_PAGE_TITLE`：首頁標題
- `SPACE_REFRESH_SECONDS`：頁面刷新秒數，預設 `3`
- `SPACE_RESTART_DELAY_SECONDS`：worker 異常退出後重啟等待秒數，預設 `8`
- `SPACE_LOG_TAIL_LINES`：首頁顯示的日誌行數，預設 `400`
- `SPACE_BASE_CONFIG_PATH`：手動指定基礎設定檔

### `SPACE_RUN_COMMAND` 可用佔位符

- `{python}`：目前 Python 執行檔
- `{config}`：生成後的 runtime config 路徑
- `{log_dir}`：`logs/` 路徑
- `{root}`：專案根目錄

## GitHub Actions 自動建構 Docker

本專案已包含：

```text
.github/workflows/docker-publish.yml
```

當你 push 到 `main` 後，GitHub Actions 會：

1. 自動建構 Docker image
2. 推送到 `ghcr.io/<你的 GitHub 帳號>/codex-reg-mailfree-hg`
3. 產生 `latest`、branch、sha 等 tag

## 本機測試

### 直接跑

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

### 用 Docker 跑

```bash
docker build -t codex-reg-mailfree-hg .
docker run --rm -p 7860:7860 codex-reg-mailfree-hg
```

打開：

```text
http://127.0.0.1:7860
```

## Hugging Face Docker Space 部署重點

- Hugging Face Docker Space 會依照 repo 裡的 `Dockerfile` 建構容器
- 服務預設走 `7860`，README front matter 已加上 `app_port: 7860`
- 如果你之後要公開 Docker image，可以在 GHCR package 設定改成 public
- 但就算映像公開，Hugging Face Docker Space 通常仍是根據 Space repo 中的 `Dockerfile` 建構

## 注意事項

- `config.json` 已被 `.gitignore` 排除，不會直接進 repo
- `config.space.json` 現在只保留安全模板，不再放真實 secrets
- `logs/`、`runtime/`、`output_fixed/`、`output_tokens/` 不會進 git / docker context
- 免費 Hugging Face Space 仍可能 sleep，不能保證真正永久不休眠
