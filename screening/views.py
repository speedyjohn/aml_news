"""Отдельные страницы панели, не привязанные к одной модели."""
import db as aml_db
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header

from export import DUMP_TABLES, NEG_DUMP_TABLES, iter_dump_json


def _dump_response(request, tables, prefix):
    with_text = request.GET.get("text") != "0"

    def chunks():
        # подключение создаём уже внутри генератора — в том потоке, который отдаёт ответ
        conn = aml_db.connect(ensure=False)
        try:
            for chunk in iter_dump_json(conn, with_text, tables):
                yield chunk.encode("utf-8")
        finally:
            conn.close()

    name = f"{prefix}_{timezone.localtime():%Y-%m-%d_%H%M}{'' if with_text else '_no_text'}.json"
    resp = StreamingHttpResponse(chunks(), content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = content_disposition_header(True, name)
    return resp


def dump_json_view(request):
    """Скачать базу скрининга одним JSON. ?text=0 — без полных текстов статей."""
    return _dump_response(request, DUMP_TABLES, "aml_dump")


def neg_dump_json_view(request):
    """Скачать таблицы негативных новостей (новости + источники) одним JSON."""
    return _dump_response(request, NEG_DUMP_TABLES, "negative_dump")
