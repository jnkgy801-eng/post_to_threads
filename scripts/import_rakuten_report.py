#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天アフィリエイトの「ショップ別」成果レポート(CSV)を読み、ショップ別の売上重みを data/sales_weights.json に書く。

背景: 楽天アフィリエイトのレポートを自動取得する公式APIは確認できなかったため、
      「管理画面でCSVをダウンロード → data/rakuten_reports/ に置く」という半自動の方式にしている。
      (置くだけで GitHub Actions が重みを更新する。.github/workflows/import-rakuten-report.yml)

使い方:
  python scripts/import_rakuten_report.py [CSVのパス ...]    # 省略時は data/rakuten_reports/*.csv を全て読む

重みの考え方:
  - 紹介1回あたりの報酬 = ショップの報酬額 ÷ そのショップを紹介した回数(post_log.jsonl)
  - 紹介回数が少ないショップは全体平均に寄せる(偶然の1件で偏らないように)
  - 0.5〜2.0 に制限し、報酬が出ていないショップも出現ゼロにはしない
  - 報酬の合計が0(まだ成果が無い)の間は何も書かない
列の自動判定: ヘッダーに「ショップ」を含む列=店名 / 「確定報酬」「発生報酬」「報酬」「売上」を含む列=報酬額(この順に優先)。
              判定できないときは、見つかった列名を表示して終了する(誤った列で重みを作らない)。
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPORT_DIR = DATA_DIR / "rakuten_reports"
POST_LOG = DATA_DIR / "post_log.jsonl"
OUT_FILE = DATA_DIR / "sales_weights.json"

SHRINK_K = 3  # 紹介回数がこの回数ぶん、平均に寄せる
WEIGHT_MIN, WEIGHT_MAX = 0.5, 2.0
SHOP_COLUMN_HINTS = ("ショップ名", "ショップ", "店舗")
REVENUE_COLUMN_HINTS = ("確定報酬", "発生報酬", "報酬", "売上")


class ReportError(RuntimeError):
    """レポートの形式が想定と違うとき。"""


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    for encoding in ("utf-8-sig", "cp932"):  # 楽天系のCSVは Shift_JIS(cp932) のことがある
        try:
            with path.open(encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                return list(reader.fieldnames or []), rows
        except UnicodeDecodeError:
            continue
    raise ReportError(f"{path.name}: 文字コードを判定できません(UTF-8 / Shift_JIS 以外)")


def find_column(headers: list[str], hints: tuple[str, ...]) -> str | None:
    for hint in hints:  # ヒントの優先順に探す
        for h in headers:
            if hint in h:
                return h
    return None


def parse_yen(raw: str) -> float:
    cleaned = re.sub(r"[^\d.\-]", "", raw or "")
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def load_revenue(paths: list[Path]) -> dict[str, float]:
    revenue: dict[str, float] = defaultdict(float)
    for path in paths:
        headers, rows = read_rows(path)
        shop_col = find_column(headers, SHOP_COLUMN_HINTS)
        rev_col = find_column(headers, REVENUE_COLUMN_HINTS)
        if not shop_col or not rev_col:
            raise ReportError(f"{path.name}: ショップ列/報酬列を判定できません。見つかった列: {headers}")
        for row in rows:
            name = (row.get(shop_col) or "").strip()
            if name:
                revenue[name] += parse_yen(row.get(rev_col, ""))
    return dict(revenue)


def count_introductions() -> Counter[str]:
    counts: Counter[str] = Counter()
    try:
        lines = POST_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            shop = json.loads(line).get("shop_name")
        except json.JSONDecodeError:
            continue
        if shop:
            counts[shop] += 1
    return counts


def build_weights(revenue: dict[str, float], posts: Counter[str]) -> dict[str, float]:
    total_rev = sum(revenue.get(s, 0.0) for s in posts)
    total_posts = sum(posts.values())
    if total_rev <= 0 or total_posts == 0:
        return {}
    mean = total_rev / total_posts  # 紹介1回あたりの全体平均報酬
    weights: dict[str, float] = {}
    for shop, n in posts.items():
        smoothed = (revenue.get(shop, 0.0) + SHRINK_K * mean) / (n + SHRINK_K)
        weights[shop] = round(min(WEIGHT_MAX, max(WEIGHT_MIN, smoothed / mean)), 3)
    return weights


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(REPORT_DIR.glob("*.csv"))
    if not paths:
        print(f"CSVがありません。{REPORT_DIR} に楽天アフィリエイトのショップ別レポート(CSV)を置いてください。")
        return 0
    try:
        revenue = load_revenue(paths)
    except (ReportError, OSError, csv.Error) as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1
    weights = build_weights(revenue, count_introductions())
    if not weights:
        print("報酬の合計が0、または紹介ログと突き合わせられるショップがありません。重みは更新しません。")
        return 0
    OUT_FILE.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_files": [p.name for p in paths],
        "shop_weights": weights,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    top = sorted(weights.items(), key=lambda kv: -kv[1])[:5]
    print(f"{len(weights)}店の重みを書き込みました: {OUT_FILE.name} / 上位: {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
