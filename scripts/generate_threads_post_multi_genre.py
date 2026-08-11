#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天市場ランキングAPIから、指定した各ジャンルごとに「1位〜5位の商品」を取得し、
Threads投稿用の本文(リンクなし)とリプライ用テキスト(リンクあり)を
ジャンル・順位ごとに生成してファイルに出力するスクリプト。

★ このスクリプトは Threads への自動投稿は一切行いません。
   生成された内容を確認し、手動でThreadsアプリ/Webからコピペ投稿してください。

必要な環境変数:
  RAKUTEN_APP_ID        楽天ウェブサービスのApplication ID
  RAKUTEN_ACCESS_KEY    楽天ウェブサービスのアクセスキー

任意の環境変数:
  RAKUTEN_AFFILIATE_ID  楽天アフィリエイトID(未指定の場合、生成されるURLは
                         アフィリエイトリンクになりません)
  RAKUTEN_GENRE_IDS     カンマ区切りのジャンルIDリスト。
                         未指定の場合は下記 DEFAULT_GENRE_IDS を使用。
                         例: "100939,551167,100227"
  RANKING_TOP_N         各ジャンルで取得する順位の数(既定: 5)。
                         30を超える値を指定した場合は自動的に複数ページを
                         取得して連結する(例: 50位まで指定するとpage1,2を取得)。
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

RAKUTEN_RANKING_URL = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"

# 出力先ディレクトリ(GitHub Actionsではartifactとしてアップロードする想定)
OUTPUT_DIR = Path(__file__).resolve().parent / "generated_posts"

# 既定で対象とするジャンルID(必要に応じて変更・追加してください)
# 例: 100939=コスメ・香水・美容, 551167=スイーツ・お菓子, 100227=キッチン用品,
#     100804=ファッション, 100371=家電
DEFAULT_GENRE_IDS = [
    "0",       # 総合ランキング
]

RANKING_TOP_N_DEFAULT = 5


# 楽天ランキングAPIは1ページあたり最大30件しか返さない(31位以降は取得できない仕様)。
# top_n が30を超える場合は、必要なページ数だけ自動でページングして取得する。
RAKUTEN_ITEMS_PER_PAGE = 30


def fetch_ranking_items(genre_id: str, top_n: int) -> list:
    app_id = os.environ["RAKUTEN_APP_ID"]
    access_key = os.environ["RAKUTEN_ACCESS_KEY"]
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
    headers = {"Referer": "https://www.rakuten.co.jp/"}

    items = []
    total_pages_needed = -(-top_n // RAKUTEN_ITEMS_PER_PAGE)  # 切り上げ除算

    for page in range(1, total_pages_needed + 1):
        params = {
            "applicationId": app_id,
            "accessKey": access_key,
            "affiliateId": affiliate_id,
            "genreId": genre_id,
            "format": "json",
            "page": page,
        }

        resp = requests.get(RAKUTEN_RANKING_URL, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"[genre={genre_id}, page={page}] 楽天APIエラーレスポンス: {resp.status_code}", file=sys.stderr)
            print(resp.text, file=sys.stderr)
        resp.raise_for_status()
        data = resp.json()

        page_items = data.get("Items", [])
        if not page_items:
            # これ以上ページがない場合は打ち切る
            break

        for entry in page_items:
            item = entry["Item"]
            items.append(
                {
                    "genreId": genre_id,
                    "itemCode": item["itemCode"],
                    "itemName": item["itemName"],
                    "itemPrice": int(item["itemPrice"]),
                    "itemUrl": item["affiliateUrl"] or item["itemUrl"],
                    "shopName": item["shopName"],
                    "rank": item.get("rank"),
                }
            )

        if len(items) >= top_n:
            break

        # ページ間でも念のため少し間隔を空ける(API負荷軽減)
        time.sleep(0.5)

    # 順位順に並べて先頭top_n件のみ返す
    items_sorted = sorted(items, key=lambda i: i.get("rank") or float("inf"))
    return items_sorted[:top_n]


STOPWORDS = {
    "送料無料", "訳あり", "セット", "限定", "公式", "正規品", "新品",
    "楽天", "楽天市場", "ポイント", "PR", "特典", "対象", "数量限定",
    "即納", "在庫限り", "入り", "まとめ買い", "人気", "サンプルキット",
    "お買い物マラソン", "最大", "倍",
}


def clean_item_name(item_name: str) -> str:
    cleaned = re.sub(r"[【\[（(『][^】\]）)』]*[】\]）)』]", " ", item_name)
    cleaned = re.sub(r"ポイント\s*最大?\s*\d+(?:\.\d+)?倍", " ", cleaned)
    cleaned = re.sub(r"\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}\s*[~〜]\s*\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}", " ", cleaned)
    cleaned = re.sub(r"\d{1,2}/\d{1,2}", " ", cleaned)
    cleaned = re.sub(r"\d{1,2}:\d{2}", " ", cleaned)
    cleaned = re.sub(r"[!！?？\"'★☆~〜]", " ", cleaned)
    cleaned = re.sub(r"[【】\[\]（）()『』]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for word in STOPWORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else item_name


def extract_hashtags(item_name: str, max_tags: int = 3) -> list:
    cleaned = clean_item_name(item_name)
    for word in STOPWORDS:
        cleaned = cleaned.replace(word, " ")
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


ENGAGEMENT_HOOKS = [
    "みんなはこういうの買う派？それとも様子見派？",
    "似たようなの持ってる人いたら感想教えてほしい👀",
    "これ気になってるんだけど、使ったことある人いますか？",
    "買うか迷い中…背中押してくれる人いる？",
    "こういう商品、正直どう思う？率直な意見ください",
]


def build_post_text(item: dict) -> tuple:
    """
    投稿本文(リンクなし)とリプライ本文(リンクあり)のタプルを返す。

    構成イメージ(参考にした実例):
      [本文]
      商品名(短縮)
      訴求ポイント(割引・お得情報)！！
      補足の一言↓

      [リプライ]
      こちらから↓
      {URL}
      #pr
    """
    name = clean_item_name(item["itemName"])
    if len(name) > 30:
        name = name[:27] + "..."

    price = f"{int(item['itemPrice']):,}円"
    url = item["itemUrl"]
    deal_reason = extract_deal_reason(item["itemName"])

    # 補足の一言(バリエーション)
    sub_lines = [
        "いろいろな種類があるよ↓",
        "今だけのお得情報あるよ↓",
        "気になる方はチェックしてみてね↓",
        "詳しくはこちらから見てみて↓",
        "この機会にぜひ↓",
    ]
    sub_line = random.choice(sub_lines)

    main_text = f"{name}\n{deal_reason}！！\n{sub_line}"

    # リプライ本文: 「こちらから↓」+ URL + #pr のシンプルな形
    reply_text = f"こちらから↓\n{url}\n#pr"

    return main_text, reply_text


def write_output(all_results: list) -> Path:
    """
    all_results: [{"genreId": ..., "items": [{item, main_text, reply_text}, ...]}, ...]
    ジャンルごと・順位ごとに整理した1つのテキストファイルにまとめて出力する。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"posts_{timestamp}.txt"

    lines = [f"# 生成日時: {timestamp}\n"]

    for genre_result in all_results:
        genre_id = genre_result["genreId"]
        lines.append(f"\n{'=' * 60}")
        lines.append(f"# ジャンルID: {genre_id}")
        lines.append(f"{'=' * 60}\n")

        for entry in genre_result["items"]:
            item = entry["item"]
            lines.append(f"--- 第{item['rank']}位 ---")
            lines.append(f"商品名: {item['itemName']}")
            lines.append(f"価格: {item['itemPrice']}円")
            lines.append(f"ショップ: {item['shopName']}")
            lines.append(f"アフィリエイトURL: {item['itemUrl']}")
            lines.append("")
            lines.append("[本文(1通目・リンクなし)]")
            lines.append(entry["main_text"])
            lines.append("")
            lines.append("[リプライ(2通目・リンクあり)]")
            lines.append(entry["reply_text"])
            lines.append("")

    content = "\n".join(lines)
    out_path.write_text(content, encoding="utf-8")
    return out_path


def main() -> int:
    genre_ids_env = os.environ.get("RAKUTEN_GENRE_IDS", "")
    if genre_ids_env.strip():
        genre_ids = [g.strip() for g in genre_ids_env.split(",") if g.strip()]
    else:
        genre_ids = DEFAULT_GENRE_IDS

    top_n = int(os.environ.get("RANKING_TOP_N", RANKING_TOP_N_DEFAULT))

    all_results = []

    for genre_id in genre_ids:
        try:
            items = fetch_ranking_items(genre_id, top_n)
        except Exception as e:
            print(f"[genre={genre_id}] 楽天APIの取得に失敗しました: {e}", file=sys.stderr)
            continue

        # fetch_ranking_items内で既に順位順ソート・上位top_n件への絞り込み済み
        genre_items = []
        for item in items:
            main_text, reply_text = build_post_text(item)
            genre_items.append(
                {"item": item, "main_text": main_text, "reply_text": reply_text}
            )
            print(f"[genre={genre_id}] 第{item['rank']}位: {item['itemName']}")

        all_results.append({"genreId": genre_id, "items": genre_items})

        # 複数ジャンルを連続でリクエストする際は、APIへの負荷軽減のため少し間隔を空ける
        time.sleep(1)

    if not all_results or all(len(r["items"]) == 0 for r in all_results):
        print("取得できた商品がありませんでした。")
        return 1

    out_path = write_output(all_results)
    print(f"\n出力ファイル: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
