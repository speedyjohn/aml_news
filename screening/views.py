"""Отдельные страницы панели, не привязанные к одной модели."""
import db as aml_db
from django.conf import settings
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header

from export import iter_dump_json


def dump_json_view(request):
    """Скачать всю базу скрининга одним JSON. ?text=0 — без полных текстов статей."""
    with_text = request.GET.get("text") != "0"

    def chunks():
        # подключение создаём уже внутри генератора — в том потоке, который отдаёт ответ
        conn = aml_db.connect(str(settings.AML_DB_PATH))
        try:
            for chunk in iter_dump_json(conn, with_text):
                yield chunk.encode("utf-8")
        finally:
            conn.close()

    name = f"aml_dump_{timezone.localtime():%Y-%m-%d_%H%M}{'' if with_text else '_no_text'}.json"
    resp = StreamingHttpResponse(chunks(), content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = content_disposition_header(True, name)
    return resp
