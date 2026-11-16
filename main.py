#!/usr/bin/env python3
"""
Adverse media скрининг: собираем новости о нарушениях и ищем в них имена из списка.

  python main.py load-names список.xlsx
  python main.py collect --gdelt --gnews --rss
  python main.py fetch-text
  python main.py match
  python main.py export matches.xlsx --min-score 60
  python main.py dump aml_dump.json
  python main.py stats
"""
import argparse
import sys

import config
from db import connect


def main():
    p = argparse.ArgumentParser(description="Поиск получателей в новостях о нарушениях (ОД/ФТ)")
    p.add_argument("--db", default=config.DB_PATH, help="путь к SQLite (по умолчанию aml_news.sqlite)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("load-names", help="загрузить список имён (xlsx/csv)")
    s.add_argument("file")
    s.add_argument("--column", help="колонка с именами (по умолчанию первая)")

    s = sub.add_parser("collect", help="собрать новости о нарушениях")
    s.add_argument("--gdelt", action="store_true", help="GDELT (международные + KZ)")
    s.add_argument("--gnews", action="store_true", help="Google News RSS")
    s.add_argument("--rss", action="store_true", help="RSS-ленты из config.RSS_FEEDS")
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

    s = sub.add_parser("dump", help="полный дамп базы в JSON (получатели, новости, совпадения)")
    s.add_argument("out", nargs="?", default="aml_dump.json")
    s.add_argument("--no-text", action="store_true", help="без полных текстов статей")

    sub.add_parser("stats", help="сводка по базе")

    a = p.parse_args()
    conn = connect(a.db)

    if a.cmd == "load-names":
        from match import load_names
        for k, v in load_names(conn, a.file, a.column).items():
            print(f"{k}: {v}")
        if conn.execute("SELECT COUNT(*) FROM articles WHERE matched_at IS NOT NULL").fetchone()[0]:
            print("В базе уже есть обработанные статьи — запустите `match --all`, "
                  "чтобы проверить их и по новым именам.")

    elif a.cmd == "collect":
        import collect
        if not (a.gdelt or a.gnews or a.rss):
            a.gdelt = a.gnews = a.rss = True
        # быстрые источники сначала, медленный GDELT — в конце
        if a.gnews:
            print("Google News, новых статей:", collect.collect_gnews(conn, min(a.days, 30)))
        if a.rss:
            print("RSS, новых статей:", collect.collect_rss(conn))
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

    elif a.cmd == "stats":
        q = lambda sql: conn.execute(sql).fetchall()
        print("Сущностей:", q("SELECT COUNT(*) FROM entities")[0][0],
              "| ищем:", q("SELECT COUNT(*) FROM entities WHERE skip_reason IS NULL")[0][0])
        print("Статей:", q("SELECT COUNT(*) FROM articles")[0][0])
        for r in q("SELECT collector, COUNT(*), SUM(text_status='ok') FROM articles GROUP BY collector"):
            print(f"  {r[0]}: {r[1]} (с полным текстом: {r[2] or 0})")
        print("Не обработано матчингом:", q("SELECT COUNT(*) FROM articles WHERE matched_at IS NULL")[0][0])
        print("Совпадений:", q("SELECT COUNT(*) FROM matches")[0][0])
        for r in q("SELECT CASE WHEN score>=80 THEN '80-100' WHEN score>=60 THEN '60-79' "
                   "ELSE '<60' END b, COUNT(*) FROM matches GROUP BY b ORDER BY b DESC"):
            print(f"  балл {r[0]}: {r[1]}")
        for r in q("SELECT review_status, COUNT(*) FROM matches GROUP BY review_status"):
            print(f"  статус {r[0]}: {r[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
