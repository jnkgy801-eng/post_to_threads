#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天market総合ランキングAPIから商品を取得し、
未投稿のものをThreadsに自動投稿するスクリプト。

必要な環境変数:
  RAKUTEN_APP_ID        楽天ウェブサービスのApplication ID
  RAKUTEN_ACCESS_KEY    楽天ウェブサービスのアクセスキー
  RAKUTEN_AFFILIATE_ID  楽天アフィリエイトID
  THREADS_ACCESS_TOKEN  Threads APIの長期アクセストークン
  THREADS_USER_ID       ThreadsのユーザーID(数値)

任意の環境変数:
  RAKUTEN_GENRE_ID      ランキングを取得するジャンルID(未指定 or 0で総合ランキング)
                        ジャンルIDは楽天ジャンル検索APIや以下を参照:
                        https://webservice.rakuten.co.jp/documentation/genre-search
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# 2026年の楽天API移行対応: 新エンドポイント(openapi.rakuten.co.jp)+ accessKey必須
RAKUTEN_RANKING_URL = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"
THREADS_API_BASE = "https://graph.threads.net/v1.0"

POSTED_FILE = Path(__file__).resolve().parent.parent / "data" / "posted.json"

# 1回の実行で投稿する件数
POSTS_PER_RUN = 1
# ランキング何位まで候補として取得するか
RANKING_FETCH_COUNT = 30


def load_posted_ids() -> set:
    if POSTED_FILE.exists():
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_posted_ids(posted_ids: set) -> None:
    POSTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(posted_ids), f, ensure_ascii=False, indent=2)


def fetch_ranking_items() -> list:
    app_id = os.environ["RAKUTEN_APP_ID"]
    access_key = os.environ["RAKUTEN_ACCESS_KEY"]
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
    genre_id = os.environ.get("RAKUTEN_GENRE_ID", "0")

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "affiliateId": affiliate_id,
        "genreId": genre_id,
        "format": "json",
        "page": 1,
    }

    headers = {
        "Referer": "https://www.rakuten.co.jp/",
    }

    resp = requests.get(RAKUTEN_RANKING_URL, params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"楽天APIエラーレスポンス: {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    data = resp.json()

    items = []
    for entry in data.get("Items", [])[:RANKING_FETCH_COUNT]:
        item = entry["Item"]
        items.append(
            {
                "itemCode": item["itemCode"],
                "itemName": item["itemName"],
                "itemPrice": int(item["itemPrice"]),
                "itemUrl": item["affiliateUrl"] or item["itemUrl"],
                "shopName": item["shopName"],
                "rank": item.get("rank"),
            }
        )
    return items


# ハッシュタグ抽出時に除外する一般的すぎる単語
STOPWORDS = {
    "送料無料", "訳あり", "セット", "限定", "公式", "正規品", "新品",
    "楽天", "楽天市場", "ポイント", "PR", "特典", "対象", "数量限定",
    "即納", "在庫限り", "入り", "まとめ買い",
}


def extract_hashtags(item_name: str, max_tags: int = 3) -> list:
    """商品名から簡易的にハッシュタグ候補を抽出する。"""
    # 括弧とその中身を除去(【限定特典】などの宣伝文言を除外するため)
    cleaned = re.sub(r"[【\[（(『][^】\]）)』]*[】\]）)』]", " ", item_name)
    # 残った記号・感嘆符などを除去
    cleaned = re.sub(r"[!！?？\"']", " ", cleaned)
    # 区切り文字で分割
    tokens = re.split(r"[ 　/・,、,\-_]+", cleaned)

    tags = []
    seen = set()
    for token in tokens:
        token = token.strip()
        if len(token) < 2 or len(token) > 12:
            continue
        if token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        tags.append(f"#{token}")
        if len(tags) >= max_tags:
            break

    return tags


def build_post_text(item: dict) -> str:
    name = item["itemName"]
    if len(name) > 50:
        name = name[:47] + "..."

    rank = item["rank"]

    # 順位に応じてフックを変える
    if rank <= 3:
        hook = f"🔥 今、楽天で売れまくってる第{rank}位はコレ！"
    elif rank <= 10:
        hook = f"👀 楽天ランキングTOP10入り！第{rank}位で話題のアイテム"
    else:
        hook = f"✨ 楽天ランキング第{rank}位で密かに人気"

    hashtags = extract_hashtags(item["itemName"])
    hashtag_line = " ".join(hashtags + ["#PR"]) if hashtags else "#PR"

    text = (
        f"{hook}\n\n"
        f"📦 {name}\n"
        f"💰 {int(item['itemPrice']):,}円\n"
        f"🏪 {item['shopName']}\n\n"
        f"気になった人はチェックしてみて👇\n"
        f"{item['itemUrl']}\n\n"
        f"{hashtag_line}"
    )
    return text


def post_to_threads(text: str) -> None:
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]

    # 1. メディアコンテナ作成
    create_url = f"{THREADS_API_BASE}/{user_id}/threads"
    create_params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }
    create_resp = requests.post(create_url, data=create_params, timeout=30)
    if create_resp.status_code >= 400:
        print(f"Threadsコンテナ作成エラー: {create_resp.status_code}", file=sys.stderr)
        print(create_resp.text, file=sys.stderr)
    create_resp.raise_for_status()
    creation_id = create_resp.json()["id"]

    # Threads APIの仕様上、公開前に少し待つことが推奨されている
    time.sleep(5)

    # 2. 公開
    publish_url = f"{THREADS_API_BASE}/{user_id}/threads_publish"
    publish_params = {
        "creation_id": creation_id,
        "access_token": access_token,
    }
    publish_resp = requests.post(publish_url, data=publish_params, timeout=30)
    if publish_resp.status_code >= 400:
        print(f"Threads公開エラー: {publish_resp.status_code}", file=sys.stderr)
        print(publish_resp.text, file=sys.stderr)
    publish_resp.raise_for_status()


def main() -> int:
    posted_ids = load_posted_ids()

    try:
        items = fetch_ranking_items()
    except Exception as e:
        print(f"楽天APIの取得に失敗しました: {e}", file=sys.stderr)
        return 1

    candidates = [i for i in items if i["itemCode"] not in posted_ids]

    if not candidates:
        print("投稿可能な新しい商品がありません(全て投稿済み)。")
        return 0

    posted_count = 0
    for item in candidates:
        if posted_count >= POSTS_PER_RUN:
            break

        text = build_post_text(item)
        try:
            post_to_threads(text)
            print(f"投稿成功: {item['itemName']}")
            posted_ids.add(item["itemCode"])
            posted_count += 1
        except requests.HTTPError as e:
            print(f"投稿失敗: {item['itemName']} - {e}", file=sys.stderr)
            continue

    save_posted_ids(posted_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
