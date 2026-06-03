# Horizon RQDB4AI Jobs

Horizon固有のjobコードはHorizonリポジトリ配下に置く。

RQDB4AI本体にはHorizon固有のPythonファイル、設定、説明を書かない。

## Job code

- `/home/kojima/work/horizon/horizon_jobs.py`

## 方針

- RQDB4AIはキュー管理とPython callable実行だけを担当する。
- Horizonの業務ロジックはHorizon側が持つ。
- enqueue成功をHorizon実処理成功として扱わない。
- 記事生成、動画生成、投稿、告知などの実処理結果はHorizon側のreportを正とする。
