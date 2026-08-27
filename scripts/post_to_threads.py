#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天市場総合ランキングAPI、またはキーワード検索APIから商品を取得し、
未投稿であればThreadsに自動投稿するスクリプト。

【キーワード検索対応版】
  - RAKUTEN_KEYWORD(単一)または RAKUTEN_KEYWORDS(カンマ区切り、自動実行時に
    ランダムで1つ選択)を指定すると、ジャンル総合ランキングの代わりに
    そのキーワードの商品検索結果(人気順)から上位→未投稿のものを投稿する
  - どちらも未指定の場合は、従来どおりジャンル総合ランキング(RAKUTEN_GENRE_ID)
    から投稿する

【AI生成対応版】
  - GEMINI_API_KEY を設定すると、投稿文(本文)をGoogle Gemini API(無料枠)で
    毎回自動生成する。未設定、またはAPI呼び出しに失敗した場合は、
    従来の固定テンプレートによる生成に自動フォールバックする。
  - ChatGPT(OpenAI API)は継続利用できる無料枠が無い(新規登録時の試用クレジット
    のみ)ため、本スクリプトでは無料枠のあるGemini APIのみ対応している。

必要な環境変数:
  RAKUTEN_APP_ID        楽天ウェブサービスのApplication ID
  RAKUTEN_ACCESS_KEY    楽天ウェブサービスのアクセスキー
  RAKUTEN_AFFILIATE_ID  楽天アフィリエイトID
  THREADS_ACCESS_TOKEN  Threads APIの長期アクセストークン
  THREADS_USER_ID       ThreadsのユーザーID(数値)

任意の環境変数:
  RAKUTEN_KEYWORD        商品検索するキーワードを直接指定する(手動実行向け)。
                         指定されていればRAKUTEN_KEYWORDSより優先される。
  RAKUTEN_KEYWORDS       カンマ区切りのキーワード候補(例: "扇風機,加湿器,掃除機")。
                         自動実行(cron)時はこの中からランダムに1つ選ばれる。
  RAKUTEN_GENRE_ID       ランキングを取得するジャンルID(未指定 or 0で総合ランキング)。
                         RAKUTEN_KEYWORD/RAKUTEN_KEYWORDSが未指定の場合のみ使われる。
                         ジャンルIDは楽天ジャンル検索APIや以下を参照:
                         https://webservice.rakuten.co.jp/documentation/genre-search
  GEMINI_API_KEY         Google AI StudioのGemini APIキー(無料枠あり)。
                         設定すると投稿文をAI生成する。
                         取得方法: https://aistudio.google.com/apikey
  GEMINI_MODEL           使用するGeminiモデル名(既定: gemini-3.5-flash-lite)。
                         Googleの都合でモデル名は今後も変わり得るので、
                         「404 model not found」エラーが出た場合は
                         https://ai.google.dev/gemini-api/docs/models で
                         現在利用可能なモデル名を確認して変更すること。
  JITTER_MAX_SECONDS     投稿実行前に待機する最大秒数(既定: 900 = 15分)。
                         0にするとジッターなし(即実行)。
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
# 商品検索API(キーワード検索用)。ランキングAPIと同じ新基盤(openapi.rakuten.co.jp)。
# 旧バージョン(20220601)は2026年8月18日付で廃止されたため、20260701に変更済み。
# 今後さらに新しいバージョンが出た場合にエラーになったら、
# https://webservice.rakuten.co.jp/documentation/ichiba-item-search
# で最新バージョンを確認し、この値を差し替えること。
RAKUTEN_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
THREADS_API_BASE = "https://graph.threads.net/v1.0"

POSTED_FILE = Path(__file__).resolve().parent.parent / "data" / "posted.json"

# 1回の実行で投稿する件数
POSTS_PER_RUN = 1
# ランキング何位まで候補として取得するか
RANKING_FETCH_COUNT = 30
# 投稿実行前に待機する最大秒数(ジッター)。GitHub Actionsのcronは分単位で固定のため、
# 実際の投稿タイミングをここでランダムにずらし、機械的な規則性を弱める。
DEFAULT_JITTER_MAX_SECONDS = 900


def load_posted_ids() -> set:
    if POSTED_FILE.exists():
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_posted_ids(posted_ids: set) -> None:
    POSTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(posted_ids), f, ensure_ascii=False, indent=2)


def apply_startup_jitter() -> None:
    """cron起動直後の完全に規則的な投稿を避けるため、ランダムに待機する。"""
    max_seconds = int(os.environ.get("JITTER_MAX_SECONDS", DEFAULT_JITTER_MAX_SECONDS))
    if max_seconds <= 0:
        return
    wait_seconds = random.randint(0, max_seconds)
    print(f"ジッター待機: {wait_seconds}秒 (最大{max_seconds}秒)")
    time.sleep(wait_seconds)


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


def fetch_items_by_keyword(keyword: str) -> list:
    """
    指定キーワードで商品検索し、人気順(標準ソート)で並んだ商品一覧を返す。
    楽天のランキングAPIはジャンル単位でしか取得できずキーワード指定ができないため、
    キーワード指定時は商品検索APIを使い、sort=standard(標準/人気順に近い並び)で
    代用する。返り値の並び順がそのまま「そのキーワードでの上位」として扱われる。
    """
    app_id = os.environ["RAKUTEN_APP_ID"]
    access_key = os.environ["RAKUTEN_ACCESS_KEY"]
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "affiliateId": affiliate_id,
        "keyword": keyword,
        "hits": RANKING_FETCH_COUNT,
        "sort": "standard",
        "format": "json",
        "page": 1,
    }

    headers = {
        "Referer": "https://www.rakuten.co.jp/",
    }

    resp = requests.get(RAKUTEN_SEARCH_URL, params=params, headers=headers, timeout=30)
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
                # 商品検索結果には楽天側の「順位」概念が無いため、
                # 返ってきた並び順(=人気順に近いsort=standardの順)をそのまま
                # 順位として扱う(1始まり)。
                "rank": len(items) + 1,
            }
        )
    return items


def resolve_keyword() -> str:
    """
    今回の実行で使うキーワードを決定する。
      1. RAKUTEN_KEYWORD が指定されていればそれを最優先で使う(手動実行向け)
      2. 未指定なら RAKUTEN_KEYWORDS (カンマ区切り) からランダムに1つ選ぶ
         (自動実行/cron時に、複数キーワードの中から毎回自動で選択される)
      3. どちらも空ならジャンルランキングモードにフォールバックするため
         空文字列を返す
    """
    keyword = os.environ.get("RAKUTEN_KEYWORD", "").strip()
    if keyword:
        return keyword

    keywords_raw = os.environ.get("RAKUTEN_KEYWORDS", "")
    keyword_list = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    if keyword_list:
        chosen = random.choice(keyword_list)
        print(f"RAKUTEN_KEYWORDSから自動選択: {chosen} (候補: {keyword_list})")
        return chosen

    return ""


# ハッシュタグ抽出時に除外する一般的すぎる単語
STOPWORDS = {
    "送料無料", "訳あり", "セット", "限定", "公式", "正規品", "新品",
    "楽天", "楽天市場", "ポイント", "PR", "特典", "対象", "数量限定",
    "即納", "在庫限り", "入り", "まとめ買い", "人気", "サンプルキット",
    "お買い物マラソン", "最大", "倍",
}


def clean_item_name(item_name: str) -> str:
    """表示・タグ抽出用に、商品名から販促文言(括弧内)や記号を除去する。"""
    cleaned = item_name
    # 「クーポンで6,980円」のようなクーポン価格表記を除去(価格情報はitemPriceで別途扱う)
    cleaned = re.sub(r"クーポンで\s*\d{1,3}(?:,\d{3})*\s*円", " ", cleaned)
    cleaned = re.sub(r"クーポン(?:利用|使用|価格)?", " ", cleaned)
    # 括弧とその中身を除去(【楽天限定】【公式】など販促文言)
    cleaned = re.sub(r"[【\[（(『][^】\]）)』]*[】\]）)』]", " ", cleaned)
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

    # 楽天の商品名はSEO目的で同じ単語(例: 「加湿器」)が何度も
    # 繰り返し出てくることが多いため、空白区切りのトークン単位で
    # 重複を除去し、短く自然な見た目にする(出現順は維持)。
    tokens = cleaned.split(" ")
    deduped = []
    seen_tokens = set()
    for token in tokens:
        if not token:
            continue
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        deduped.append(token)
    cleaned = " ".join(deduped)

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
        # 「ランキング1位」「1位」「ランキング」のような順位関連の語は、
        # Threads上でユーザー名の横にトピックタグとして目立って表示されてしまい
        # 不自然なため、ハッシュタグ候補から除外する。
        if re.search(r"ランキング|\d+位", token):
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


# ジャンル判定用キーワード(商品名からの簡易マッチング)
# 季節性は「今が買い時」という時間的な訴求力が強いため優先度を最も高くし、
# 続けて家電・日用品・食品の順で判定する。どれにも当てはまらなければ汎用(default)。
_GENRE_KEYWORDS = {
    "seasonal": [
        "冷感", "虫除け", "虫よけ", "扇風機", "日焼け", "UV", "紫外線",
        "クーラー", "冷却", "熱中症", "加湿器", "ヒーター", "防寒", "カイロ",
        "こたつ", "扇子", "日傘",
    ],
    "appliance": [
        "家電", "掃除機", "ドライヤー", "空気清浄機", "充電器", "スピーカー",
        "イヤホン", "コードレス", "電動", "自動", "タイマー", "USB", "モバイルバッテリー",
        "調理器", "クッカー", "炊飯器", "電気",
    ],
    "daily": [
        "洗剤", "ティッシュ", "キッチン", "掃除", "クリーナー", "スポンジ",
        "ゴミ袋", "日用品", "消耗品", "トイレット", "ラップ", "洗濯", "柔軟剤",
        "マスク", "除菌", "消臭",
    ],
    "food": [
        "お菓子", "スイーツ", "コーヒー", "紅茶", "グルメ", "スナック",
        "ドリンク", "食品", "米", "肉", "魚", "野菜", "調味料",
    ],
}


def classify_genre_category(item_name: str) -> str:
    """商品名から簡易的にジャンルカテゴリを判定する。"""
    for category in ("seasonal", "appliance", "daily", "food"):
        for kw in _GENRE_KEYWORDS[category]:
            if kw in item_name:
                return category
    return "default"


# ジャンルごとの「問いかけ・会話誘発型」フォールバック素材。
# 構成: 共感フック(1行) → 二択質問 → 締め(コメント誘導)
# Gemini API未使用時でも、商品名やURLを出さず「話題提起」に徹する点はAI生成版と揃える。
_CATEGORY_HOOKS = {
    "daily": {
        "hook": [
            "毎日使う日用品、ちゃんと選んでる人と何となく選んでる人で"
            "結構差が出ますよね",
            "日用品まわり、地味に「もっと早く知りたかった」ってなること多くないですか",
        ],
        "question": [
            "みんなは「安さ重視」派？それとも「多少高くても質重視」派？",
            "ストック派？使い切ってから買う派？",
        ],
        "closing": "コメントで教えてください🙏",
    },
    "appliance": {
        "hook": [
            "家電って「安い方」を買うか「ちょっと良い方」を買うかで"
            "後々の満足度めちゃくちゃ変わりません？",
            "家電選び、スペック重視で失敗した経験ある人多そうな気がしてます",
        ],
        "question": [
            "みんなは家電選ぶとき「価格」と「機能」どっちを優先します？",
            "型落ちでも安い方派？最新機能ある方派？",
        ],
        "closing": "コメントで教えてください🙏",
    },
    "seasonal": {
        "hook": [
            "この時期になると毎年「もっと早く対策すればよかった」って"
            "なりません？",
            "季節モノの対策グッズ、ギリギリになって焦って買う人多い気がしてます",
        ],
        "question": [
            "みんなは早め準備派？必要になってから買う派？",
            "毎年同じもの買い直す派？新しいの試す派？",
        ],
        "closing": "コメントで教えてください🙏",
    },
    "food": {
        "hook": [
            "同じジャンルの食品でも、選ぶブランドで満足度が全然違うこと"
            "ありますよね",
            "お取り寄せ系、当たり外れある気がして選ぶの迷いません？",
        ],
        "question": [
            "みんなは定番だけリピートする派？新商品も気になったら試す派？",
            "自分用と贈答用、選び方変えます？",
        ],
        "closing": "コメントで教えてください🙏",
    },
    "default": {
        "hook": [
            "似たような商品がたくさんある中で選ぶの、地味に悩みません？",
            "買ってから「もっと早く知りたかった」ってなるもの、結構ある気がしてます",
        ],
        "question": [
            "みんなは口コミ重視派？直感で決める派？",
            "定番一択派？新しいの気になったら試す派？",
        ],
        "closing": "コメントで教えてください🙏",
    },
}


def build_feature_bullets(deal_reason: str) -> list:
    """extract_deal_reason()の結果(・区切り)を、最大2つの短いチェック項目に分ける。"""
    bullets = [part.strip() for part in deal_reason.split("・") if part.strip()]
    return bullets[:2]


# Threads本文の上限は500文字。AI生成テキスト+ハッシュタグがこれを超えないよう
# 安全マージンを取って切り詰める。
THREADS_TEXT_MAX_LENGTH = 480


def generate_ai_post_text(item: dict, category: str, price: str, deal_reason: str) -> str:
    """
    Gemini API(無料枠)を使って投稿文の本文(見出し〜締めの一言まで。
    ハッシュタグは含まない)を生成する。
    GEMINI_API_KEY が未設定、またはAPI呼び出しに失敗した場合は
    空文字列を返す(呼び出し側でテンプレート生成にフォールバックする)。
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return ""

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    prompt = (
        "あなたはThreads運用歴の長いプロのアフィリエイターです。\n"
        "以下の商品情報をもとに、Threadsで会話(リプライ)が生まれやすい"
        "「問いかけ・会話誘発型」の投稿本文を1本だけ作ってください。\n\n"
        "【商品情報】\n"
        f"商品名: {item['itemName']}\n"
        f"価格: {price}\n"
        f"お得ポイント: {deal_reason}\n"
        f"ジャンル: {category}\n\n"
        "【構成ルール】(必ずこの3ブロック構成にすること)\n"
        "1. 共感を誘う一言(このジャンルで多くの人が悩みがちなこと、"
        "または「〜な人多くないですか？」のような投げかけ)\n"
        "2. 自分の実体験や失敗談・気づきを1〜2行で(一人称・カジュアルな口調)\n"
        "3. 読者に選ばせる二択質問、または「コメントで教えてください」等の"
        "リプライを促す一文で締める\n\n"
        "【厳守事項】\n"
        "- 商品名・価格・URLは本文中に一切出さない(商品はあとでリプライとして紹介するため、"
        "この本文はあくまで『話題提起』に徹すること)\n"
        "- 「いいねしてください」「フォローお願いします」のような直接的な"
        "エンゲージメント依頼はしない\n"
        "- ハッシュタグは含めない(このあと別途付与するため)\n"
        "- 医学的な効能・効果を断定する表現や、誇大な表現は使わない\n"
        "- 未成年に関する表現は使わない\n"
        "- 絵文字は多用しすぎず、1〜2個程度に留める\n"
        "- 出力は投稿文の本文のみとし、前置き・説明・コードブロック記号(```)は一切付けない\n"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": 300,
        },
    }

    try:
        resp = requests.post(
            url,
            params={"key": api_key},
            json=payload,
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"Gemini APIエラーレスポンス: {resp.status_code}", file=sys.stderr)
            print(resp.text, file=sys.stderr)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # ```や```text のようなコードブロック記号が付いて返ってきた場合は除去する
        text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()
    except Exception as e:
        print(f"Gemini API呼び出しに失敗しました(テンプレート生成にフォールバックします): {e}", file=sys.stderr)
        return ""


def build_template_post_text(item: dict, name: str, price: str, deal_reason: str, category: str) -> str:
    """
    Gemini APIが使えない場合のフォールバックとして使う、
    「問いかけ・会話誘発型」の固定テンプレートによる本文生成。
    AI生成版と同じく、商品名・価格・URLはここでは出さず、
    共感フック→二択質問→コメント誘導、の3行構成にする。
    """
    hooks = _CATEGORY_HOOKS.get(category, _CATEGORY_HOOKS["default"])
    hook = random.choice(hooks["hook"])
    question = random.choice(hooks["question"])
    closing = hooks["closing"]

    return "\n\n".join([hook, question, closing])


def build_post_text(item: dict) -> tuple:
    """
    投稿本文(リンクなし)とリプライ本文(リンクあり)のタプルを返す。
    リンクは1通目の本文ではなく、リプライとして投稿する。

    本文はまずGemini API(無料枠)での生成を試み、
    GEMINI_API_KEY未設定またはAPI失敗時は、固定テンプレートに
    フォールバックする。いずれの場合も「問いかけ・会話誘発型」構成:
      共感フック(1〜2行)
      二択質問 or コメント誘導
      #ハッシュタグ #PR
    商品名・価格・URLは本文に含めず、リプライ側でまとめて紹介する。
    """
    name = clean_item_name(item["itemName"])
    # 短いフォーマットに合わせて、商品名も簡潔な長さに収める
    if len(name) > 28:
        name = name[:26] + "…"

    price = f"{int(item['itemPrice']):,}円"
    url = item["itemUrl"]
    deal_reason = extract_deal_reason(item["itemName"])
    category = classify_genre_category(item["itemName"])

    hashtags = extract_hashtags(item["itemName"])
    hashtag_line = " ".join(hashtags + ["#PR"]) if hashtags else "#PR"

    body_text = generate_ai_post_text(item, category, price, deal_reason)
    used_ai = bool(body_text)
    if not body_text:
        body_text = build_template_post_text(item, name, price, deal_reason, category)

    main_text = f"{body_text}\n\n{hashtag_line}"

    # Threadsの文字数上限(500文字)を超えないよう安全のため切り詰める
    if len(main_text) > THREADS_TEXT_MAX_LENGTH:
        overflow = len(main_text) - THREADS_TEXT_MAX_LENGTH
        body_text = body_text[: max(0, len(body_text) - overflow - 1)] + "…"
        main_text = f"{body_text}\n\n{hashtag_line}"

    print(f"本文生成: {'Gemini API' if used_ai else 'テンプレート'}")

    # リプライ本文(リンクはここに集約する)。
    # 本文の「話題提起」を受けて、「ちなみに自分が使ってるのはこれ」という
    # 自然な流れで商品名・お得ポイント・価格・リンクをまとめて紹介する。
    reply_templates = [
        f"ちなみに自分が使ってるのはこれです\n{name}\n{deal_reason}・{price}\n{url}",
        f"個人的に今気になってるのがこれ\n{name}\n{deal_reason}でこの価格({price})はかなりお得でした\n{url}",
        f"ちなみに自分はこれを選びました\n{name}\n{deal_reason}\n{price}\n{url}",
    ]
    reply_text = random.choice(reply_templates)

    # リプライも念のため文字数上限を超えないよう切り詰める
    if len(reply_text) > THREADS_TEXT_MAX_LENGTH:
        reply_text = reply_text[: THREADS_TEXT_MAX_LENGTH - 1] + "…"

    return main_text, reply_text


def create_thread_container(text: str, reply_to_id: str = None) -> str:
    """Threadsのメディアコンテナを作成し、creation_idを返す。"""
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]

    create_url = f"{THREADS_API_BASE}/{user_id}/threads"
    create_params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }
    if reply_to_id:
        # reply_to_idを指定すると、指定した投稿へのリプライとして作成される
        create_params["reply_to_id"] = reply_to_id

    create_resp = requests.post(create_url, data=create_params, timeout=30)
    if create_resp.status_code >= 400:
        print(f"Threadsコンテナ作成エラー: {create_resp.status_code}", file=sys.stderr)
        print(create_resp.text, file=sys.stderr)
    create_resp.raise_for_status()
    return create_resp.json()["id"]


def publish_thread_container(creation_id: str) -> str:
    """作成済みのコンテナを公開し、公開された投稿のidを返す。"""
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]

    # Threads APIの仕様上、公開前に少し待つことが推奨されている
    time.sleep(5)

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
    return publish_resp.json()["id"]


def post_to_threads(main_text: str, reply_text: str) -> None:
    """
    本文(リンクなし)を投稿した後、その投稿へのリプライとしてリンク付き本文を投稿する。
    """
    # 1. 本文(リンクなし)を投稿
    main_creation_id = create_thread_container(main_text)
    main_post_id = publish_thread_container(main_creation_id)
    print(f"本文投稿成功: id={main_post_id}")

    # リプライ投稿までも少し間を空ける(連続APIコールを避ける)
    time.sleep(random.uniform(3, 8))

    # 2. リンク付きリプライを投稿
    reply_creation_id = create_thread_container(reply_text, reply_to_id=main_post_id)
    reply_post_id = publish_thread_container(reply_creation_id)
    print(f"リプライ投稿成功: id={reply_post_id}")


def main() -> int:
    apply_startup_jitter()

    posted_ids = load_posted_ids()

    keyword = resolve_keyword()

    try:
        if keyword:
            print(f"モード: キーワード検索 (keyword={keyword})")
            items = fetch_items_by_keyword(keyword)
        else:
            print("モード: ジャンル総合ランキング")
            items = fetch_ranking_items()
    except Exception as e:
        print(f"楽天APIの取得に失敗しました: {e}", file=sys.stderr)
        return 1

    # 1位から順に(取得した RANKING_FETCH_COUNT 件まで)、まだ投稿していない商品を探す
    sorted_items = sorted(items, key=lambda i: i.get("rank") or float("inf"))
    candidates = []
    for item in sorted_items:
        if item["itemCode"] not in posted_ids:
            candidates.append(item)
            break

    if not candidates:
        label = f"キーワード「{keyword}」の検索結果" if keyword else f"楽天ランキング1〜{RANKING_FETCH_COUNT}位"
        print(f"{label}はすべて投稿済みです(順位変動待ち)。")
        return 0

    posted_count = 0
    for item in candidates:
        if posted_count >= POSTS_PER_RUN:
            break

        main_text, reply_text = build_post_text(item)
        try:
            post_to_threads(main_text, reply_text)
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
