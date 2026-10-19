#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threads のインサイト(閲覧数・返信数など)を取得し、data/post_log.jsonl と突き合わせて分析する。

出力(すべて data/ 配下。ワークフローが自動コミットする):
  threads_metrics.json  投稿ごとの Threads 側の値(メディアID・閲覧数・いいね・返信など)
  feedback.json         scripts/schedule_to_buffer.py が読む「重み」(テンプレート/カテゴリ/キーワード)
  threads_report.md     人が読む分析レポート(上位/下位の投稿、カテゴリ別、時間帯別、ショップ別)

重みの考え方:
  - 指標は「閲覧数」(外れ値に引きずられないよう log1p を取る)。返信率はレポートに併記する。
  - 件数が少ないグループは全体平均に引き寄せる(SHRINK_K)。偶然の当たりで偏らないため。
  - 重みは 0.5〜2.0 に制限する(悪い結果でも出現ゼロにしない = 探索を残す)。
  - 商品投稿 / 質問のみ投稿は別々に集計し、各 MIN_POSTS 件に達するまでは重みを出さない(従来どおり完全ランダム)。
  - 時間帯は重みにせず、レポートで提示するだけ(枠の変更は POST_SLOTS_JST を人が判断する)。

必須の環境変数:
  THREADS_ACCESS_TOKEN      threads_basic + threads_manage_insights 権限のトークン

任意の環境変数:
  LOOKBACK_DAYS             何日前までの投稿のインサイトを更新するか 既定: 45
  MIN_AGE_HOURS             投稿から何時間経った投稿を集計対象にするか 既定: 24
  MIN_POSTS                 重みを出すのに必要な件数(種類ごと)         既定: 15
  SHRINK_K                  少数グループを全体平均へ寄せる強さ         既定: 5
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

API_BASE = "https://graph.threads.net/v1.0"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_FILE = DATA_DIR / "post_log.jsonl"
METRICS_FILE = DATA_DIR / "threads_metrics.json"
FEEDBACK_FILE = DATA_DIR / "feedback.json"
REPORT_FILE = DATA_DIR / "threads_report.md"

HTTP_TIMEOUT = 30
MAX_ATTEMPTS = 3
MAX_PAGES = 10  # 1ページ50件 × 10 = 直近500件まで
INSIGHT_METRICS = "views,likes,replies,reposts,quotes"
WEIGHT_MIN, WEIGHT_MAX = 0.5, 2.0
EXACT_MATCH_WINDOW = timedelta(hours=12)
FUZZY_MATCH_WINDOW = timedelta(minutes=30)
FUZZY_RATIO = 0.8


class ThreadsError(RuntimeError):
    """Threads API 呼び出し失敗。メッセージにトークンが入らないよう、通信例外の詳細は出さない。"""


# ----------------------------- 設定 ------------------------------------------
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"[設定エラー] {name} は整数で指定してください: {raw!r}") from None
    if value < minimum:
        raise SystemExit(f"[設定エラー] {name} は {minimum} 以上で指定してください: {value}")
    return value


# ----------------------------- Threads API -----------------------------------
def api_get(token: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET して JSON を返す。requests の例外文字列は URL(=トークン)を含み得るので、型名だけ出す。"""
    query = dict(params)
    query["access_token"] = token
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            resp = requests.get(f"{API_BASE}/{path}", params=query, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            if not last:
                time.sleep(2**attempt)
                continue
            raise ThreadsError(f"通信エラー({type(exc).__name__})") from None
        if resp.status_code == 429 or resp.status_code >= 500:
            if not last:
                time.sleep(5 * 2**attempt)
                continue
            raise ThreadsError(f"HTTP {resp.status_code}: 再試行上限")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200:
            err = body.get("error") or {}
            hint = ""
            if err.get("code") == 190:
                hint = "(トークンが無効または期限切れです。再発行して THREADS_ACCESS_TOKEN を更新してください)"
            raise ThreadsError(f"HTTP {resp.status_code} code={err.get('code')} {err.get('message')}{hint}")
        return body
    raise ThreadsError("到達不能")


def parse_ts(raw: str) -> datetime:
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def fetch_recent_posts(token: str, cutoff: datetime) -> list[dict[str, Any]]:
    """自分のルート投稿を新しい順に取得し、cutoff より古くなったら打ち切る。"""
    posts: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"fields": "id,text,timestamp,permalink", "limit": 50}
        if after:
            params["after"] = after
        body = api_get(token, "me/threads", params)
        reached_old = False
        for item in body.get("data") or []:
            if not item.get("timestamp"):
                continue
            ts = parse_ts(item["timestamp"])
            if ts < cutoff:
                reached_old = True
                continue
            posts.append({"id": item["id"], "text": item.get("text") or "", "ts": ts,
                          "permalink": item.get("permalink")})
        paging = body.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if reached_old or not after or not paging.get("next"):
            break
    return posts


def parse_insights(body: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for metric in body.get("data") or []:
        name = metric.get("name")
        values = metric.get("values")
        value = values[0].get("value") if values else (metric.get("total_value") or {}).get("value")
        if name and isinstance(value, (int, float)):
            out[name] = int(value)
    return out


def fetch_insights(token: str, media_id: str) -> dict[str, int] | None:
    try:
        return parse_insights(api_get(token, f"{media_id}/insights", {"metric": INSIGHT_METRICS})) or None
    except ThreadsError as exc:
        print(f"[警告] インサイト取得失敗 media_id={media_id}: {exc}", file=sys.stderr)
        return None


# ----------------------------- 入出力 ----------------------------------------
def load_log() -> list[dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    entries: list[dict[str, Any]] = []
    for lineno, line in enumerate(LOG_FILE.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            print(f"[警告] {LOG_FILE.name} {lineno}行目を読み飛ばしました(JSON不正)", file=sys.stderr)
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def load_metrics() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # 書き込み途中で落ちてもファイルが壊れないようにする


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ----------------------------- 突き合わせ ------------------------------------
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def match_posts(log: list[dict[str, Any]], posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    予約ログ(buffer_post_id) と Threads の投稿を対応づける。 {buffer_post_id: threads投稿}
      1) 本文が一致し、予約時刻との差が12時間以内 → 時刻差が最小のものを採用
      2) 1で残った投稿は、予約時刻との差が30分以内で本文が8割以上一致する未対応ログがあれば採用
    1つのログは1つの投稿にしか対応しない。
    """
    entries: list[tuple[dict[str, Any], datetime]] = []
    for e in log:
        if e.get("buffer_post_id") and e.get("due_at") and e.get("main_text") is not None:
            try:
                entries.append((e, datetime.fromisoformat(e["due_at"])))
            except ValueError:
                continue

    used: set[str] = set()
    matched: dict[str, dict[str, Any]] = {}
    leftover: list[dict[str, Any]] = []
    ordered = sorted(posts, key=lambda p: p["ts"])

    for post in ordered:
        text = _norm(post["text"])
        best: tuple[float, dict[str, Any]] | None = None
        for e, due in entries:
            if e["buffer_post_id"] in used or _norm(e["main_text"]) != text:
                continue
            diff = abs((post["ts"] - due).total_seconds())
            if diff <= EXACT_MATCH_WINDOW.total_seconds() and (best is None or diff < best[0]):
                best = (diff, e)
        if best:
            used.add(best[1]["buffer_post_id"])
            matched[best[1]["buffer_post_id"]] = post
        else:
            leftover.append(post)

    for post in leftover:
        text = _norm(post["text"])
        best_fuzzy: tuple[float, dict[str, Any]] | None = None
        for e, due in entries:
            if e["buffer_post_id"] in used:
                continue
            diff = abs((post["ts"] - due).total_seconds())
            if diff > FUZZY_MATCH_WINDOW.total_seconds():
                continue
            ratio = difflib.SequenceMatcher(None, _norm(e["main_text"]), text).ratio()
            if ratio >= FUZZY_RATIO and (best_fuzzy is None or diff < best_fuzzy[0]):
                best_fuzzy = (diff, e)
        if best_fuzzy:
            used.add(best_fuzzy[1]["buffer_post_id"])
            matched[best_fuzzy[1]["buffer_post_id"]] = post
    return matched


# ----------------------------- 集計 ------------------------------------------
def build_rows(log: list[dict[str, Any]], metrics: dict[str, dict[str, Any]], now: datetime,
               min_age: timedelta) -> list[dict[str, Any]]:
    """ログ + 実績 = 集計対象の行。投稿から min_age 以上たち、閲覧数が取れているものだけ。"""
    rows: list[dict[str, Any]] = []
    for e in log:
        rec = metrics.get(e.get("buffer_post_id", ""))
        if not rec or not isinstance(rec.get("views"), int) or not rec.get("posted_at"):
            continue
        if now - parse_ts(rec["posted_at"]) < min_age:
            continue
        row = dict(e)
        for name in ("views", "likes", "replies", "reposts", "quotes"):
            row[name] = int(rec.get(name) or 0)
        rows.append(row)
    return rows


def _geo_views(rows: list[dict[str, Any]]) -> float:
    return math.expm1(statistics.fmean(math.log1p(r["views"]) for r in rows)) if rows else 0.0


def _reply_rate(rows: list[dict[str, Any]]) -> float:
    views = sum(r["views"] for r in rows)
    return sum(r["replies"] for r in rows) / views if views else 0.0


def weights_for(rows: list[dict[str, Any]], keyfn: Callable[[dict[str, Any]], Any], base: float,
                shrink_k: float) -> dict[str, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        key = keyfn(r)
        if key:
            groups[str(key)].append(math.log1p(r["views"]))
    out: dict[str, float] = {}
    for key, vals in groups.items():
        shrunk = (sum(vals) + shrink_k * base) / (len(vals) + shrink_k)  # 少数グループは全体平均へ寄せる
        out[key] = round(min(WEIGHT_MAX, max(WEIGHT_MIN, math.exp(shrunk - base))), 3)
    return out


def compute_feedback(rows: list[dict[str, Any]], min_posts: int, shrink_k: float, now: datetime) -> dict[str, Any]:
    fb: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "active": False,
        "min_posts": min_posts,
        "counts": {},
        "template_weights": {},
        "category_weights": {},
        "keyword_weights": {},
    }
    for kind in ("product", "engage"):
        kind_rows = [r for r in rows if r.get("kind") == kind]
        fb["counts"][kind] = len(kind_rows)
        if len(kind_rows) < min_posts:
            continue
        base = statistics.fmean(math.log1p(r["views"]) for r in kind_rows)
        fb["template_weights"].update(weights_for(kind_rows, lambda r: r.get("template_key"), base, shrink_k))
        if kind == "product":
            fb["category_weights"].update(weights_for(kind_rows, lambda r: r.get("category"), base, shrink_k))
            fb["keyword_weights"].update(weights_for(kind_rows, lambda r: r.get("keyword"), base, shrink_k))
    fb["active"] = any(fb[k] for k in ("template_weights", "category_weights", "keyword_weights"))
    return fb


# ----------------------------- レポート --------------------------------------
def _table(rows: list[dict[str, Any]], keyfn: Callable[[dict[str, Any]], Any], title: str,
           limit: int | None = None) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = keyfn(r)
        if key not in (None, ""):
            groups[str(key)].append(r)
    if not groups:
        return []
    ordered = sorted(groups.items(), key=lambda kv: _geo_views(kv[1]), reverse=True)
    lines = [f"### {title}", "", "| 項目 | 件数 | 閲覧数(幾何平均) | 返信率 |", "|---|---:|---:|---:|"]
    for key, grp in ordered[:limit]:
        lines.append(f"| {key} | {len(grp)} | {_geo_views(grp):.0f} | {_reply_rate(grp) * 100:.1f}% |")
    return lines + [""]


def _excerpt(text: str, n: int = 36) -> str:
    one = _norm(text).replace("|", "/")
    return one if len(one) <= n else one[: n - 1] + "…"


def build_report(rows: list[dict[str, Any]], fb: dict[str, Any], now: datetime, stats: dict[str, int]) -> str:
    jst = timezone(timedelta(hours=9))
    out = [
        "# Threads 分析レポート",
        "",
        f"更新: {now.astimezone(jst):%Y-%m-%d %H:%M} JST",
        "",
        f"- ログ上の予約: {stats['log']}件 / Threadsと突き合わせできた投稿(直近): {stats['matched']}件",
        f"- 集計対象(投稿から一定時間経過・閲覧数あり): {len(rows)}件"
        f"(商品 {fb['counts'].get('product', 0)} / 質問のみ {fb['counts'].get('engage', 0)})",
        f"- Threads上にあるがログに無い投稿(ログ導入前など): {stats['unlogged']}件",
        f"- フィードバック: {'**有効**(重みを schedule_to_buffer.py が使用)' if fb['active'] else '無効(各種類が ' + str(fb['min_posts']) + ' 件に達するまで完全ランダム)'}",
        "",
    ]
    if not rows:
        return "\n".join(out + ["集計対象の投稿がまだありません。投稿が溜まるまでお待ちください。", ""])

    for kind, label in (("product", "商品投稿"), ("engage", "質問のみ投稿")):
        kr = [r for r in rows if r.get("kind") == kind]
        if kr:
            views = sorted(r["views"] for r in kr)
            out.append(f"- {label}: 閲覧数の中央値 {statistics.median(views):.0f} / 幾何平均 {_geo_views(kr):.0f} / "
                       f"返信率 {_reply_rate(kr) * 100:.1f}%")
    out.append("")

    ranked = sorted(rows, key=lambda r: r["views"], reverse=True)
    out += ["## 閲覧数 上位5件", "", "| 閲覧 | 返信 | いいね | 種類 | カテゴリ | 本文 |", "|---:|---:|---:|---|---|---|"]
    for r in ranked[:5]:
        out.append(f"| {r['views']} | {r['replies']} | {r['likes']} | {r.get('kind')} | {r.get('category') or '-'} | {_excerpt(r.get('main_text', ''))} |")
    out += ["", "## 閲覧数 下位5件", "", "| 閲覧 | 返信 | いいね | 種類 | カテゴリ | 本文 |", "|---:|---:|---:|---|---|---|"]
    for r in ranked[-5:]:
        out.append(f"| {r['views']} | {r['replies']} | {r['likes']} | {r.get('kind')} | {r.get('category') or '-'} | {_excerpt(r.get('main_text', ''))} |")
    out.append("")

    product = [r for r in rows if r.get("kind") == "product"]
    out += ["## 商品投稿の内訳", ""]
    out += _table(product, lambda r: r.get("category"), "カテゴリ別")
    out += _table(product, lambda r: r.get("keyword"), "キーワード別(上位15)", limit=15)
    out += _table(product, lambda r: f"{r.get('hour_jst'):02d}時台" if isinstance(r.get("hour_jst"), int) else None, "時間帯別(参考: 重みには使いません)")
    out += _table(rows, lambda r: r.get("template_key"), "テンプレート別(商品+質問のみ)")

    shop_rows = defaultdict(list)
    for r in product:
        if r.get("shop_name"):
            shop_rows[r["shop_name"]].append(r)
    if shop_rows:
        out += ["## ショップ別(楽天アフィリエイトの「ショップ別」レポートと照らし合わせる用)", "",
                "楽天のレポートは投稿単位では見られないため、ショップ単位で「紹介した回数・閲覧数」と「クリック・売上」を見比べてください。", "",
                "| ショップ | 紹介回数 | 閲覧数合計 | 平均価格 |", "|---|---:|---:|---:|"]
        for name, grp in sorted(shop_rows.items(), key=lambda kv: sum(r["views"] for r in kv[1]), reverse=True)[:20]:
            prices = [r["price"] for r in grp if isinstance(r.get("price"), int)]
            avg_price = f"{statistics.fmean(prices):,.0f}円" if prices else "-"
            out.append(f"| {name} | {len(grp)} | {sum(r['views'] for r in grp)} | {avg_price} |")
        out.append("")

    out += ["---", "注: テンプレート・カテゴリ・時間帯は互いに影響し合うため、件数が少ないうちは傾向として見てください。", ""]
    return "\n".join(out)


# ----------------------------- メイン ----------------------------------------
def main() -> int:
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("[設定エラー] THREADS_ACCESS_TOKEN が未設定です", file=sys.stderr)
        return 1
    lookback = timedelta(days=_env_int("LOOKBACK_DAYS", 45, minimum=1))
    min_age = timedelta(hours=_env_int("MIN_AGE_HOURS", 24))
    min_posts = _env_int("MIN_POSTS", 15, minimum=1)
    shrink_k = float(_env_int("SHRINK_K", 5))
    now = datetime.now(timezone.utc)

    log = load_log()
    if not log:
        print("post_log.jsonl が無い/空のため、分析対象がありません(予約が1回以上走ってから集計されます)。")
        return 0

    try:
        posts = fetch_recent_posts(token, now - lookback)
    except ThreadsError as exc:
        print(f"[Threadsエラー] {exc}", file=sys.stderr)
        return 1
    matched = match_posts(log, posts)
    print(f"Threads投稿(直近{lookback.days}日): {len(posts)}件 / ログと対応: {len(matched)}件 / ログ総数: {len(log)}件")

    metrics = load_metrics()
    fetched = 0
    for buffer_id, post in matched.items():
        insights = fetch_insights(token, post["id"])
        if insights is None:
            continue
        rec = metrics.setdefault(buffer_id, {})
        rec.update(insights)
        rec.update(media_id=post["id"], permalink=post.get("permalink"),
                   posted_at=post["ts"].isoformat(timespec="seconds"),
                   fetched_at=now.isoformat(timespec="seconds"))
        fetched += 1
        time.sleep(0.3)  # APIへの連続アクセスを避ける
    print(f"インサイト更新: {fetched}件")
    write_json(METRICS_FILE, metrics)

    rows = build_rows(log, metrics, now, min_age)
    feedback = compute_feedback(rows, min_posts, shrink_k, now)
    write_json(FEEDBACK_FILE, feedback)
    stats = {"log": len(log), "matched": len(matched), "unlogged": len(posts) - len(matched)}
    write_text(REPORT_FILE, build_report(rows, feedback, now, stats))
    print(f"集計対象: {len(rows)}件 / フィードバック: {'有効' if feedback['active'] else '無効(データ不足)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
