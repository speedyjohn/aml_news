"""Данные для страницы «Обзор»."""
from urllib.parse import urlencode

from django.db.models import Count
from django.urls import reverse

import config as aml_config

from .models import Article, Entity, Job, Match, Schedule

STRONG_SCORE = 60   # с какого балла совпадение считаем «сильным»


def _url(name: str, **params) -> str:
    url = reverse(name)
    return f"{url}?{urlencode(params)}" if params else url


def dashboard_callback(request, context):
    matches, articles = "admin:screening_match_changelist", "admin:screening_article_changelist"

    cats = []
    for c in aml_config.KEYWORDS:
        n = Match.objects.filter(categories__icontains=c, score__gte=STRONG_SCORE).count()
        if n:
            cats.append({"name": c, "n": n,
                         "url": _url(matches, cat=c, score_from=STRONG_SCORE)})
    cats.sort(key=lambda x: -x["n"])
    top_n = max((c["n"] for c in cats), default=1)
    for c in cats:
        c["pct"] = round(100 * c["n"] / top_n)

    context.update({
        "kpi": [
            {"title": f"Ждут проверки (балл ≥ {STRONG_SCORE})", "icon": "pending_actions",
             "value": Match.objects.filter(score__gte=STRONG_SCORE, review_status="new").count(),
             "url": _url(matches, review_status__exact="new", score_from=STRONG_SCORE)},
            {"title": "Релевантных", "icon": "flag",
             "value": Match.objects.filter(review_status="relevant").count(),
             "url": _url(matches, review_status__exact="relevant")},
            {"title": "Всего совпадений", "icon": "rule",
             "value": Match.objects.count(), "url": _url(matches)},
            {"title": "Новостей в базе", "icon": "newspaper",
             "value": Article.objects.count(), "url": _url(articles)},
            {"title": "Получателей ищем", "icon": "groups",
             "value": Entity.objects.filter(skip_reason__isnull=True).count(),
             "url": _url("admin:screening_entity_changelist", searched="yes")},
            {"title": "Не прошли матчинг", "icon": "hourglass_top",
             "value": Article.objects.filter(matched_at__isnull=True).count(),
             "url": _url(articles, matched="no")},
        ],
        "top_matches": Match.objects.filter(review_status="new").order_by("-score", "-id")[:10],
        "categories": cats,
        "sources": Article.objects.values("collector").annotate(n=Count("id")).order_by("-n"),
        "last_jobs": Job.objects.order_by("-id")[:5],
        "jobs_url": _url("admin:screening_job_changelist"),
        "schedule": Schedule.objects.filter(pk=1).first(),
        "schedule_url": _url("admin:screening_schedule_changelist"),
        "new_job_url": _url("admin:screening_job_add"),
        "dump_url": _url("dump_json"),
        "dump_no_text_url": _url("dump_json", text=0),
    })
    return context
