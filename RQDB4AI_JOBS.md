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

## 共通result仕様

Horizon jobも、他のRQDB4AI jobと同じresult形式で返す。

```json
{
  "ok": true,
  "status": "ok",
  "items": 1,
  "metrics": {
    "articles_created": 1,
    "videos_created": 1,
    "youtube_uploaded": 1
  },
  "note": "short summary",
  "artifacts": [{"type": "url", "label": "youtube", "url": "https://youtu.be/..."}],
  "error": null
}
```

- `enqueue成功` と `外部worker起動成功` は成功扱いしない。
- dashboardに表示する件数は必ず `items`。
- 詳細件数は `metrics`。
- RQDB4AI側にHorizon専用の件数推測・stdout解析・例外処理を追加しない。
