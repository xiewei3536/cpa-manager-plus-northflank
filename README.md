---
title: CPA Manager Plus
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
fullWidth: true
header: mini
license: mit
---

# CPA + CPA Manager Plus

This Space runs an integrated, pinned deployment of:

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) v7.2.99
- [CPA Manager Plus](https://github.com/seakee/CPA-Manager-Plus) v1.11.7

Nginx exposes the management panel at `/management.html` and routes model API
traffic such as `/v1/*` and `/v1beta/*` directly to CPA. CPA Manager Plus uses
the internal CPA endpoint at `http://127.0.0.1:8317`.

All runtime data is stored under `/data`, backed by the private
`04191bw88tk/cr-data` Storage Bucket mounted as a read-write Space volume.
CPA configuration, provider credentials, plugins, SQLite data and encryption
keys therefore survive Space restarts. Credentials are configured as Hugging
Face Space secrets and are not committed to this repository.
