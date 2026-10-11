#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天市場の商品を選定し、Buffer API 経由で Threads の「予約投稿」として登録する。

- 投稿そのものは Buffer が指定時刻に実行する(本スクリプトは予約を積むだけ)。
- GitHub Actions で1日1回実行し、直近 LOOKAHEAD_HOURS 時間ぶんの「空いている投稿枠」だけを埋める。
  何度実行しても同じ枠に二重予約しない(冪等)。
- 投稿文は AI を使わず、短い固定コメント + 商品リプライ(リンク)で構成する。

必須の環境変数:
  RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY / RAKUTEN_AFFILIATE_ID
  BUFFER_API_KEY            Buffer > Settings > API で発行したキー

任意の環境変数:
  BUFFER_CHANNEL_ID         Threads チャンネルID(未指定ならThreadsチャンネルを自動検出)
  POST_SLOTS_JST            投稿時刻(JST, カンマ区切り)  既定: 12:10,18:45,21:45
  LOOKAHEAD_HOURS           何時間先まで予約を埋めるか    既定: 48
  SLOT_JITTER_MINUTES       各枠に足すランダム分数        既定: 8
  MAX_SCHEDULED             予約の上限(無料プランは10)    既定: 10
  MIN_PRICE / MAX_PRICE     価格帯フィルタ(円)           既定: 500 / 8000
  RAKUTEN_KEYWORD           手動指定キーワード(最優先)
  RAKUTEN_KEYWORDS          カンマ区切りの候補(未指定なら内蔵の季節+定番プール)
  DRY_RUN                   true なら Buffer に送らず内容だけ表示
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")

BUFFER_API_URL = "https://api.buffer.com"
RAKUTEN_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
POSTED_FILE = Path(__file__).resolve().parent.parent / "data" / "posted.json"

# Threads の本文上限は500文字。固定コメントなので余裕はあるが念のため。
THREADS_TEXT_MAX = 480
MIN_LEAD_MINUTES = 20  # 予約時刻が近すぎるとBuffer側で弾かれ得るため、これ未満の枠は使わない
MAX_KEYWORD_TRIES = 4  # 1枠あたり、候補が見つかるまで試すキーワード数
HTTP_TIMEOUT = 30
MAX_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# 商品選定: Threads(20〜40代・女性比率高め・「暇つぶし/トレンド/商品検討」目的)で
# 反応を取りやすい「安価・共感しやすい・レビューが豊富」なジャンルに寄せる。
# ---------------------------------------------------------------------------
EVERGREEN_KEYWORDS: dict[str, list[str]] = {
    "beauty": ["シートマスク", "ヘアオイル", "リップ 保湿", "ハンドクリーム", "ボディクリーム", "日焼け止め"],
    "gadget": ["モバイルバッテリー 軽量", "ワイヤレスイヤホン", "スマホスタンド", "ケーブル 収納", "USB 充電器"],
    "daily": ["時短 便利グッズ", "隙間 収納", "洗濯洗剤", "キッチン 収納", "掃除 ブラシ"],
    "food": ["プロテイン", "ドリップコーヒー", "個包装 お菓子", "ゼリー ドリンク", "インスタント スープ"],
    "home": ["入浴剤", "アイマスク 温め", "ルームフレグランス", "着る毛布", "快眠 枕"],
}

# 月(JST)ごとの季節キーワード。「今必要」な商品は閲覧・クリックされやすい。
SEASONAL_KEYWORDS: dict[int, list[str]] = {
    1: ["加湿器", "電気毛布", "空気清浄機", "鍋つゆ", "ロボット掃除機"],
    2: ["加湿器", "電気毛布", "空気清浄機", "花粉対策マスク", "ロボット掃除機"],
    3: ["花粉対策マスク", "空気清浄機", "ロボット掃除機", "洗濯洗剤", "プロテイン"],
    4: ["ロボット掃除機", "洗濯洗剤", "プロテイン", "モバイルバッテリー", "ワイヤレスイヤホン"],
    5: ["除湿機", "冷感タオル", "ロボット掃除機", "モバイルバッテリー", "ワイヤレスイヤホン"],
    6: ["除湿機", "冷感タオル", "日傘", "携帯扇風機", "洗濯洗剤"],
    7: ["携帯扇風機", "冷感タオル", "日傘", "ドリップコーヒー", "除湿機"],
    8: ["携帯扇風機", "冷感タオル", "日傘", "ドリップコーヒー", "モバイルバッテリー"],
    9: ["空気清浄機", "加湿器", "ロボット掃除機", "プロテイン", "ワイヤレスイヤホン"],
    10: ["加湿器", "着る毛布", "電気毛布", "ハンドクリーム", "洗濯洗剤"],
    11: ["加湿器", "電気毛布", "空気清浄機", "こたつ", "鍋つゆ"],
    12: ["加湿器", "電気毛布", "こたつ", "鍋つゆ", "空気清浄機"],
}

# 投稿対象から外す商品(規制商材・ふるさと納税・中古・予約など)
NG_NAME = re.compile(r"ふるさと納税|中古|医薬品|第[1-3]類|コンタクト|アダルト|大人のおもちゃ|予約販売")
NG_SHOP = re.compile(r"^(f\d{6}-|book$)")  # 自治体(ふるさと納税)・書籍ショップ

# レビュー条件(厳しい順)。上から順に満たす商品を探し、無ければ緩める。
REVIEW_TIERS: tuple[tuple[float, int], ...] = ((4.3, 100), (4.0, 30))

# ---------------------------------------------------------------------------
# 固定コメント(1行・短文)。会話/保存を誘う共感系。
# 薬機法・景表法の観点から、効能の断定や「自分が使った」等の体験の偽装は入れない。
# ---------------------------------------------------------------------------
HOOKS: dict[str, list[str]] = {
    "general": [
        "これ、地味にレビュー高いやつ👀",
        "気になってた人、多いはず",
        "迷ってる人はこれ参考にして",
        "保存しとくと後で助かるかも👇",
        "レビュー数がすごいやつ見つけた",
        "これ知ってた？",
    ],
    "seasonal": [
        "そろそろ必要になる季節、準備できてる？",
        "ギリギリで焦る前にチェック👀",
        "この時期になると探す人多いやつ",
        "今のうちに見ておくと安心かも",
        "毎年これ探してる人いない？",
    ],
    "beauty": [
        "プチプラで評判いいやつ見つけた",
        "お守りコスメに入れたいやつ👀",
        "レビュー数すごい美容アイテム",
        "気になってる人、保存しといて",
    ],
    "gadget": [
        "あると地味に便利なやつ",
        "持っておくと安心なやつ👀",
        "ガジェット好きに刺さるやつ",
        "コスパ良さそうなやつ見つけた",
    ],
    "daily": [
        "地味に助かる日用品👀",
        "買い置きしておきたいやつ",
        "生活の小さなストレス減らしたい人へ",
        "これ、保存しとく価値あるかも",
    ],
    "food": [
        "ついリピしちゃいそうなやつ",
        "小腹が空いたとき用に👇",
        "ストック派の人、これどう？",
        "ご褒美にちょうどいいやつ",
    ],
    "home": [
        "おうち時間が少し快適になるやつ",
        "寝る前にチェックしたいやつ👀",
        "夜のリラックス用に保存して",
        "癒やされたい人、これ気になる",
    ],
}

PR_LABEL = "【PR】"  # ステマ規制対応。#タグだとトピックタグ化されるため文字で明示する


class ConfigError(RuntimeError):
    """設定(環境変数)不備。"""


class RakutenError(RuntimeError):
    """楽天API呼び出し失敗。"""


class BufferError(RuntimeError):
    """Buffer API呼び出し失敗。"""


class BufferLimitReached(BufferError):
    """Bufferの予約上限(プラン制限)に到達。以降の予約は無駄なので打ち切る。"""


@dataclass(frozen=True)
class Config:
    rakuten_app_id: str
    rakuten_access_key: str
    rakuten_affiliate_id: str
    buffer_api_key: str
    buffer_channel_id: str
    slots: tuple[tuple[int, int], ...]
    lookahead_hours: int
    jitter_minutes: int
    max_scheduled: int
    min_price: int
    max_price: int
    manual_keyword: str
    custom_keywords: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True)
class Item:
    code: str
    shop_code: str
    name: str
    price: int
    url: str
    review_avg: float | None
    review_count: int | None
    postage_free: bool


# ----------------------------- 設定 ------------------------------------------
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} は整数で指定してください: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} は {minimum} 以上で指定してください: {value}")
    return value


def _parse_slots(raw: str) -> tuple[tuple[int, int], ...]:
    slots: list[tuple[int, int]] = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", token)
        if not m:
            raise ConfigError(f"POST_SLOTS_JST の形式が不正です(HH:MM): {token!r}")
        slots.append((int(m.group(1)), int(m.group(2))))
    if not slots:
        raise ConfigError("POST_SLOTS_JST が空です")
    return tuple(sorted(set(slots)))


def load_config() -> Config:
    required = ["RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY", "RAKUTEN_AFFILIATE_ID", "BUFFER_API_KEY"]
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        # アフィリエイトID欠落は「投稿できるが成果ゼロ」になるため、起動時に止める
        raise ConfigError(f"必須の環境変数が未設定です: {', '.join(missing)}")

    cfg = Config(
        rakuten_app_id=os.environ["RAKUTEN_APP_ID"].strip(),
        rakuten_access_key=os.environ["RAKUTEN_ACCESS_KEY"].strip(),
        rakuten_affiliate_id=os.environ["RAKUTEN_AFFILIATE_ID"].strip(),
        buffer_api_key=os.environ["BUFFER_API_KEY"].strip(),
        buffer_channel_id=os.environ.get("BUFFER_CHANNEL_ID", "").strip(),
        slots=_parse_slots(os.environ.get("POST_SLOTS_JST", "").strip() or "12:10,18:45,21:45"),
        lookahead_hours=_env_int("LOOKAHEAD_HOURS", 48, minimum=1),
        jitter_minutes=_env_int("SLOT_JITTER_MINUTES", 8),
        max_scheduled=_env_int("MAX_SCHEDULED", 10, minimum=1),
        min_price=_env_int("MIN_PRICE", 500),
        max_price=_env_int("MAX_PRICE", 8000, minimum=1),
        manual_keyword=os.environ.get("RAKUTEN_KEYWORD", "").strip(),
        custom_keywords=tuple(
            k.strip() for k in os.environ.get("RAKUTEN_KEYWORDS", "").split(",") if k.strip()
        ),
        dry_run=os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"},
    )
    if cfg.min_price > cfg.max_price:
        raise ConfigError("MIN_PRICE が MAX_PRICE を超えています")
    return cfg


# ----------------------------- 投稿済み管理 ----------------------------------
def load_posted_ids() -> set[str]:
    if not POSTED_FILE.exists():
        return set()
    try:
        return set(json.loads(POSTED_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        # 空集合で続行すると過去商品を重複投稿するため、壊れている場合は止める
        raise ConfigError(f"{POSTED_FILE} を読み込めません: {exc}") from exc


def save_posted_ids(posted: set[str]) -> None:
    POSTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POSTED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(posted), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(POSTED_FILE)  # 書き込み途中で落ちてもファイルが壊れないようにする


# ----------------------------- 楽天API ---------------------------------------
def fetch_items(keyword: str, cfg: Config) -> list[Item]:
    params = {
        "applicationId": cfg.rakuten_app_id,
        "accessKey": cfg.rakuten_access_key,
        "affiliateId": cfg.rakuten_affiliate_id,
        "keyword": keyword,
        "hits": 30,
        "sort": "standard",
        "format": "json",
        "page": 1,
    }
    try:
        resp = requests.get(
            RAKUTEN_SEARCH_URL,
            params=params,
            headers={"Referer": "https://www.rakuten.co.jp/"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RakutenError(f"keyword={keyword!r}: {exc}") from exc

    items: list[Item] = []
    for entry in data.get("Items", []):
        raw: dict[str, Any] = entry.get("Item", entry)
        affiliate_url = raw.get("affiliateUrl") or ""
        if not affiliate_url:
            continue  # 成果が計測されない通常URLは投稿しない
        try:
            code = raw["itemCode"]
            items.append(
                Item(
                    code=code,
                    shop_code=raw.get("shopCode") or code.split(":")[0],
                    name=raw["itemName"],
                    price=int(raw["itemPrice"]),
                    url=affiliate_url,
                    review_avg=float(raw["reviewAverage"]) if raw.get("reviewAverage") not in (None, "") else None,
                    review_count=int(raw["reviewCount"]) if raw.get("reviewCount") not in (None, "") else None,
                    postage_free=raw.get("postageFlag") == 0,
                )
            )
        except (KeyError, ValueError, TypeError):
            continue  # 想定外の形式の商品は黙ってスキップ
    return items


def _is_eligible(item: Item, cfg: Config) -> bool:
    if NG_NAME.search(item.name) or NG_SHOP.match(item.shop_code):
        return False
    return cfg.min_price <= item.price <= cfg.max_price


def _score(item: Item) -> float:
    # 評価 × レビュー数(対数)。少数レビューの高評価に引っ張られないようにする
    return (item.review_avg or 0.0) * math.log10((item.review_count or 0) + 10) + (0.3 if item.postage_free else 0.0)


def pick_best(items: list[Item], excluded: set[str], cfg: Config) -> Item | None:
    pool = [i for i in items if i.code not in excluded and _is_eligible(i, cfg)]
    if not pool:
        return None
    if all(i.review_count is None for i in pool):
        # レビュー項目がAPIから返らない場合は、楽天標準ソート順(人気順)を信頼する
        print("[警告] レビュー情報が取得できないため、レビュー条件なしで選定します", file=sys.stderr)
        return pool[0]
    for min_avg, min_count in REVIEW_TIERS:
        tier = [i for i in pool if (i.review_avg or 0) >= min_avg and (i.review_count or 0) >= min_count]
        if tier:
            return max(tier, key=_score)
    return None


def build_keyword_pool(cfg: Config, now: datetime) -> list[tuple[str, str]]:
    """(キーワード, カテゴリ) のリスト。季節ものを2重に入れて当たりやすくする。"""
    if cfg.manual_keyword:
        return [(cfg.manual_keyword, "general")]
    if cfg.custom_keywords:
        return [(k, "general") for k in cfg.custom_keywords]
    pool = [(k, "seasonal") for k in SEASONAL_KEYWORDS[now.month]] * 2
    for category, words in EVERGREEN_KEYWORDS.items():
        pool += [(w, category) for w in words]
    return pool


def select_item(pool: list[tuple[str, str]], excluded: set[str], cfg: Config) -> tuple[Item, str] | None:
    tried: set[str] = set()
    candidates = pool[:]
    random.shuffle(candidates)
    for keyword, category in candidates:
        if keyword in tried:
            continue
        if len(tried) >= MAX_KEYWORD_TRIES:
            break
        tried.add(keyword)
        try:
            items = fetch_items(keyword, cfg)
        except RakutenError as exc:
            print(f"[警告] 楽天API取得失敗: {exc}", file=sys.stderr)
            continue
        item = pick_best(items, excluded, cfg)
        if item:
            print(f"選定: keyword={keyword!r} / {item.name[:40]}")
            return item, category
        time.sleep(0.5)  # 楽天APIへの連続アクセスを避ける
    return None


# ----------------------------- 投稿文(固定) ----------------------------------
def clean_item_name(name: str) -> str:
    """商品名から販促文言・日時・記号を除き、重複語を畳む。"""
    s = re.sub(r"クーポン(?:で\s*[\d,]+\s*円|利用|使用|価格)?", " ", name)
    s = re.sub(r"[【\[（(『][^】\]）)』]*[】\]）)』]", " ", s)  # 括弧内の販促文言
    s = re.sub(r"ポイント\s*最大?\s*\d+(?:\.\d+)?倍", " ", s)
    s = re.sub(r"\d{1,2}/\d{1,2}|\d{1,2}:\d{2}", " ", s)
    s = re.sub(r"[!！?？\"'★☆~〜【】\[\]（）()『』]", " ", s)
    for word in ("送料無料", "楽天市場", "楽天", "正規品", "新品", "公式", "数量限定", "在庫限り", "まとめ買い"):
        s = s.replace(word, " ")
    seen: set[str] = set()
    tokens = [t for t in re.sub(r"\s+", " ", s).strip().split(" ") if t and not (t in seen or seen.add(t))]
    return " ".join(tokens) or name


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_thread(item: Item, category: str, used_hooks: set[str]) -> tuple[str, str]:
    """(本文, リプライ) を返す。本文はリンクなしの短い固定コメント。"""
    candidates = HOOKS.get(category, []) + HOOKS["general"]
    fresh = [h for h in candidates if h not in used_hooks] or candidates  # 同一実行内の重複を避ける
    hook = random.choice(fresh)
    used_hooks.add(hook)

    name = _truncate(clean_item_name(item.name), 26)
    detail = f"{item.price:,}円"
    if item.review_avg is not None and item.review_count is not None:
        detail = f"★{item.review_avg:.1f}({item.review_count:,}件) / {detail}"

    reply = f"{PR_LABEL}{name}\n{detail}\n{item.url}"
    return _truncate(hook, THREADS_TEXT_MAX), _truncate(reply, THREADS_TEXT_MAX)


# ----------------------------- 投稿枠 ----------------------------------------
def build_slots(now: datetime, cfg: Config) -> list[datetime]:
    """now〜now+LOOKAHEAD の範囲にある、JSTの基準時刻の枠を昇順で返す。"""
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)
    latest = now + timedelta(hours=cfg.lookahead_hours)
    slots: list[datetime] = []
    for day_offset in range(cfg.lookahead_hours // 24 + 2):
        day = (now + timedelta(days=day_offset)).date()
        for hour, minute in cfg.slots:
            slot = datetime.combine(day, dtime(hour, minute), tzinfo=JST)
            if earliest <= slot <= latest:
                slots.append(slot)
    return sorted(slots)


def slot_is_taken(slot: datetime, scheduled: list[datetime], cfg: Config) -> bool:
    """その枠(ジッター幅を含む)に、既に予約があるか。再実行時の二重予約防止。"""
    start = slot - timedelta(minutes=10)
    end = slot + timedelta(minutes=cfg.jitter_minutes + 10)
    return any(start <= due <= end for due in scheduled)


# ----------------------------- Buffer API ------------------------------------
def buffer_request(
    cfg: Config, query: str, variables: dict[str, Any] | None = None, *, is_write: bool = False
) -> dict[str, Any]:
    """
    Buffer(GraphQL)へリクエストする。
    書き込み(createPost)は冪等キーが無く、タイムアウト後の再送は二重予約になり得る。
    そのため書き込みは429(=未実行)のときだけ再試行し、他の失敗は呼び出し側に任せる
    (次回実行時の slot_is_taken が「実は登録済み」だったケースを吸収する)。
    """
    headers = {
        "Authorization": f"Bearer {cfg.buffer_api_key}",
        "Content-Type": "application/json",
        # 既定のpython-requests UAはWAFに弾かれる事例があるため明示する
        "User-Agent": "Mozilla/5.0 (compatible; rakuten-threads-scheduler/1.0)",
    }
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables

    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            resp = requests.post(BUFFER_API_URL, headers=headers, json=body, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            if not is_write and not last:
                time.sleep(2**attempt)
                continue
            raise BufferError(f"通信エラー: {exc}") from exc

        if resp.status_code == 429 or (resp.status_code >= 500 and not is_write):
            if not last:
                try:
                    wait = int(resp.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 5 * 2**attempt
                time.sleep(min(wait, 120))
                continue
            raise BufferError(f"HTTP {resp.status_code}: 再試行上限")
        if resp.status_code != 200:
            raise BufferError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise BufferError(f"JSONとして解釈できません: {resp.text[:300]}") from exc

        errors = payload.get("errors")
        if errors:
            code = (errors[0].get("extensions") or {}).get("code")
            if code == "RATE_LIMIT_EXCEEDED" and not last:
                time.sleep(5 * 2**attempt)
                continue
            raise BufferError(f"GraphQLエラー({code}): {errors[0].get('message')}")
        return payload.get("data") or {}
    raise BufferError("到達不能")  # 型チェッカー向け


def resolve_channel(cfg: Config) -> tuple[str, str]:
    """(organization_id, threads_channel_id) を返す。"""
    data = buffer_request(cfg, "query { account { organizations { id name } } }")
    orgs = (data.get("account") or {}).get("organizations") or []
    if not orgs:
        raise BufferError("Bufferの組織(Organization)が見つかりません")

    for org in orgs:
        query = f'query {{ channels(input: {{ organizationId: {json.dumps(org["id"])} }}) {{ id name service }} }}'
        channels = buffer_request(cfg, query).get("channels") or []
        threads = [c for c in channels if str(c.get("service", "")).lower() == "threads"]
        if cfg.buffer_channel_id:
            threads = [c for c in channels if c["id"] == cfg.buffer_channel_id]
        if len(threads) == 1:
            print(f"Threadsチャンネル: {threads[0].get('name')} ({threads[0]['id']})")
            return org["id"], threads[0]["id"]
        if len(threads) > 1:
            raise BufferError("Threadsチャンネルが複数あります。BUFFER_CHANNEL_ID を指定してください")
    raise BufferError("Threadsチャンネルが見つかりません。BufferでThreadsを接続済みか確認してください")


def fetch_scheduled_due_times(cfg: Config, org_id: str, channel_id: str) -> list[datetime]:
    query = (
        "query { posts(first: 100, input: { "
        f"organizationId: {json.dumps(org_id)}, "
        f"filter: {{ status: [scheduled], channelIds: [{json.dumps(channel_id)}] }} "
        "}) { edges { node { id dueAt } } pageInfo { hasNextPage } } }"
    )
    posts = buffer_request(cfg, query).get("posts") or {}
    if (posts.get("pageInfo") or {}).get("hasNextPage"):
        print("[警告] 予約が100件を超えています。枠の重複判定が不完全になる可能性があります", file=sys.stderr)

    due_times: list[datetime] = []
    for edge in posts.get("edges") or []:
        due = (edge.get("node") or {}).get("dueAt")
        if due:
            due_times.append(datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(JST))
    return due_times


CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
"""


def schedule_thread(cfg: Config, channel_id: str, due: datetime, main_text: str, reply_text: str) -> str:
    """本文+リプライのスレッドを予約する。成功したBufferのpost idを返す。"""
    variables = {
        "input": {
            "channelId": channel_id,
            "text": main_text,  # ドキュメント仕様: thread[0] と同じ内容にする
            "schedulingType": "automatic",
            "mode": "customScheduled",
            "dueAt": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "metadata": {"threads": {"thread": [{"text": main_text}, {"text": reply_text}]}},
        }
    }
    result = buffer_request(cfg, CREATE_POST_MUTATION, variables, is_write=True).get("createPost") or {}
    post = result.get("post")
    if post:
        return post["id"]
    message = result.get("message") or f"不明なレスポンス: {result}"
    if re.search(r"limit|上限|quota", message, re.IGNORECASE):
        raise BufferLimitReached(message)
    raise BufferError(f"{result.get('__typename')}: {message}")


# ----------------------------- メイン ----------------------------------------
def main() -> int:
    try:
        cfg = load_config()
        posted = load_posted_ids()
    except ConfigError as exc:
        print(f"[設定エラー] {exc}", file=sys.stderr)
        return 1

    now = datetime.now(JST)
    slots = build_slots(now, cfg)
    print(f"現在(JST): {now:%Y-%m-%d %H:%M} / 対象枠: {len(slots)}件 / DRY_RUN={cfg.dry_run}")

    try:
        org_id, channel_id = resolve_channel(cfg)
        scheduled = fetch_scheduled_due_times(cfg, org_id, channel_id)
    except BufferError as exc:
        print(f"[Bufferエラー] {exc}", file=sys.stderr)
        return 1

    open_slots = [s for s in slots if not slot_is_taken(s, scheduled, cfg)]
    capacity = max(cfg.max_scheduled - len(scheduled), 0)
    print(f"予約済み: {len(scheduled)}件 / 空き枠: {len(open_slots)}件 / 追加可能: {capacity}件")
    open_slots = open_slots[:capacity]
    if not open_slots:
        print("追加する枠はありません。")
        return 0

    pool = build_keyword_pool(cfg, now)
    used_hooks: set[str] = set()
    chosen_now: set[str] = set()
    failures = 0

    for slot in open_slots:
        picked = select_item(pool, posted | chosen_now, cfg)
        if not picked:
            print(f"[警告] {slot:%m/%d %H:%M} に使える商品が見つかりませんでした", file=sys.stderr)
            failures += 1
            continue
        item, category = picked
        main_text, reply_text = build_thread(item, category, used_hooks)
        due = slot + timedelta(minutes=random.randint(0, cfg.jitter_minutes))  # 機械的な定時投稿を避ける

        print(f"--- {due:%Y-%m-%d %H:%M} JST ---\n{main_text}\n(リプライ)\n{reply_text}\n")
        if cfg.dry_run:
            chosen_now.add(item.code)
            continue

        try:
            post_id = schedule_thread(cfg, channel_id, due, main_text, reply_text)
        except BufferLimitReached as exc:
            print(f"[Buffer上限] 予約上限に達したため終了します: {exc}", file=sys.stderr)
            break
        except BufferError as exc:
            print(f"[予約失敗] {due:%m/%d %H:%M}: {exc}", file=sys.stderr)
            failures += 1
            continue

        print(f"予約成功: bufferPostId={post_id}")
        posted.add(item.code)
        chosen_now.add(item.code)
        save_posted_ids(posted)  # 途中で落ちても成功分は失わない
        time.sleep(1)  # Buffer APIのレート制限への配慮

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
