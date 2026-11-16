"""Обзор негативных новостей и их выгрузки (страницы панели вне списка моделей)."""
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import admin
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

import config as aml_config

from .models import NegNews, NegSource
from .neg_admin import NEG_CAT_SHORT, neg_json_response, neg_xlsx_response

PERIODS = [(1, "Сутки"), (7, "7 дней"), (30, "30 дней"), (90, "90 дней")]
STRONG = aml_config.NEG_STRONG_SCORE


def _period(request) -> int:
    try:
        days = int(request.GET.get("days", 7))
    except ValueError:
        days = 7
    return days if days in dict(PERIODS) else 7


def _period_qs(days: int):
    since = timezone.now() - timedelta(days=days)
    # дата публикации бывает неизвестна (Google News, часть RSS) — тогда по дате сбора
    return NegNews.objects.filter(Q(published__gte=since) |
                                  Q(published__isnull=True, collected_at__gte=since))


def _list_url(**params) -> str:
    url = reverse("admin:screening_negnews_changelist")
    return f"{url}?{urlencode(params)}" if params else url


def _bars(counter: Counter, url_param: str, days: int, limit: int = 12, short=None):
    top = counter.most_common(limit)
    peak = max((n for _, n in top), default=1)
    period = {"period": days} if days in (1, 7, 30, 90) else {}
    return [{"name": (short or {}).get(k, k), "full": k, "n": n,
             "pct": max(round(100 * n / peak), 2),
             "url": _list_url(**{url_param: k}, **period) if url_param else ""}
            for k, n in top]


def _split_counter(values) -> Counter:
    c = Counter()
    for v in values:
        for x in (v or "").split(";"):
            if x.strip():
                c[x.strip()] += 1
    return c


def overview_view(request):
    days = _period(request)
    qs = _period_qs(days)

    export = request.GET.get("export")
    if export in ("xlsx", "json"):
        sub = qs.exclude(review_status="irrelevant") if request.GET.get("skip_irrelevant") else qs
        if request.GET.get("strong"):
            sub = sub.filter(score__gte=STRONG)
        sub = sub.order_by("-published", "-id")
        name = f"negative_{days}d_{timezone.localtime():%Y-%m-%d_%H%M}"
        if export == "xlsx":
            return neg_xlsx_response(sub, name + ".xlsx")
        return neg_json_response(sub, name + ".json")

    agg = qs.aggregate(
        total=Count("id"), strong=Count("id", filter=Q(score__gte=STRONG)),
        new_strong=Count("id", filter=Q(score__gte=STRONG, review_status="new")),
        relevant=Count("id", filter=Q(review_status="relevant")),
        new=Count("id", filter=Q(review_status="new")),
        sources=Count("source", distinct=True),
    )

    # по дням (по местному времени); дни без новостей тоже показываем
    today = timezone.localdate()
    chart_days = max(days, 14)
    start = today - timedelta(days=chart_days - 1)
    daily = {r["d"]: r for r in (
        _period_qs(chart_days).exclude(review_status="irrelevant")
        .annotate(d=TruncDate("published", tzinfo=timezone.get_current_timezone()))
        .values("d").annotate(n=Count("id"), s=Count("id", filter=Q(score__gte=STRONG))))}
    series = []
    for i in range(chart_days):
        d = start + timedelta(days=i)
        r = daily.get(d, {})
        series.append({"date": d, "n": r.get("n", 0), "s": r.get("s", 0)})
    peak = max((x["n"] for x in series), default=0) or 1
    for x in series:
        # строкой: шаблон с русской локалью превратил бы 37.5 в «37,5» и сломал CSS
        x["h"] = f"{100 * x['n'] / peak:.1f}"
        x["rest"] = x["n"] - x["s"]
    no_date = daily.get(None, {}).get("n", 0)

    rows = qs.exclude(review_status="irrelevant").values_list("categories", "regions", "source")
    rows = list(rows)
    cats = _split_counter(r[0] for r in rows)
    regs = _split_counter(r[1] for r in rows)
    srcs = Counter(r[2] for r in rows if r[2])

    latest = (qs.filter(score__gte=STRONG, review_status="new")
              .order_by("-published", "-score")[:12])

    sources_err = NegSource.objects.filter(enabled=True, last_status__startswith="error").count()
    stale_since = timezone.now() - timedelta(days=14)
    sources_stale = NegSource.objects.filter(enabled=True, last_item_at__lt=stale_since).count()

    def period_url(d):
        return f"?days={d}"

    def export_url(fmt, **extra):
        return "?" + urlencode({"days": days, "export": fmt, **extra})

    context = {
        **admin.site.each_context(request),
        "title": "Негативные новости РК",
        "days": days,
        "periods": [{"d": d, "label": lbl, "url": period_url(d), "active": d == days}
                    for d, lbl in PERIODS],
        "kpi": [
            {"title": f"Сильные, ждут проверки (≥ {STRONG})", "icon": "pending_actions",
             "value": agg["new_strong"],
             "url": _list_url(review_status__exact="new", strength="strong", period=days)},
            {"title": "Отмечены актуальными", "icon": "flag", "value": agg["relevant"],
             "url": _list_url(review_status__exact="relevant", period=days)},
            {"title": "Всего найдено", "icon": "campaign", "value": agg["total"],
             "url": _list_url(period=days)},
            {"title": f"Сильные (балл ≥ {STRONG})", "icon": "priority_high",
             "value": agg["strong"], "url": _list_url(strength="strong", period=days)},
            {"title": "Не проверено", "icon": "hourglass_top", "value": agg["new"],
             "url": _list_url(review_status__exact="new", period=days)},
            {"title": "Изданий", "icon": "newspaper", "value": agg["sources"],
             "url": reverse("admin:screening_negsource_changelist")},
        ],
        "series": series,
        "series_peak": peak,
        "series_mid": peak // 2 if peak >= 4 else None,
        "chart_days": chart_days,
        "no_date": no_date,
        "categories": _bars(cats, "cat", days, short=None),
        "regions": _bars(regs, "region", days, limit=21),
        "sources": _bars(srcs, "source__icontains", days, limit=10),
        "latest": latest,
        "cat_short": NEG_CAT_SHORT,
        "sources_err": sources_err,
        "sources_stale": sources_stale,
        "sources_url": reverse("admin:screening_negsource_changelist"),
        "list_url": _list_url(period=days),
        "export_xlsx": export_url("xlsx"),
        "export_xlsx_strong": export_url("xlsx", strong=1, skip_irrelevant=1),
        "export_json": export_url("json"),
        "dump_url": reverse("neg_dump_json"),
        "dump_no_text_url": reverse("neg_dump_json") + "?text=0",
        "strong": STRONG,
    }
    return TemplateResponse(request, "admin/screening/negnews/overview.html", context)
