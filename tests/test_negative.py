"""Тесты разметки негативных новостей (без сети и без базы).

  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from negative import (analyze, canonical_url, is_kz_domain, make_title,  # noqa: E402
                      parse_telegram_page, title_key)


class ExamplesFromTask(unittest.TestCase):
    """Три примера из постановки задачи должны ловиться как сильные."""

    def test_railway_cost_and_delay(self):
        v = analyze("Критика строительства железной дороги Бахты – Аягоз",
                    text="Основная критика вокруг строительства линии «Бахты – Аягоз» "
                         "сосредоточена на резком удорожании проекта и угрозе переноса сроков.")
        self.assertTrue(v.keep)
        self.assertGreaterEqual(v.score, 70)
        self.assertIn("Удорожание / бюджет", v.categories)
        self.assertIn("Срыв сроков / долгострой", v.categories)
        self.assertEqual(v.regions[0], "Абайская обл.")

    def test_atyrau_road_repair(self):
        v = analyze("Жители Атырау критикуют ремонт проспекта Азаттык",
                    text="Недовольство жителей связано с затягиванием сроков, дорожным "
                         "коллапсом и технологической несогласованностью.")
        self.assertTrue(v.keep)
        self.assertGreaterEqual(v.score, 80)
        self.assertEqual(v.regions, ["Атырауская обл."])

    def test_budget_inflation(self):
        v = analyze("Проект подорожал на 302 млрд тенге",
                    text="Резкое раздувание бюджета. Аудиторы указывают на риски завышения "
                         "стоимости и вывода средств через субподрядчиков. Казахстан.")
        self.assertTrue(v.keep)
        self.assertGreaterEqual(v.score, 70)
        self.assertEqual(v.categories, ["Удорожание / бюджет"])


class Filtering(unittest.TestCase):
    def test_no_negative_words(self):
        v = analyze("Токаев принял главу Минэнерго", kz_outlet=True)
        self.assertFalse(v.keep)

    def test_sport_excluded_by_title(self):
        v = analyze("Скандал на матче чемпионата Казахстана по футболу", kz_outlet=True)
        self.assertFalse(v.keep)

    def test_digest_excluded(self):
        v = analyze("Доброе утро! Обзор главных новостей: задержан аким, авария в Алматы",
                    kz_outlet=True)
        self.assertFalse(v.keep)

    def test_foreign_outlet_without_kz_mention(self):
        v = analyze("Прокуратура выявила нарушения в школе", kz_outlet=False)
        self.assertFalse(v.keep)
        self.assertEqual(v.reason, "не про Казахстан")

    def test_kz_outlet_foreign_story(self):
        v = analyze("В России начался топливный кризис и дефицит бензина", kz_outlet=True)
        self.assertFalse(v.keep)
        self.assertEqual(v.reason, "новость о другой стране")

    def test_foreign_story_with_kz_deep_in_text(self):
        body = ("Дефицит бензина и коллапс на заправках. " * 30
                + "Эксперты не исключают, что подорожание затронет и Казахстан.")
        v = analyze("В России начался топливный кризис", text=body, kz_outlet=True)
        self.assertFalse(v.keep)

    def test_foreign_anticorruption_body(self):
        v = analyze("Антикоррупционный комитет Армении: чиновники задержаны по делу о взятке")
        self.assertFalse(v.keep)
        v = analyze("Антикор задержал заместителя акима за взятку")
        self.assertTrue(v.keep)

    def test_foreign_story_about_kazakhstanis(self):
        v = analyze("В России задержали казахстанца по делу о мошенничестве", kz_outlet=True)
        self.assertTrue(v.keep)

    def test_kz_outlet_local_story_without_city(self):
        v = analyze("Жители жалуются на некачественный ремонт дороги", kz_outlet=True)
        self.assertTrue(v.keep)
        self.assertLess(v.score, 70)      # география не подтверждена — не «сильная»

    def test_kz_outlet_world_story_full_text(self):
        body = ("Программа Nvidia столкнулась с проблемами, отставка главы компании, "
                "инвесторы опасаются убытков и банкротства партнёров. ") * 20
        v = analyze("Финансовая схема Nvidia дала трещину", text=body, kz_outlet=True)
        self.assertFalse(v.keep)
        self.assertEqual(v.reason, "в тексте нет упоминаний Казахстана")

    def test_word_forms(self):
        for title in ("Полиция задержала подозреваемого в хищении",
                      "Чиновника арестовали за взятку",
                      "Строительство школы затянулось на два года"):
            with self.subTest(title=title):
                self.assertTrue(analyze(title, kz_outlet=True).keep)

    def test_keywords_in_original_form(self):
        v = analyze("Возбуждено уголовное дело о хищении", kz_outlet=True)
        self.assertIn("уголовное дело", v.keywords)
        self.assertIn("хищении", v.keywords)


class NormalizationCollisions(unittest.TestCase):
    """Короткие казахские основы не должны ловить русские слова после транслитерации."""

    def test_akkaunt_is_not_akau(self):
        v = analyze("Блогер удалил аккаунт", kz_outlet=True)
        self.assertFalse(v.keep)

    def test_smog_verb(self):
        v = analyze("Он смог победить", kz_outlet=True)
        self.assertFalse(v.keep)

    def test_semey_vs_family(self):
        v = analyze("Семейный бюджет вырос", text="Казахстан")
        self.assertNotIn("Абайская обл.", v.regions)

    def test_almaty_region_vs_city(self):
        v = analyze("В Алматинской области аким уволен", kz_outlet=True)
        self.assertEqual(v.regions, ["Алматинская обл."])


class Helpers(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(
            canonical_url("https://WWW.Tengrinews.kz/news/1/?utm_source=tg&id=5#comments"),
            "https://www.tengrinews.kz/news/1/?id=5")

    def test_kz_domain(self):
        self.assertTrue(is_kz_domain("azh.kz"))
        self.assertTrue(is_kz_domain("almaty.tv"))
        self.assertTrue(is_kz_domain("kz.kursiv.media"))
        self.assertFalse(is_kz_domain("lenta.ru"))
        self.assertFalse(is_kz_domain("kazan.ru"))

    def test_make_title_strips_emoji(self):
        self.assertEqual(make_title("🗣В КТЖ заявили о содействии\nдалее текст"),
                         "В КТЖ заявили о содействии")

    def test_title_key_short_titles_ignored(self):
        self.assertIsNone(title_key("Пожар в Алматы"))
        self.assertEqual(title_key("Пожар в Алматы: эвакуированы 50 человек"),
                         title_key("ПОЖАР в Алматы — эвакуированы 50 человек!"))

    def test_parse_telegram_page(self):
        html = """<html><head><link rel="prev" href="/s/chan?before=100"></head><body>
        <div class="tgme_widget_message" data-post="chan/123">
          <div class="tgme_widget_message_text js-message_reply_text">цитата</div>
          <div class="tgme_widget_message_text js-message_text">Первая строка<br>вторая
            <a href="https://tengrinews.kz/a/1">ссылка</a> <a href="https://t.me/x">tg</a></div>
          <a class="tgme_widget_message_date" href="#"><time datetime="2026-11-16T10:00:00+00:00"></time></a>
        </div>
        <div class="tgme_widget_message" data-post="chan/124"><div class="photo"></div></div>
        </body></html>"""
        posts, before = parse_telegram_page(html)
        self.assertEqual(before, "100")
        self.assertEqual(len(posts), 1)
        pid, dt, text, links = posts[0]
        self.assertEqual(pid, 123)
        self.assertEqual(dt.isoformat(), "2026-11-16T10:00:00+00:00")
        self.assertTrue(text.startswith("Первая строка\nвторая"))
        self.assertEqual(links, ["https://tengrinews.kz/a/1"])


if __name__ == "__main__":
    unittest.main()
