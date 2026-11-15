# -*- coding: utf-8 -*-
"""購入導線型の投稿文の回帰テスト(外部通信なし・標準ライブラリのみ)。

実行: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import random
import re
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import schedule_to_buffer as sb  # noqa: E402

# 効能の断定・最上級・体験の偽装になり得る語(景表法・薬機法対策)。本文・リプライに出てはいけない。
BANNED = re.compile(r"除菌|消臭|改善|予防|治る|効く|最安|最安値|日本一|No\.?1|使ってみた|買ってみた|愛用中|在庫わずか|残りわずか")


def make_item(name: str, price: int = 3980, avg: float | None = 4.5, count: int | None = 1200,
              free: bool = True, catchcopy: str = "") -> sb.Item:
    return sb.Item(code="shop:1", shop_code="shop", name=name, price=price, url="https://example.com/aff",
                   review_avg=avg, review_count=count, postage_free=free, catchcopy=catchcopy)


class UnitPriceTest(unittest.TestCase):
    def test_single_quantity_is_computed(self) -> None:
        up = sb.compute_unit_price("トイレットペーパー シングル 96ロール", 3980, "トイレットペーパー")
        self.assertEqual((round(up[0]), up[1]), (41, "1ロール"))

    def test_weight_is_normalized(self) -> None:
        up = sb.compute_unit_price("洗濯洗剤 詰め替え 1500g", 1500, "洗濯洗剤")
        self.assertEqual((round(up[0]), up[1]), (1000, "1kg"))

    def test_ambiguous_quantity_is_skipped(self) -> None:
        for name in ("トイレットペーパー 12ロール×8パック(96ロール)", "トイレットペーパー 96ロール 2個セット",
                     "洗濯洗剤 本体400g 詰め替え1000g"):
            kw = "洗濯洗剤" if "洗剤" in name else "トイレットペーパー"
            self.assertIsNone(sb.compute_unit_price(name, 3980, kw), name)

    def test_unknown_keyword_or_out_of_range(self) -> None:
        self.assertIsNone(sb.compute_unit_price("電気毛布 130cm", 5000, "電気毛布"))
        self.assertIsNone(sb.compute_unit_price("トイレットペーパー 2ロール", 500, "トイレットペーパー"))


class DealTest(unittest.TestCase):
    def _items(self, target_price: int, peer_price: int = 5000, peers: int = 9) -> list[sb.Item]:
        base = [make_item(f"トイレットペーパー {n}ロール", price=peer_price * n // 96) for n in [96] * peers]
        base = [sb.Item(code=f"s:{i}", **{k: v for k, v in vars(it).items() if k != "code"}) for i, it in enumerate(base)]
        target = sb.Item(code="s:t", **{k: v for k, v in vars(make_item("トイレットペーパー 96ロール", price=target_price)).items()
                                        if k != "code"})
        return sb.annotate_unit_prices(base + [target], "トイレットペーパー")

    def test_cheaper_than_median_is_explained_with_basis(self) -> None:
        target = self._items(3500)[-1]
        self.assertEqual(sb.deal_percent(target), 30)
        text = sb.deal_text(target) or ""
        self.assertIn("中央値", text)
        self.assertIn("30%安い", text)

    def test_no_deal_when_not_cheaper_or_too_few_peers_or_implausible(self) -> None:
        self.assertIsNone(sb.deal_percent(self._items(4800)[-1]))          # 4%差
        self.assertIsNone(sb.deal_percent(self._items(3500, peers=3)[-1]))  # 比較対象が少ない
        self.assertIsNone(sb.deal_percent(self._items(1000)[-1]))          # 80%差=誤読を疑う


class ProofLineTest(unittest.TestCase):
    def test_low_review_count_is_hidden(self) -> None:
        text, _ = sb.build_proof_line(make_item("x", count=40), with_reviews=True, with_price=True)
        self.assertNotIn("★", text)

    def test_reviews_not_repeated_when_solution_uses_them(self) -> None:
        text, _ = sb.build_proof_line(make_item("x"), with_reviews=False, with_price=True)
        self.assertNotIn("★", text)
        self.assertIn("3,980円", text)


class ThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        random.seed(0)

    def test_many_random_posts_are_safe(self) -> None:
        items = [
            ("電気毛布", make_item("電気毛布 敷き 速暖 洗える シングル")),
            ("トイレットペーパー", make_item("トイレットペーパー シングル 96ロール 長持ち")),
            ("洗濯洗剤", make_item("洗濯洗剤 部屋干し 詰め替え 4kg", count=None, avg=None)),
            ("食器用洗剤", make_item("食器用洗剤 手にやさしい", count=30)),
        ]
        for _ in range(100):
            kw, item = random.choice(items)
            t = sb.build_thread(item, kw, "daily", [], None, day=date(2026, 11, 16), keyword=kw, style="problem")
            self.assertLessEqual(len(t.main_text), sb.THREADS_TEXT_MAX)
            self.assertFalse(t.main_text.startswith(sb.PR_LABEL))  # 既定は本文にPRなし
            self.assertEqual(t.closing_type, "cta")  # 既定は全て購入導線
            self.assertIn(sb.PR_LABEL, t.reply_text)
            self.assertIn(item.url, t.reply_text)
            self.assertNotIn(item.url, t.main_text)  # リンクはリプ欄のみ
            self.assertIsNone(BANNED.search(t.main_text), t.main_text)
            self.assertIsNone(BANNED.search(t.reply_text), t.reply_text)
            self.assertNotIn("\n\n\n", t.main_text)

    def test_pr_in_main_can_be_enabled(self) -> None:
        item = make_item("電気毛布 洗える")
        t = sb.build_thread(item, "電気毛布", "seasonal", [], None, keyword="電気毛布", pr_in_main=True)
        self.assertTrue(t.main_text.startswith(sb.PR_LABEL))

    def test_ask_ratio_extremes(self) -> None:
        item = make_item("電気毛布 洗える")
        for ratio, expected in ((0.0, "cta"), (1.0, "ask")):
            t = sb.build_thread(item, "電気毛布", "seasonal", [], None, keyword="電気毛布", ask_ratio=ratio)
            self.assertEqual(t.closing_type, expected)


class ReportImportTest(unittest.TestCase):
    def test_weights_shrink_and_clamp(self) -> None:
        from collections import Counter
        import import_rakuten_report as imp
        posts = Counter({"A": 10, "B": 10, "C": 1})
        w = imp.build_weights({"A": 1000.0, "B": 0.0, "C": 0.0}, posts)
        self.assertGreater(w["A"], 1.0)
        self.assertLess(w["B"], 1.0)
        self.assertGreaterEqual(min(w.values()), imp.WEIGHT_MIN)
        self.assertLessEqual(max(w.values()), imp.WEIGHT_MAX)
        self.assertEqual(imp.build_weights({}, posts), {})  # 報酬ゼロなら何も作らない

    def test_column_detection(self) -> None:
        import import_rakuten_report as imp
        self.assertEqual(imp.find_column(["日付", "ショップ名", "確定報酬額"], imp.REVENUE_COLUMN_HINTS), "確定報酬額")
        self.assertIsNone(imp.find_column(["日付", "クリック数"], imp.SHOP_COLUMN_HINTS))


class LinkAndPointDayTest(unittest.TestCase):
    def setUp(self) -> None:
        random.seed(1)

    def test_link_in_main_has_pr_and_fits(self) -> None:
        item = make_item("電気毛布 洗える シングル")
        t = sb.build_thread(item, "電気毛布", "seasonal", [], None, keyword="電気毛布", link_in_main_ratio=1.0)
        self.assertTrue(t.link_in_main)
        self.assertIsNone(t.reply_text)
        self.assertTrue(t.main_text.startswith(sb.PR_LABEL))  # 本文にリンクを置くなら広告表示は必須
        self.assertTrue(t.main_text.endswith(item.url))
        self.assertLessEqual(len(t.main_text), sb.THREADS_TEXT_MAX)

    def test_link_in_main_never_breaks_url(self) -> None:
        # 長いURLでも、本文に収まるなら本文に、収まらなければリプ欄に。どちらでもURLは完全な形で残る
        for n in range(20):
            random.seed(n)
            item = make_item("電気毛布 洗える 長い商品名" * 3)
            item = sb.Item(**{**vars(item), "url": "https://hb.afl.rakuten.co.jp/" + "a" * 345})
            t = sb.build_thread(item, "電気毛布", "seasonal", [], None, keyword="電気毛布", link_in_main_ratio=1.0)
            holder = t.main_text if t.link_in_main else (t.reply_text or "")
            self.assertTrue(holder.endswith(item.url))
            self.assertLessEqual(len(t.main_text), sb.THREADS_TEXT_MAX)

    def test_point_day_is_in_main_once(self) -> None:
        item = make_item("電気毛布 洗える")
        t = sb.build_thread(item, "電気毛布", "seasonal", [], None, day=date(2026, 11, 15), keyword="電気毛布")
        self.assertIn("5と0のつく日", t.main_text)
        self.assertIn("要エントリー", t.main_text)
        self.assertNotIn("5と0のつく日", t.reply_text or "")
        t2 = sb.build_thread(item, "電気毛布", "seasonal", [], None, day=date(2026, 11, 17), keyword="電気毛布")
        self.assertNotIn("5と0のつく日", t2.main_text)


class UrlSafetyTest(unittest.TestCase):
    def test_reply_never_cuts_url(self) -> None:
        url = "https://hb.afl.rakuten.co.jp/" + "b" * 340
        reply = "説明" * 150 + "\n" + url
        fitted = sb._fit_reply(reply, url, sb.THREADS_TEXT_MAX)
        self.assertLessEqual(len(fitted), sb.THREADS_TEXT_MAX)
        self.assertTrue(fitted.endswith(url))


class GenreTest(unittest.TestCase):
    def test_every_default_keyword_has_pain_points(self) -> None:
        from pain_points import PAIN_POINTS
        pairs = [p for n in sb.DEFAULT_NICHES.split(",") if n in sb.NICHE_KEYWORDS for p in sb.NICHE_KEYWORDS[n]]
        for month in sb.SEASONAL_KEYWORDS.values():
            pairs += month
        missing = sorted({q for q, _ in pairs} - set(PAIN_POINTS))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
