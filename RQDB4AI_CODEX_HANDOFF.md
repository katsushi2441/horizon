# RQDB4AI Codex Handoff: Horizon Worker

## 結論

- ジョブ投入はこのサーバの Hermes が行う。
- ジョブ実行は RQDB4AI サーバの RQ worker が行う。
- Horizon の実処理はこのサーバの AIxEC API が `horizon_worker.py` を起動して行う。
- RQDB4AI job は「起動POSTしただけ」で完了扱いにしない。`horizon-worker-enqueue` の最終 report まで待つ。

## サーバ責務

このサーバ `/home/kojima/exdirect`:

- Hermes の enqueue スクリプトを持つ。
- AIxEC API `https://aixec.exbridge.jp/api.php?path=horizon/run-worker` を提供する。
- `/home/kojima/exdirect/horizon/horizon_worker.py` を実行する。
- 本番Web/API/dashboardを管理する。
- RQDB4AI jobを直接実行しない。

RQDB4AIサーバ:

- `/home/kojima/work/horizon/horizon_jobs.py` を実行する。
- RQ workerで `horizon_jobs.worker_auto_cycle_job` を処理する。
- `kurage.exbridge.jp/rqdb4ai.php` に正しい状態を表示する。
- このサーバのDBやWebファイルを直接触らない。

## RQDB4AI側で直すこと

対象:

```text
/home/kojima/work/horizon/horizon_jobs.py
```

必要な挙動:

1. `https://aixec.exbridge.jp/api.php?path=horizon/run-worker` に `api_token` 付きJSONでPOSTする。
2. 起動成功後、同APIへ `dry_run=true` でpollし、`running=false` になるまで待つ。
3. その後 `https://aixec.exbridge.jp/api.php?path=worker/status` を読み、`horizon-worker-enqueue` の最終状態を確認する。
4. `status=ok` かつ `items=1` なら RQDB4AI jobも成功にする。
5. `status=down/error` なら RQDB4AI jobも失敗にする。
6. timeoutしたら失敗にする。

禁止:

- RQDB4AI側で `horizon_worker.py` を直接実行しない。
- 起動POSTだけで `finished` にしない。
- `enqueue成功` を `Horizon成功` と扱わない。

## このサーバ側で完了済み

- `/tmp/horizon_worker_api.pid` の stale lock を削除。
- AIxEC API の `_pid_alive()` が zombie PID を alive 扱いしないよう修正。
- `/horizon/run-worker` 起動直後のdashboard statusを即 `ok` にせず `running` に変更。
- `horizon_worker.py` の dashboard report 名を `horizon-worker-enqueue` に統一。
- Hermesの `rqdb4ai_status_sync.sh` でRQ failedを古い成功表示で隠さないよう修正。

## 確認コマンド

このサーバ:

```bash
curl -sS -X POST 'https://aixec.exbridge.jp/api.php?path=horizon/run-worker' \
  -H 'Content-Type: application/json' \
  -d '{"api_token":"<AIXEC_API_TOKEN>","dry_run":true}' | jq .

curl -sS 'https://aixec.exbridge.jp/api.php?path=worker/status' \
  | jq '.workers["horizon-worker-enqueue"]'
```

RQDB4AIサーバ:

```bash
cd /home/kojima/work/horizon
git pull origin main
python3 -m py_compile horizon_jobs.py
```

期待:

- `rqdb4ai.php` では実行中は実行中、完了後は成功/失敗が矛盾なく表示される。
- 実行キュー0なのに「起動済み・未完了」のような矛盾状態を残さない。
