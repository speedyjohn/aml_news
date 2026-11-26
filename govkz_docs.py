"""
Скачивание документов госорганов с gov.kz.

Раздел «Документы» на gov.kz берёт данные из открытого API
/api/v1/public/content-manager/documents. Здесь разбирается ссылка на раздел
(со всеми фильтрами из неё), проходим все страницы и скачиваем файлы каждого
документа: основной текст (full_text) и приложения (other_files).
У казахской версии документов обычно свои файлы, поэтому по умолчанию
проходим и русскую, и казахскую.

  python main.py govkz-docs "https://www.gov.kz/org/esep/documents/1?directions=_12058&lang=ru"
"""
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

import config

SITE = "https://www.gov.kz"
API = SITE + "/api/v1/public/content-manager/documents"
LANGS = ("ru", "kk", "en")
PAGE_SIZE = 50
PAUSE_SEC = 0.3
TITLE_CHARS = 90
# параметры ссылки, которые не являются фильтрами
_NOT_FILTERS = {"lang", "page", "size", "sort-by"}

_session = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = config.HTTP_USER_AGENT
    return _session


class BadLink(ValueError):
    pass


@dataclass
class Source:
    slug: str                                   # организация: esep, adm и т.п.
    filters: dict = field(default_factory=dict)  # directions=12058, type=7 …
    doc_id: str | None = None                   # ссылка на один документ


def parse_link(link: str) -> Source:
    """Ссылка на раздел документов, на один документ или просто код организации."""
    link = (link or "").strip()
    if re.fullmatch(r"[a-z0-9_-]+", link, re.I):
        return Source(slug=link.lower())
    if not re.match(r"https?://", link, re.I):
        link = "https://" + link.lstrip("/")
    u = urlsplit(link)
    if not re.fullmatch(r"(www\.)?gov\.kz", u.hostname or "", re.I):
        raise BadLink("Нужна ссылка на gov.kz")
    m = re.search(r"/(?:org|memleket/entities)/([^/]+)/documents(?:/details/(\d+)|/\d*)?/?$",
                  u.path)
    if not m:
        raise BadLink("Это не раздел «Документы» организации на gov.kz: нужна ссылка вида "
                      "https://www.gov.kz/org/<орган>/documents/1?…")
    src = Source(slug=m.group(1).lower(), doc_id=m.group(2))
    for k, v in parse_qsl(u.query):
        if k not in _NOT_FILTERS and v:
            # на сайте значения фильтров пишутся с подчёркиванием: directions=_12058
            src.filters[k] = ",".join(x.strip().lstrip("_") for x in v.split(",") if x.strip())
    return src


def _get(url, params=None, lang="ru", stream=False, tries=4):
    for i in range(tries):
        try:
            r = session().get(url, params=params, headers={"Accept-Language": lang},
                              timeout=(15, 120), stream=stream)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            return r
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def list_documents(src: Source, lang: str = "ru") -> list[dict]:
    """Все документы по ссылке (все страницы)."""
    if src.doc_id:
        r = _get(f"{API}/{src.doc_id}", lang=lang)
        r.raise_for_status()
        return [r.json()]
    out, seen, page = [], set(), 1
    while True:
        params = {"projects": f"eq:{src.slug}", "page": page, "size": PAGE_SIZE,
                  "sort-by": "created_date:DESC", **src.filters}
        r = _get(API, params=params, lang=lang)
        data = r.json()
        if isinstance(data, dict):     # API вернул ошибку, например неизвестный фильтр
            raise BadLink(f"gov.kz не принял запрос: {data.get('message') or data}")
        new = [d for d in data if d.get("id") not in seen]
        seen.update(d.get("id") for d in new)
        out += new
        if len(data) < PAGE_SIZE or not new:
            return out
        page += 1
        time.sleep(PAUSE_SEC)


def doc_files(doc: dict) -> list[dict]:
    """Файлы документа: сначала основной текст, потом приложения."""
    files = []
    for kind, key in (("основной", "full_text"), ("приложение", "other_files")):
        for f in doc.get(key) or []:
            if f and f.get("document"):
                files.append({"kind": kind, "path": f["document"], "name": f.get("name") or ""})
    return files


def _items(doc, key, attr):
    items = (doc.get(key) or {}).get("items") or []
    return "; ".join(str(i.get(attr) or "") for i in items if i)


def safe_name(text: str, limit: int = TITLE_CHARS) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f«»“”]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return text.rstrip(" .") or "документ"


def file_name(doc: dict, f: dict, n_attach: int) -> str:
    ext = os.path.splitext(f["path"])[1].lower() or ".bin"
    date = (doc.get("created_date") or "")[:10]
    title = safe_name(doc.get("title") or f["name"])
    suffix = f" прил. {n_attach}" if f["kind"] == "приложение" else ""
    return f"{date} {title} ({doc.get('id')}){suffix}{ext}".strip()


def download(url: str, dest: Path) -> int:
    """Скачивает файл через временный .part. Уже скачанный файл не трогает."""
    if dest.exists() and dest.stat().st_size > 0:
        return -dest.stat().st_size      # отрицательный размер — «уже был»
    tmp = dest.with_name(dest.name + ".part")
    r = _get(url, stream=True)
    r.raise_for_status()
    size = 0
    with open(tmp, "wb") as fh:
        for chunk in r.iter_content(256 * 1024):
            fh.write(chunk)
            size += len(chunk)
    os.replace(tmp, dest)
    return size


def human(size: int) -> str:
    size = abs(size)
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024 or unit == "МБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024


def write_index(rows: list[dict], path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    cols = [("lang", "Язык", 6), ("id", "ID", 10), ("date", "Дата", 11), ("type", "Тип", 16),
            ("number", "Номер", 12), ("direction", "Направление", 28),
            ("title", "Название", 70), ("kind", "Файл", 11), ("file", "Имя файла", 60),
            ("size", "Размер", 10), ("status", "Статус", 18), ("url", "Ссылка на файл", 50),
            ("page", "Страница документа", 50)]
    wb = Workbook()
    ws = wb.active
    ws.title = "Документы"
    ws.append([c[1] for c in cols])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, (_, _, w) in enumerate(cols, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    for r in rows:
        ws.append([r.get(k, "") for k, _, _ in cols])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def make_zip(folder: Path) -> Path:
    zpath = folder.with_suffix(".zip")
    tmp = zpath.with_name(zpath.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and not p.name.endswith(".part"):
                z.write(p, p.relative_to(folder))
    os.replace(tmp, zpath)
    return zpath


def download_all(link: str, out_dir: str | Path, langs=("ru", "kk"), with_zip=False,
                 log=print) -> dict:
    """Проходит все документы по ссылке и скачивает их файлы в out_dir/<язык>/.
    Рядом кладёт опись «документы.xlsx». Возвращает счётчики."""
    src = parse_link(link)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    filters = ", ".join(f"{k}={v}" for k, v in src.filters.items()) or "без фильтров"
    log(f"[gov.kz] организация: {src.slug}, {('документ ' + src.doc_id) if src.doc_id else filters}")

    st = {"documents": 0, "files": 0, "downloaded": 0, "existing": 0, "duplicates": 0,
          "failed": 0, "bytes": 0}
    rows, by_url = [], {}
    for lang in langs:
        docs = list_documents(src, lang)
        log(f"[gov.kz] {lang}: документов {len(docs)}")
        st["documents"] = max(st["documents"], len(docs))
        folder = out / lang
        folder.mkdir(exist_ok=True)
        for i, doc in enumerate(docs, 1):
            files = doc_files(doc)
            if not files:
                log(f"[gov.kz] {lang} {i}/{len(docs)} #{doc.get('id')}: файлов нет")
            n_attach = 0
            for f in files:
                if f["kind"] == "приложение":
                    n_attach += 1
                name = file_name(doc, f, n_attach)
                url = SITE + f["path"]
                row = {"lang": lang, "id": doc.get("id"), "date": (doc.get("created_date") or "")[:10],
                       "type": _items(doc, "type", "type"), "number": doc.get("documentnumber") or "",
                       "direction": _items(doc, "directions", "title"),
                       "title": (doc.get("title") or "").strip(), "kind": f["kind"],
                       "file": f"{lang}/{name}", "url": url,
                       "page": f"{SITE}/org/{src.slug}/documents/details/{doc.get('id')}?lang={lang}"}
                st["files"] += 1
                if url in by_url:            # тот же файл уже скачан для другого языка
                    row.update(file=by_url[url]["file"], size=by_url[url].get("size", ""),
                               status="тот же файл")
                    st["duplicates"] += 1
                    rows.append(row)
                    continue
                try:
                    size = download(url, folder / name)
                    row["size"] = human(size)
                    if size < 0:
                        row["status"] = "уже был"
                        st["existing"] += 1
                    else:
                        row["status"] = "скачан"
                        st["downloaded"] += 1
                        st["bytes"] += size
                        time.sleep(PAUSE_SEC)
                    log(f"[gov.kz] {lang} {i}/{len(docs)} {row['status']}: {name} ({row['size']})")
                except Exception as e:
                    row["status"] = f"ошибка: {e}"[:200]
                    st["failed"] += 1
                    log(f"[gov.kz] {lang} {i}/{len(docs)} ОШИБКА {url}: {e}")
                by_url[url] = row
                rows.append(row)

    write_index(rows, out / "документы.xlsx")
    log(f"[gov.kz] опись: {out / 'документы.xlsx'}")
    if with_zip:
        z = make_zip(out)
        st["zip"] = str(z)
        log(f"[gov.kz] архив: {z} ({human(z.stat().st_size)})")
    return st
