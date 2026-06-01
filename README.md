# dirtracker

## English

### Overview
dirtracker is a lightweight file-change tracking tool for Linux environments.
It watches target directories, stores file snapshots in SQLite, shows history in a web UI, and exports tracked files as ZIP.

The project is composed of two services:
- backend: FastAPI app that stores and serves snapshot data
- watcher: inotify-based watcher that sends file events to backend

### Features
- Recursive file watching for one or more directories
- Snapshot history per file (created/modified/deleted)
- Unified diff generation against previous snapshots
- Web UI for browsing files, history, and snapshot content
- ZIP export for selected files/snapshots or all latest files
- Docker Compose based setup

### Requirements
- Linux host (inotify is Linux specific)
- Docker and Docker Compose

### Quick Start
1. Move to the project directory.
2. Start services:

   docker compose up --build -d

3. Open the UI:

   http://127.0.0.1:41730

4. Check logs if needed:

   docker compose logs -f backend
   docker compose logs -f watcher

5. Stop services:

   docker compose down

### Configuration
Set values in docker-compose.yml or environment variables.

Backend:
- DB_PATH: SQLite path in container (default: /data/dirtracker.db)
- DIRTRACKER_LOG_LEVEL: Logging level (default: INFO)
- UI_BIND_HOST: Host bind address for UI port mapping (default: 127.0.0.1)

Watcher:
- BACKEND_URL: Backend URL (default: http://backend:8000)
- WATCH_DIRS: Comma-separated watch roots (default: /watch/etc)
- WATCH_PREFIX_STRIP: Prefix to strip for restoring host paths (default: /watch)
- BATCH_WINDOW_SECONDS: Batch flush interval in seconds (default: 60)

### Data and Persistence
- Database file is persisted via volume mapping:
  - ./data:/data
- Main database file:
  - ./data/dirtracker.db

### API Endpoints (Summary)
- POST /api/ingest: ingest single event
- POST /api/ingest/batch: ingest batched events
- GET /api/files: list tracked files
- GET /api/files/{file_id}/snapshots: list snapshots for a file
- GET /api/snapshots/{snapshot_id}: get snapshot content
- GET /api/snapshots/{snapshot_id}/diff: get computed diff
- GET /api/export: export as ZIP
- GET /: web UI

### Security Notes
- By default, UI is exposed only to localhost.
- Watcher mounts host directories as read-only in container.
- Track only directories you explicitly allow.

### Troubleshooting
- UI not reachable:
  - Confirm containers are running with docker compose ps.
  - Check backend logs and port mapping in docker-compose.yml.

- No file updates appear:
  - Verify WATCH_DIRS matches mounted paths in watcher service.
  - Ensure watched files are UTF-8 text files (binary files are skipped).
  - Confirm backend is healthy.

- Permission errors:
  - Check host file permissions for mounted directories.

### Development Notes
- Backend: FastAPI + aiosqlite
- Watcher: inotify-simple + requests
- Python base image: 3.12-slim

### License
No license file is currently included.
Add a LICENSE file if you want to define redistribution and usage terms.

---

## 日本語

### 概要
dirtracker は Linux 環境向けの軽量なファイル変更追跡ツールです。
監視対象ディレクトリの変更を検知し、SQLite にスナップショットを保存し、Web UI で履歴確認と ZIP エクスポートを行えます。

構成は 2 サービスです。
- backend: スナップショット保存・参照を行う FastAPI
- watcher: inotify で監視し、イベントを backend に送信

### 主な機能
- 複数ディレクトリの再帰監視
- ファイルごとの履歴管理（created/modified/deleted）
- 直前スナップショットとの差分（unified diff）生成
- Web UI でファイル一覧・履歴・内容確認
- 選択対象または全最新ファイルの ZIP エクスポート
- Docker Compose で起動可能

### 動作要件
- Linux ホスト（inotify は Linux 固有）
- Docker / Docker Compose

### クイックスタート
1. プロジェクトディレクトリに移動します。
2. サービスを起動します。

   docker compose up --build -d

3. UI を開きます。

   http://127.0.0.1:41730

4. 必要に応じてログを確認します。

   docker compose logs -f backend
   docker compose logs -f watcher

5. 停止します。

   docker compose down

### 設定
設定は docker-compose.yml または環境変数で調整できます。

Backend:
- DB_PATH: コンテナ内 SQLite パス（既定: /data/dirtracker.db）
- DIRTRACKER_LOG_LEVEL: ログレベル（既定: INFO）
- UI_BIND_HOST: UI のバインド先ホスト（既定: 127.0.0.1）

Watcher:
- BACKEND_URL: Backend URL（既定: http://backend:8000）
- WATCH_DIRS: 監視ルートをカンマ区切りで指定（既定: /watch/etc）
- WATCH_PREFIX_STRIP: ホストパス復元時に除去するプレフィックス（既定: /watch）
- BATCH_WINDOW_SECONDS: バッチ送信間隔（秒、既定: 60）

### データ永続化
- ボリュームマウントで DB を保持します。
  - ./data:/data
- 主な DB ファイル:
  - ./data/dirtracker.db

### API エンドポイント（概要）
- POST /api/ingest: 単一イベント取り込み
- POST /api/ingest/batch: バッチイベント取り込み
- GET /api/files: 追跡ファイル一覧
- GET /api/files/{file_id}/snapshots: ファイルの履歴一覧
- GET /api/snapshots/{snapshot_id}: スナップショット内容取得
- GET /api/snapshots/{snapshot_id}/diff: 差分取得
- GET /api/export: ZIP エクスポート
- GET /: Web UI

### セキュリティ上の注意
- 既定では UI は localhost のみ公開です。
- watcher の監視対象は read-only マウントを想定しています。
- 監視対象ディレクトリは必要最小限にしてください。

### トラブルシューティング
- UI に接続できない:
  - docker compose ps でコンテナ稼働を確認
  - backend ログとポート設定を確認

- 変更が反映されない:
  - WATCH_DIRS と watcher の volumes 設定の対応を確認
  - UTF-8 テキスト以外のファイルは取り込まれない点を確認
  - backend のヘルス状態を確認

- 権限エラー:
  - マウント元ディレクトリの権限を確認

### 開発メモ
- Backend: FastAPI + aiosqlite
- Watcher: inotify-simple + requests
- Python ベースイメージ: 3.12-slim

### ライセンス
現時点ではライセンスファイルは同梱されていません。
配布・利用条件を明確化する場合は LICENSE ファイルを追加してください。
