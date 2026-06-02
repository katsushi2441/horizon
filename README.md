# Horizon Worker

AI-generated news video pipeline for VWork articles, Horizon.AI reports, YouTube publishing, and AIxSNS announcements.

This repository contains the local Horizon-based workflow used by ExBridge:

- collect and summarize AI/Web3/startup news with Horizon
- convert summaries into VWork/Zenn articles
- generate Horizon/Kurage news videos
- upload generated videos to YouTube
- announce articles and videos on AIxSNS
- report execution status to the AIxEC dashboard

## Main Scripts

- `horizon_worker.py` - daily end-to-end worker
- `post_to_zenn.py` - Horizon summary to VWork/Zenn article
- `generate_news_videos.py` - Horizon summary to Kurage video job
- `Horizon/` - modified Horizon runtime used by the worker

## Local Notes

Secrets such as OAuth tokens, API keys, and runtime data must stay outside Git.
