"""Тесты разбора ссылок на каналы и страницы t.me/s (без сети).

    python -m unittest tests.test_telegram -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_collect as tg  # noqa: E402

PAGE = """<html><head><meta property="og:title" content="Тест &amp; канал"></head><body>
<div class="tgme_channel_info"></div>
<div class="tgme_widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="testchan/10">
  <div class="tgme_widget_message_bubble">
   <a class="tgme_widget_message_reply" href="https://t.me/testchan/5">
     <div class="tgme_widget_message_text js-message_reply_text">цитата из старого поста</div>
   </a>
   <div class="tgme_widget_message_text js-message_text" dir="auto"><b>АФМ задержала</b> директора ТОО «Ромашка»<br/><br/>Подробности по ссылке</div>
   <a class="tgme_widget_message_link_preview" href="https://example.kz/news">
     <div class="link_preview_title">Отмывание денег</div>
     <div class="link_preview_description">Возбуждено уголовное дело</div>
   </a>
   <a class="tgme_widget_message_date" href="https://t.me/testchan/10"><time datetime="2026-11-20T08:30:00+00:00" class="time">08:30</time></a>
  </div>
 </div>
</div>
<div class="tgme_widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="testchan/11">
  <a class="tgme_widget_message_date" href="https://t.me/testchan/11"><time datetime="2026-11-21T10:00:00+05:00"></time></a>
 </div>
</div>
</body></html>"""


class ParseRefTest(unittest.TestCase):
    def check(self, raw, ref, username="", invite="", cid=None):
        r = tg.parse_channel_ref(raw)
        self.assertIsNotNone(r, raw)
        self.assertEqual((r.ref, r.username, r.invite_hash, r.channel_id),
                         (ref, username, invite, cid), raw)

    def test_public(self):
        self.check("@TengriNews", "tengrinews", "TengriNews")
        self.check("tengrinews", "tengrinews", "tengrinews")
        self.check("https://t.me/tengrinews", "tengrinews", "tengrinews")
        self.check("t.me/s/tengrinews", "tengrinews", "tengrinews")
        self.check("https://t.me/tengrinews/12345?single", "tengrinews", "tengrinews")
        self.check("https://telegram.me/zakonkz/", "zakonkz", "zakonkz")

    def test_private(self):
        self.check("https://t.me/+AbCdEf_123-xyz", "+AbCdEf_123-xyz", invite="AbCdEf_123-xyz")
        self.check("t.me/joinchat/AAAAAEhjk2Lkd", "+AAAAAEhjk2Lkd", invite="AAAAAEhjk2Lkd")
        self.check("https://t.me/c/1234567890/77", "c/1234567890", cid=1234567890)
        self.check("-1001234567890", "c/1234567890", cid=1234567890)

    def test_garbage(self):
        for raw in ("", "   ", "Канал", "abc", "https://example.com/x", "t.me/s", "t.me/c/abc",
                    "some/path", "12345"):
            self.assertIsNone(tg.parse_channel_ref(raw), raw)

    def test_extract(self):
        refs, bad = tg.extract_refs("@a_chan, https://t.me/A_CHAN\nКолонка; t.me/+Zx9_abcdefgh")
        self.assertEqual([r.ref for r in refs], ["a_chan", "+Zx9_abcdefgh"])
        self.assertEqual(bad, ["Колонка"])

    def test_strict_for_tables(self):
        # в таблицах голые слова — названия, каналом считаем только @ и ссылки
        refs, bad = tg.extract_refs("Kazinform\n@kursivkz\nt.me/inbusinesskz", allow_bare=False)
        self.assertEqual([r.ref for r in refs], ["kursivkz", "inbusinesskz"])
        self.assertEqual(bad, ["Kazinform"])


class ParsePageTest(unittest.TestCase):
    def test_parse(self):
        title, posts = tg.parse_web_page(PAGE.encode("utf-8"))
        self.assertEqual(title, "Тест & канал")
        self.assertEqual([p.msg_id for p in posts], [10, 11])
        p = posts[0]
        # цитата ответа не попадает в текст, <br> превращается в перенос строки
        self.assertEqual(p.text, "АФМ задержала директора ТОО «Ромашка»\n\nПодробности по ссылке")
        self.assertEqual(p.summary, "Отмывание денег — Возбуждено уголовное дело")
        self.assertEqual(p.date, datetime(2026, 11, 20, 8, 30, tzinfo=timezone.utc))
        self.assertEqual(posts[1].text, "")
        self.assertEqual(posts[1].date, datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc))

    def test_no_preview(self):
        with self.assertRaises(tg.NoWebPreview):
            tg.parse_web_page(b"<html><body><div class='tgme_page'></div></body></html>")

    def test_title_and_url(self):
        self.assertEqual(tg.make_title("\n  Первая строка  \nвторая"), "Первая строка")
        long = "слово " * 60
        t = tg.make_title(long, 50)
        self.assertTrue(t.endswith("…") and len(t) <= 51)
        self.assertEqual(tg.post_url("chan", None, 5), "https://t.me/chan/5")
        self.assertEqual(tg.post_url(None, 123, 5), "https://t.me/c/123/5")


if __name__ == "__main__":
    unittest.main()
