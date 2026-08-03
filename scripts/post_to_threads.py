#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天market総合ランキングAPIから商品を取得し、
未投稿のものをThreadsに自動投稿するスクリプト。

必要な環境変数:
  RAKUTEN_APP_ID        楽天ウェブサービスのApplication ID
  RAKUTEN_AFFILIATE_ID  楽天アフィリエイトID
  THREADS_ACCESS_TOKEN  Threads APIの長期アクセストークン
  THREADS_USER_ID       ThreadsのユーザーID(数値)
"""

import json
import os
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

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "affiliateId": affiliate_id,
        "genreId": 0,
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


def build_post_text(item: dict) -> str:
    name = item["itemName"]
    if len(name) > 60:
        name = name[:57] + "..."

    text = (
        f"【楽天ランキング {item['rank']}位】\n"
        f"{name}\n"
        f"価格: {int(item['itemPrice']):,}円\n"
        f"店舗: {item['shopName']}\n\n"
        f"{item['itemUrl']}\n\n"
        f"#楽天 #楽天ランキング #PR"
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
