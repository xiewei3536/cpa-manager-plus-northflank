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

# CPA Manager Plus

This Space runs [seakee/CPA-Manager-Plus](https://github.com/seakee/CPA-Manager-Plus) v1.11.7.

Runtime data is stored under `/data`, which is backed by the private
`04191bw88tk/cr-data` Storage Bucket mounted as a read-write Space volume.
The administrator key and data-encryption key are configured as Hugging Face
Space secrets and are not committed to this repository.
