"""Тесты скачивания документов gov.kz без сети: разбор ссылок, страницы API, имена файлов.

    python -m unittest tests.test_govkz -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import govkz_docs as g  # noqa: E402


def doc(i, files=1, attach=0, title="ИНФОРМАЦИЯ по итогам аудита «Х»: 2025/2026"):
    return {"id": i, "title": title, "created_date": "2026-08-31T14:19:00.000+00:00",
            "type": {"items": [{"id": 7, "type": "Заключение"}]},
            "directions": {"items": [{"id": 12058, "title": "Аудит"}]},
            "full_text": [{"name": title, "document": f"/uploads/2026/1/1/a{i}_original.10.pdf"}
                          for _ in range(files)],
            "other_files": [{"name": "", "document": f"/uploads/2026/1/1/b{i}_{n}.rar"}
                            for n in range(attach)]}


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class ParseLink(unittest.TestCase):
    def test_section_with_filters(self):
        s = g.parse_link("https://www.gov.kz/org/esep/documents/1?directions=_12058&lang=ru")
        self.assertEqual((s.slug, s.filters, s.doc_id), ("esep", {"directions": "12058"}, None))

    def test_page_and_several_filters(self):
        s = g.parse_link("gov.kz/org/ESEP/documents/3?type=_7&directions=_1,_2&page=2")
        self.assertEqual(s.slug, "esep")
        self.assertEqual(s.filters, {"type": "7", "directions": "1,2"})

    def test_details_and_old_url_and_slug(self):
        self.assertEqual(g.parse_link(
            "https://www.gov.kz/org/esep/documents/details/1101808?lang=ru").doc_id, "1101808")
        self.assertEqual(g.parse_link("https://www.gov.kz/memleket/entities/adm/documents").slug,
                         "adm")
        self.assertEqual(g.parse_link("esep").slug, "esep")

    def test_bad_links(self):
        for bad in ("https://example.com/org/esep/documents/1", "https://www.gov.kz/org/esep/news/1",
                    "", "https://www.gov.kz/"):
            with self.assertRaises(g.BadLink, msg=bad):
                g.parse_link(bad)


class Listing(unittest.TestCase):
    def test_all_pages_are_walked(self):
        pages = {1: [doc(i) for i in range(g.PAGE_SIZE)], 2: [doc(i) for i in range(100, 103)]}
        calls = []

        def fake_get(url, params=None, lang="ru", **kw):
            calls.append(params)
            return Resp(pages.get(params["page"], []))

        with mock.patch.object(g, "_get", fake_get), mock.patch.object(g, "PAUSE_SEC", 0):
            docs = g.list_documents(g.parse_link("https://www.gov.kz/org/esep/documents/1?directions=_12058"))
        self.assertEqual(len(docs), g.PAGE_SIZE + 3)
        self.assertEqual(calls[0]["projects"], "eq:esep")
        self.assertEqual(calls[0]["directions"], "12058")

    def test_api_error_is_reported(self):
        with mock.patch.object(g, "_get", lambda *a, **k: Resp({"message": "For input string"})):
            with self.assertRaises(g.BadLink):
                g.list_documents(g.parse_link("esep"))


class Names(unittest.TestCase):
    def test_file_names_are_safe_and_unique(self):
        d = doc(5, attach=2)
        files = g.doc_files(d)
        self.assertEqual([f["kind"] for f in files], ["основной", "приложение", "приложение"])
        main = g.file_name(d, files[0], 0)
        self.assertEqual(main, "2026-08-31 ИНФОРМАЦИЯ по итогам аудита Х 2025 2026 (5).pdf")
        self.assertTrue(g.file_name(d, files[2], 2).endswith("(5) прил. 2.rar"))

    def test_long_title_is_cut_on_word(self):
        name = g.safe_name("слово " * 50)
        self.assertLessEqual(len(name), g.TITLE_CHARS)
        self.assertTrue(name.endswith("слово"))


class DownloadAll(unittest.TestCase):
    def test_shared_files_downloaded_once_and_index_written(self):
        ru = [doc(1), doc(2, attach=1)]
        kk = [doc(1, title="Аудит қорытындысы"), doc(2, attach=1, title="Екінші")]
        kk[0]["full_text"][0]["document"] = "/uploads/kk1.pdf"   # у казахской версии свой файл
        fetched = []

        def fake_download(url, dest):
            fetched.append(url)
            dest.write_bytes(b"%PDF")
            return 4

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "list_documents", lambda src, lang: ru if lang == "ru" else kk), \
                mock.patch.object(g, "download", fake_download), mock.patch.object(g, "PAUSE_SEC", 0):
            out = Path(tmp) / "out"     # архив кладётся рядом с папкой: out.zip
            st = g.download_all("esep", out, ("ru", "kk"), with_zip=True, log=lambda *a: None)
            self.assertTrue((out / "документы.xlsx").is_file())
            self.assertTrue(Path(st["zip"]).is_file())
        self.assertEqual(st["files"], 6)
        self.assertEqual(st["downloaded"], 4)        # 3 русских + 1 свой казахский
        self.assertEqual(st["duplicates"], 2)
        self.assertEqual(len(fetched), len(set(fetched)))


if __name__ == "__main__":
    unittest.main()
