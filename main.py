#!/usr/bin/env python3
"""
Adverse media скрининг: собираем новости о нарушениях и ищем в них имена из списка,
плюс мониторинг негативных новостей о Казахстане.

Скрининг по списку:
  python main.py load-names список.xlsx
  python main.py collect --gdelt --gnews --rss --telegram
  python main.py fetch-text
  python main.py match
  python main.py export matches.xlsx --min-score 60
  python main.py dump aml_dump.json

Негативные новости о Казахстане:
  python main.py neg-collect --days 7            # все источники
  python main.py neg-fetch-text
  python main.py neg-analyze
  python main.py neg-export negative.xlsx --days 30 --status relevant
  python main.py neg-dump negative.json

Документы госорганов с gov.kz (все страницы раздела, все файлы):
  python main.py govkz-docs "https://www.gov.kz/org/esep/documents/1?directions=_12058" --out docs --zip

  python main.py init-db      # создать/обновить таблицы
  python main.py stats

База — PostgreSQL, адрес в файле .env (AML_DATABASE_URL).
"""
import argparse
import sys

from db import DatabaseNotConfigured, connect


def main():
    p = argparse.ArgumentParser(description="Поиск получателей в новостях о нарушениях (ОД/ФТ) "
                                            "и мониторинг негативных новостей о Казахстане")
    p.add_argument("--dsn", help="адрес PostgreSQL (по умолчанию AML_DATABASE_URL из .env)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="создать / обновить таблицы в базе")

    s = sub.add_parser("load-names", help="загрузить список имён (xlsx/csv)")
    s.add_argument("file")
    s.add_argument("--column", help="колонка с именами (по умолчанию первая)")

    s = sub.add_parser("collect", help="собрать новости о нарушениях")
    s.add_argument("--gdelt", action="store_true", help="GDELT (международные + KZ)")
    s.add_argument("--gnews", action="store_true", help="Google News RSS")
    s.add_argument("--rss", action="store_true", help="RSS-ленты из config.RSS_FEEDS")
    s.add_argument("--telegram", action="store_true",
                   help="Telegram-каналы из панели (Telegram → Каналы)")
    s.add_argument("--days", type=int, default=90, help="за сколько дней (GDELT до ~90, gnews ~30)")
    s.add_argument("--slice-days", type=int, help="резать период GDELT на окна (обходит лимит 250)")

    s = sub.add_parser("fetch-text", help="докачать полный текст статей")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--limit", type=int)

    s = sub.add_parser("match", help="прогнать новости по списку")
    s.add_argument("--all", action="store_true", help="перепроверить все статьи, а не только новые")
    s.add_argument("--keep-weak", action="store_true",
                   help="сохранять совпадения без ключевых слов рядом")

    s = sub.add_parser("export", help="выгрузить совпадения (xlsx/csv/json)")
    s.add_argument("out")
    s.add_argument("--min-score", type=int, default=0)
    s.add_argument("--status", help="только с этим статусом проверки (new/relevant/false_positive)")

    s = sub.add_parser("export-skipped", help="выгрузить имена, которые не ищутся, и причину")
    s.add_argument("out")

    s = sub.add_parser("dump", help="полный дамп скрининга в JSON (получатели, новости, совпадения)")
    s.add_argument("out", nargs="?", default="aml_dump.json")
    s.add_argument("--no-text", action="store_true", help="без полных текстов статей")

    # --- негативные новости ---
    s = sub.add_parser("neg-collect", help="собрать негативные новости о Казахстане")
    s.add_argument("--gnews", action="store_true", help="Google News (поисковые запросы)")
    s.add_argument("--gdelt", action="store_true", help="GDELT (запросы с негативной тональностью)")
    s.add_argument("--rss", action="store_true", help="RSS-ленты из раздела «Источники»")
    s.add_argument("--telegram", action="store_true", help="Telegram-каналы из раздела «Источники»")
    s.add_argument("--days", type=int, default=7, help="за сколько дней (по умолчанию 7)")
    s.add_argument("--source-id", type=int, help="только один источник из раздела «Источники»")

    s = sub.add_parser("neg-fetch-text", help="докачать полный текст негативных новостей")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--limit", type=int)

    s = sub.add_parser("neg-analyze", help="пересчитать категории, регионы и балл")
    s.add_argument("--all", action="store_true",
                   help="все новости (после правки словарей в config.py), а не только новые")

    s = sub.add_parser("neg-export", help="выгрузить негативные новости (xlsx/csv/json)")
    s.add_argument("out")
    s.add_argument("--min-score", type=int, default=0)
    s.add_argument("--status", choices=["new", "relevant", "irrelevant"],
                   help="new — не проверено, relevant — актуально, irrelevant — не актуально")
    s.add_argument("--days", type=int, help="только за последние N дней")
    s.add_argument("--no-text", action="store_true", help="без полных текстов")

    s = sub.add_parser("neg-dump", help="дамп негативных новостей и источников в JSON")
    s.add_argument("out", nargs="?", default="negative_dump.json")
    s.add_argument("--no-text", action="store_true", help="без полных текстов")

    sub.add_parser("stats", help="сводка по базе")

    s = sub.add_parser("govkz-docs", help="скачать все файлы раздела «Документы» с gov.kz")
    s.add_argument("link", help="ссылка на раздел документов (с фильтрами) или на один документ")
    s.add_argument("--out", default="govkz_docs", help="куда сохранить (по умолчанию govkz_docs)")
    s.add_argument("--lang", default="ru,kk",
                   help="языки через запятую: ru, kk, en (по умолчанию ru,kk)")
    s.add_argument("--zip", action="store_true", help="дополнительно упаковать всё в zip")

    a = p.parse_args()
    if a.cmd == "govkz-docs":      # база не нужна
        import govkz_docs
        langs = [x.strip() for x in a.lang.split(",") if x.strip()]
        bad = [x for x in langs if x not in govkz_docs.LANGS]
        if bad or not langs:
            print(f"ОШИБКА: неизвестный язык {', '.join(bad) or '(пусто)'}; можно ru, kk, en",
                  file=sys.stderr)
            return 2
        try:
            st = govkz_docs.download_all(a.link, a.out, langs, with_zip=a.zip)
        except govkz_docs.BadLink as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        print(f"Документов: {st['documents']}, файлов: {st['files']} — скачано {st['downloaded']} "
              f"({govkz_docs.human(st['bytes'])}), уже были {st['existing']}, "
              f"повторы {st['duplicates']}, ошибок {st['failed']}")
        return 1 if st["failed"] else 0

    try:
        conn = connect(a.dsn)
    except DatabaseNotConfigured as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2

    if a.cmd == "init-db":
        from db import SCHEMA_VERSION
        print(f"Схема базы в порядке (версия {SCHEMA_VERSION})")

    elif a.cmd == "load-names":
        from match import load_names
        for k, v in load_names(conn, a.file, a.column).items():
            print(f"{k}: {v}")
        if conn.execute("SELECT COUNT(*) FROM articles WHERE matched_at IS NOT NULL").fetchone()[0]:
            print("В базе уже есть обработанные статьи — запустите `match --all`, "
                  "чтобы проверить их и по новым именам.")

    elif a.cmd == "collect":
        import collect
        if not (a.gdelt or a.gnews or a.rss or a.telegram):
            a.gdelt = a.gnews = a.rss = a.telegram = True
        # быстрые источники сначала, медленный GDELT — в конце
        if a.gnews:
            print("Google News, новых статей:", collect.collect_gnews(conn, min(a.days, 30)))
        if a.rss:
            print("RSS, новых статей:", collect.collect_rss(conn))
        if a.telegram:
            import tg_collect
            print("Telegram, новых постов:", tg_collect.collect_telegram(conn, a.days))
        if a.gdelt:
            print("GDELT, новых статей:", collect.collect_gdelt(conn, a.days, a.slice_days))

    elif a.cmd == "fetch-text":
        import collect
        collect.fetch_text(conn, a.workers, a.limit)

    elif a.cmd == "match":
        from match import EmptyListError, run_match
        try:
            print("Совпадений записано/обновлено:", run_match(conn, a.all, a.keep_weak))
        except EmptyListError as e:
            print(f"[match] ОШИБКА: {e}", file=sys.stderr)
            return 2

    elif a.cmd == "export":
        from export import export_matches
        print("Выгружено строк:", export_matches(conn, a.out, a.min_score, a.status), "->", a.out)

    elif a.cmd == "export-skipped":
        from export import export_skipped
        print("Выгружено:", export_skipped(conn, a.out), "->", a.out)

    elif a.cmd == "dump":
        from export import dump_json
        counts = dump_json(conn, a.out, with_text=not a.no_text)
        print("Выгружено:", ", ".join(f"{k}: {v}" for k, v in counts.items()), "->", a.out)

    elif a.cmd == "neg-collect":
        import negative
        if not (a.gnews or a.gdelt or a.rss or a.telegram):
            a.gnews = a.gdelt = a.rss = a.telegram = True
        steps = []
        # быстрые и самые полезные источники — первыми, медленный GDELT — в конце
        if a.telegram:
            steps.append(("Telegram", lambda: negative.collect_telegram(
                conn, a.days, only_id=a.source_id)))
        if a.rss:
            steps.append(("RSS", lambda: negative.collect_rss(conn, a.days, a.source_id)))
        if a.gnews and not a.source_id:
            steps.append(("Google News", lambda: negative.collect_gnews(conn, a.days)))
        if a.gdelt and not a.source_id:
            steps.append(("GDELT", lambda: negative.collect_gdelt(conn, a.days)))
        for name, fn in steps:
            st = fn()
            print(f"{name}: {negative._fmt(st)}")

    elif a.cmd == "neg-fetch-text":
        import negative
        negative.fetch_text(conn, a.workers, a.limit)

    elif a.cmd == "neg-analyze":
        import negative
        negative.reanalyze(conn, a.all)

    elif a.cmd == "neg-export":
        from export import export_neg
        n = export_neg(conn, a.out, a.min_score, a.status, a.days, with_text=not a.no_text)
        print("Выгружено:", n, "->", a.out)

    elif a.cmd == "neg-dump":
        from export import NEG_DUMP_TABLES, dump_json
        counts = dump_json(conn, a.out, with_text=not a.no_text, tables=NEG_DUMP_TABLES)
        print("Выгружено:", ", ".join(f"{k}: {v}" for k, v in counts.items()), "->", a.out)

    elif a.cmd == "stats":
        q = lambda sql: conn.execute(sql).fetchall()
        print("Сущностей:", q("SELECT COUNT(*) FROM entities")[0][0],
              "| ищем:", q("SELECT COUNT(*) FROM entities WHERE skip_reason IS NULL")[0][0])
        print("Статей:", q("SELECT COUNT(*) FROM articles")[0][0])
        for r in q("SELECT collector, COUNT(*), COUNT(*) FILTER (WHERE text_status = 'ok') "
                   "FROM articles GROUP BY collector ORDER BY 2 DESC"):
            print(f"  {r[0]}: {r[1]} (с полным текстом: {r[2]})")
        print("Не обработано матчингом:", q("SELECT COUNT(*) FROM articles WHERE matched_at IS NULL")[0][0])
        print("Совпадений:", q("SELECT COUNT(*) FROM matches")[0][0])
        for r in q("SELECT CASE WHEN score >= 80 THEN '80-100' WHEN score >= 60 THEN '60-79' "
                   "ELSE '<60' END b, COUNT(*) FROM matches GROUP BY b ORDER BY b DESC"):
            print(f"  балл {r[0]}: {r[1]}")
        for r in q("SELECT review_status, COUNT(*) FROM matches GROUP BY review_status"):
            print(f"  статус {r[0]}: {r[1]}")
        print("Негативных новостей:", q("SELECT COUNT(*) FROM neg_news")[0][0])
        for r in q("SELECT collector, COUNT(*) FROM neg_news GROUP BY collector ORDER BY 2 DESC"):
            print(f"  {r[0]}: {r[1]}")
        for r in q("SELECT review_status, COUNT(*) FROM neg_news GROUP BY review_status"):
            print(f"  статус {r[0]}: {r[1]}")
        on, total = q("SELECT COUNT(*) FILTER (WHERE enabled), COUNT(*) FROM neg_sources")[0]
        print(f"Источников (RSS/Telegram): {total}, включено {on}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
