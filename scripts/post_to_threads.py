#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天市場総合ランキングAPIから「1位の商品」を取得し、
未投稿であればThreadsに自動投稿するスクリプト。

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
import random
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
    genre_id = os.environ.get("RAKUTEN_GENRE_ID", "0") or "0"

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
    "即納", "在庫限り", "入り", "まとめ買い", "人気", "サンプルキット",
    "お買い物マラソン", "最大", "倍",
}


def clean_item_name(item_name: str) -> str:
    """表示・タグ抽出用に、商品名から販促文言(括弧内)や記号を除去する。"""
    # 括弧とその中身を除去(【楽天限定】【公式】など販促文言)
    cleaned = re.sub(r"[【\[（(『][^】\]）)』]*[】\]）)』]", " ", item_name)
    # ポイント倍率表記(例: ポイント最大19倍)を除去
    cleaned = re.sub(r"ポイント\s*最大?\s*\d+(?:\.\d+)?倍", " ", cleaned)
    # 日付・期間表記(例: 8/4 20:00~ 8/11 01:59)を除去
    cleaned = re.sub(r"\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}\s*[~〜]\s*\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}", " ", cleaned)
    cleaned = re.sub(r"\d{1,2}/\d{1,2}", " ", cleaned)
    cleaned = re.sub(r"\d{1,2}:\d{2}", " ", cleaned)
    # 感嘆符・星などの記号を除去
    cleaned = re.sub(r"[!！?？\"'★☆~〜]", " ", cleaned)
    # 入れ子の括弧などで残った孤立記号を除去
    cleaned = re.sub(r"[【】\[\]（）()『』]", " ", cleaned)
    # 余分な空白を1つにまとめる
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 販促目的のSTOPWORDS(複合語含む)を除去
    for word in STOPWORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else item_name


def extract_hashtags(item_name: str, max_tags: int = 3) -> list:
    """商品名から簡易的にハッシュタグ候補を抽出する。"""
    cleaned = clean_item_name(item_name)
    # STOPWORDSは複合語の一部としても除去する
    for word in STOPWORDS:
        cleaned = cleaned.replace(word, " ")
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
        # 日付・時刻・記号混じりの断片を除外(例: "20:00~", "01:59★", "8/4")
        if re.search(r"[:：~〜/★☆%％]", token):
            continue
        if re.fullmatch(r"[0-9]+[日時分月]?", token):
            continue
        if token in seen:
            continue
        seen.add(token)
        tags.append(f"#{token}")
        if len(tags) >= max_tags:
            break

    return tags


def extract_deal_reason(raw_item_name: str) -> str:
    """元の商品名から「お得な理由」を抽出する。見つからなければ汎用フレーズを返す。"""
    reasons = []

    m = re.search(r"ポイント\s*最大?\s*(\d+(?:\.\d+)?)倍", raw_item_name)
    if m:
        reasons.append(f"ポイント最大{m.group(1)}倍還元中")

    if re.search(r"送料無料", raw_item_name):
        reasons.append("送料無料")

    if re.search(r"タイムセール|期間限定|数量限定", raw_item_name):
        reasons.append("期間・数量限定価格")

    m2 = re.search(r"(\d+)\s*(?:%|％)\s*(?:OFF|オフ|off)", raw_item_name)
    if m2:
        reasons.append(f"{m2.group(1)}%OFF")

    if reasons:
        return "・".join(reasons)

    return "このクオリティでこの価格はかなりお得"


def build_post_text(item: dict) -> str:
    name = clean_item_name(item["itemName"])
    if len(name) > 50:
        name = name[:47] + "..."

    price = f"{int(item['itemPrice']):,}円"
    shop = item["shopName"]
    url = item["itemUrl"]
    deal_reason = extract_deal_reason(item["itemName"])

    hashtags = extract_hashtags(item["itemName"])
    hashtag_line = " ".join(hashtags + ["#PR"]) if hashtags else "#PR"

    # 「お得な理由」を軸にした投稿文のバリエーション(順位への言及はしない)
    templates = [
        (
            f"🛍️ これはお得！と思わず紹介したくなった商品\n\n"
            f"📦 {name}\n"
            f"💰 {price}(税込)\n"
            f"🏪 {shop}\n\n"
            f"✅ お得ポイント: {deal_reason}\n\n"
            f"気になった人はチェックしてみて👇\n"
            f"{url}\n\n"
            f"{hashtag_line}"
        ),
        (
            f"😳 この価格、見過ごせない…\n\n"
            f"{name}\n\n"
            f"💰 {price}\n"
            f"🏪 {shop}\n\n"
            f"🎯 {deal_reason}\n\n"
            f"詳細はこちらから確認できます👇\n"
            f"{url}\n\n"
            f"{hashtag_line}"
        ),
        (
            f"🔥 今が買い時かもしれません\n\n"
            f"📦 {name}\n"
            f"💰 {price}\n\n"
            f"お得な理由 → {deal_reason}\n\n"
            f"詳しくはこちらをチェック👇\n"
            f"{url}\n\n"
            f"{hashtag_line}"
        ),
        (
            f"👀 気になっていた人はこのタイミングをお見逃しなく\n\n"
            f"{name}\n"
            f"{price} / {shop}\n\n"
            f"✅ {deal_reason}\n\n"
            f"気になる方はリンクからどうぞ👇\n"
            f"{url}\n\n"
            f"{hashtag_line}"
        ),
    ]

    return random.choice(templates)


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

    # 1位から順に、まだ投稿していない商品を探す(1位が投稿済みなら2位、それも投稿済みなら3位…)
    ranked = {i["rank"]: i for i in items if i.get("rank") in (1, 2, 3)}
    candidates = []
    for r in (1, 2, 3):
        item = ranked.get(r)
        if item and item["itemCode"] not in posted_ids:
            candidates.append(item)
            break

    if not candidates:
        print("楽天ランキング1〜3位はすべて投稿済みです(順位変動待ち)。")
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
