# 楽天ランキング → Threads 自動投稿

楽天市場の総合ランキングAPIから商品情報を取得し、Threadsへ自動投稿するGitHub Actions構成です。
1日3回(JST 9:00 / 13:00 / 19:00)実行され、1回につき1件、未投稿の商品を投稿します。

## セットアップ手順

### 1. 楽天ウェブサービスの登録
1. https://webservice.rakuten.co.jp/ でアカウント作成しApplication IDを取得
2. 楽天アフィリエイトに登録し、アフィリエイトIDを取得

### 2. Threads APIの準備
1. https://developers.facebook.com/ でアプリを作成し、「Threads API」を追加
2. Threadsアカウント(Instagramアカウントと連携済み)をテストユーザーとして追加
3. 認可フローで短期トークンを取得 → 長期トークン(60日)に交換
4. Threadsのユーザーidを確認

### 3. GitHubリポジトリへの登録
このフォルダの中身をリポジトリにpushし、Settings > Secrets and variables > Actions で以下を登録します。

| Secret名 | 内容 |
|---|---|
| `RAKUTEN_APP_ID` | 楽天Application ID |
| `RAKUTEN_AFFILIATE_ID` | 楽天アフィリエイトID |
| `THREADS_ACCESS_TOKEN` | Threads長期アクセストークン |
| `THREADS_USER_ID` | ThreadsユーザーID |

### 4. 動作確認
Actionsタブから `Rakuten to Threads Auto Post` を選び、`Run workflow` で手動実行して動作を確認してください。

## 運用上の注意
- Threadsの長期アクセストークンは**60日で失効**します。期限が切れる前に手動、または別途リフレッシュ用のworkflowを用意して更新してください。
- `data/posted.json` に投稿済みの商品コードを記録し、重複投稿を防いでいます。ワークフロー実行後に自動でコミットされます。
- 投稿件数・時間帯は `scripts/post_to_threads.py` の `POSTS_PER_RUN` や `.github/workflows/post.yml` の `cron` を変更することで調整できます。
- 楽天アフィリエイトの規約に沿った表記(#PR表記など)を必ず確認してください。
