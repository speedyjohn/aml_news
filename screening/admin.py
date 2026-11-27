"""Админ-панель: совпадения, новости, список получателей (с профилем), запуски, автозапуск.
Негативные новости о Казахстане — в neg_admin.py."""
import io
import os
import re
from urllib.parse import urlencode, urlparse

import pandas as pd
from django.contrib import admin, messages
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, Max, Min, OuterRef, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import escape, format_html
from django.utils.http import content_disposition_header
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import (ChoicesDropdownFilter, FieldTextFilter,
                                          RangeNumericFilter)
from unfold.decorators import action, display
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

import config as aml_config

from . import jobs
from .models import REVIEW_CHOICES, Article, Entity, Job, Match, Schedule

admin.site.site_header = admin.site.site_title = "AML-скрининг"
admin.site.index_title = "Обзор"

# --- Пользователи в стиле Unfold ------------------------------------------------
admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass


# --- Фильтры --------------------------------------------------------------------
class CategoryFilter(admin.SimpleListFilter):
    title = "Категория нарушения"
    parameter_name = "cat"

    def lookups(self, request, model_admin):
        return [(c, c) for c in aml_config.KEYWORDS]

    def queryset(self, request, qs):
        return qs.filter(categories__icontains=self.value()) if self.value() else qs


class ScoreBucketFilter(admin.SimpleListFilter):
    title = "Балл"
    parameter_name = "bucket"

    def lookups(self, request, model_admin):
        return [("80", "80–100 (сильные)"), ("60", "60–79"), ("0", "меньше 60")]

    def queryset(self, request, qs):
        v = self.value()
        if v == "80":
            return qs.filter(score__gte=80)
        if v == "60":
            return qs.filter(score__gte=60, score__lt=80)
        if v == "0":
            return qs.filter(score__lt=60)
        return qs


CAT_SHORT = {
    "ОД (отмывание)": "ОД", "ФТ / экстремизм": "ФТ", "Санкции": "Санкции",
    "Мошенничество / хищения": "Мошенничество", "Коррупция": "Коррупция",
    "Уголовное преследование": "Уголовное", "Контрабанда / налоги": "Контрабанда",
    "Регулятор / финмониторинг": "АФМ / регулятор",
}


def _score_label(score):
    if score is None:
        return "-"
    kind = "danger" if score >= 80 else "warning" if score >= 60 else "info"
    return kind, str(score)


def _highlight(snippet: str | None, needle: str | None):
    if not snippet:
        return "-"
    html = escape(snippet)
    if needle:
        html = re.sub(re.escape(escape(needle)),
                      lambda m: f'<mark style="background:#fde68a;padding:0 2px">{m.group(0)}</mark>',
                      html, flags=re.I)
    return mark_safe(f'<div style="max-width:900px;line-height:1.6">{html}</div>')


def _xlsx_response(df: pd.DataFrame, filename: str) -> HttpResponse:
    from export import naive_local
    df = naive_local(df)        # Excel не понимает даты с часовым поясом
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Лист1")
        ws = xw.sheets["Лист1"]
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col[:200]), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 70)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    resp = HttpResponse(buf.getvalue(), content_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    resp["Content-Disposition"] = content_disposition_header(True, filename)
    return resp


def _filtered_queryset(model_admin, request):
    """Queryset списка с теми фильтрами и поиском, что сейчас открыты на странице
    (берём их из адреса страницы, с которой нажали кнопку)."""
    query = urlparse(request.META.get("HTTP_REFERER", "")).query
    orig = request.GET
    request.GET = QueryDict(query)
    try:
        cl = model_admin.get_changelist_instance(request)
        return cl.get_queryset(request)
    finally:
        request.GET = orig


def _local_date(dt, fmt="d.m.Y"):
    return date_format(timezone.localtime(dt), fmt) if dt else ""


def _ext_link(url, text=None):
    if not url:
        return "-"
    return format_html('<a href="{}" target="_blank" rel="noopener" class="text-primary-600 '
                       'underline">{}</a>', url, text or url)


# --- Совпадения -----------------------------------------------------------------
@admin.register(Match)
class MatchAdmin(ModelAdmin):
    list_display = ("score_badge", "entity_link", "matched_text", "cat_badges", "news_cell",
                    "review_badge")
    list_display_links = None
    list_filter = (("review_status", ChoicesDropdownFilter), ScoreBucketFilter, CategoryFilter,
                   ("entity_kind", ChoicesDropdownFilter), "frequent", "in_title",
                   ("source", FieldTextFilter), ("score", RangeNumericFilter))
    list_filter_submit = True
    search_fields = ("entity_name", "matched_text", "title", "url", "snippet")
    search_help_text = "Поиск по имени, заголовку, ссылке и фрагменту"
    ordering = ("-score", "-id")
    list_per_page = 50
    actions = ["mark_relevant", "mark_false", "mark_new", "export_xlsx"]
    actions_list = ["export_filtered"]
    actions_row = ["row_relevant", "row_false", "row_open"]
    actions_detail = ["detail_relevant", "detail_false", "detail_open"]

    fieldsets = (
        ("Совпадение", {"fields": ("entity_profile", "matched_text", "score", "score_raw",
                                   "categories", "keywords", "frequent", "in_title")}),
        ("Новость", {"fields": ("title", "article_link", "source", "published", "lang",
                                "snippet_view")}),
        ("Проверка", {"fields": ("review_status", "review_note")}),
    )
    readonly_fields = ("entity_profile", "matched_text", "score", "score_raw", "categories",
                       "keywords", "frequent", "in_title", "title", "article_link", "source",
                       "published", "lang", "snippet_view")

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("entity")

    # колонки
    @display(description="Балл", ordering="score", label={"danger": "danger",
                                                          "warning": "warning", "info": "info"})
    def score_badge(self, obj):
        return _score_label(obj.score)

    @display(description="Имя / организация", ordering="entity_name")
    def entity_link(self, obj):
        url = reverse("admin:screening_match_change", args=[obj.pk])
        freq = mark_safe(' · <span class="text-orange-600" title="Имя встречается в очень многих '
                         'статьях — вероятно, обычное слово">частое имя</span>') if obj.frequent else ""
        profile = reverse("admin:screening_entity_change", args=[obj.entity_id])
        return format_html('<div style="min-width:180px"><a href="{}" class="font-medium">{}</a>'
                           '<div class="text-subtle text-[11px] mt-0.5">{}{} · '
                           '<a href="{}" class="text-primary-600 hover:underline">профиль</a>'
                           '</div></div>',
                           url, obj.entity_name, obj.get_entity_kind_display() or "", freq,
                           profile)

    @display(description="Имя / организация")
    def entity_profile(self, obj):
        url = reverse("admin:screening_entity_change", args=[obj.entity_id])
        return format_html('<a href="{}" class="text-primary-600 font-medium underline">{}</a>'
                           ' <span class="text-subtle">— открыть профиль, все новости по имени</span>',
                           url, obj.entity_name)

    @display(description="Нарушения", label=True)
    def cat_badges(self, obj):
        cats = [c.strip() for c in (obj.categories or "").split(";") if c.strip()]
        return [CAT_SHORT.get(c, c) for c in cats] or "-"

    @display(description="Новость", ordering="title")
    def news_cell(self, obj):
        t = obj.title or obj.url or ""
        t = t[:110] + "…" if len(t) > 110 else t
        meta = " · ".join(x for x in (obj.source, _local_date(obj.published)) if x)
        return format_html('<div style="min-width:260px;max-width:420px">'
                           '<a href="{}" target="_blank" rel="noopener" class="text-primary-600 '
                           'hover:underline">{}</a><div class="text-subtle text-xs mt-0.5">{}</div>'
                           '</div>', obj.url, t, meta)

    @display(description="Статус", ordering="review_status",
             label={"new": "warning", "relevant": "danger", "false_positive": "info"})
    def review_badge(self, obj):
        short = {"new": "Не проверено", "relevant": "Релевантно", "false_positive": "Ложное"}
        return obj.review_status, short.get(obj.review_status, obj.review_status)

    @display(description="Ссылка")
    def article_link(self, obj):
        return _ext_link(obj.url)

    @display(description="Фрагмент")
    def snippet_view(self, obj):
        return _highlight(obj.snippet, obj.matched_text)

    # массовые действия
    def _set(self, request, qs, status):
        n = qs.update(review_status=status)
        self.message_user(request, f"Обновлено: {n}", messages.SUCCESS)

    @admin.action(description="Отметить: релевантно")
    def mark_relevant(self, request, qs):
        self._set(request, qs, "relevant")

    @admin.action(description="Отметить: ложное срабатывание")
    def mark_false(self, request, qs):
        self._set(request, qs, "false_positive")

    @admin.action(description="Сбросить статус проверки")
    def mark_new(self, request, qs):
        self._set(request, qs, "new")

    def _matches_df(self, qs):
        cols = ["score", "entity_name", "entity_kind", "matched_text", "categories", "keywords",
                "source", "title", "url", "published", "lang", "in_title", "frequent",
                "snippet", "review_status", "review_note", "id"]
        names = {f.name: str(f.verbose_name) for f in Match._meta.fields}
        names["id"] = "ID совпадения"
        df = pd.DataFrame(list(qs.values(*cols)), columns=cols)
        if not df.empty:
            df["entity_kind"] = df["entity_kind"].map(dict(Match._meta.get_field("entity_kind").choices))
            df["review_status"] = df["review_status"].map(
                dict(Match._meta.get_field("review_status").choices))
            df["in_title"] = df["in_title"].map({True: "да", False: ""})
            df["frequent"] = df["frequent"].map({True: "да", False: ""})
        return df.rename(columns=names)

    @admin.action(description="Выгрузить выбранные в Excel")
    def export_xlsx(self, request, qs):
        return _xlsx_response(self._matches_df(qs.order_by("-score", "-id")), "matches.xlsx")

    @action(description="Выгрузить в Excel (по текущим фильтрам)", icon="download")
    def export_filtered(self, request):
        qs = _filtered_queryset(self, request)
        return _xlsx_response(self._matches_df(qs), "matches.xlsx")

    # кнопки в строке
    def _quick(self, request, object_id, status):
        Match.objects.filter(pk=object_id).update(review_status=status)
        return redirect(request.META.get("HTTP_REFERER") or
                        reverse("admin:screening_match_changelist"))

    @action(description="Релевантно", icon="flag")
    def row_relevant(self, request, object_id):
        return self._quick(request, object_id, "relevant")

    @action(description="Ложное", icon="block")
    def row_false(self, request, object_id):
        return self._quick(request, object_id, "false_positive")

    @action(description="Открыть новость", icon="open_in_new")
    def row_open(self, request, object_id):
        return redirect(Match.objects.get(pk=object_id).url)

    def _detail(self, request, object_id, status, message):
        Match.objects.filter(pk=object_id).update(review_status=status)
        messages.success(request, message)
        return redirect(reverse("admin:screening_match_change", args=[object_id]))

    @action(description="Релевантно", icon="flag", variant="danger")
    def detail_relevant(self, request, object_id):
        return self._detail(request, object_id, "relevant", "Отмечено как релевантное")

    @action(description="Ложное срабатывание", icon="block")
    def detail_false(self, request, object_id):
        return self._detail(request, object_id, "false_positive",
                            "Отмечено как ложное срабатывание")

    @action(description="Открыть новость", icon="open_in_new")
    def detail_open(self, request, object_id):
        return self.row_open(request, object_id)


# --- Новости --------------------------------------------------------------------
class MatchInline(TabularInline):
    model = Match
    fk_name = "article"
    fields = ("score", "entity_profile", "matched_text", "categories", "review_status")
    readonly_fields = ("score", "entity_profile", "matched_text", "categories")
    extra = 0
    can_delete = False
    show_change_link = True
    verbose_name_plural = "Совпадения в этой новости"

    def has_add_permission(self, request, obj=None):
        return False

    @display(description="Имя / организация")
    def entity_profile(self, obj):
        url = reverse("admin:screening_entity_change", args=[obj.entity_id])
        return format_html('<a href="{}" class="text-primary-600 underline">{}</a>',
                           url, obj.entity_name)


class MatchedFilter(admin.SimpleListFilter):
    title = "Матчинг"
    parameter_name = "matched"

    def lookups(self, request, model_admin):
        return [("no", "Ещё не проверены по списку"), ("yes", "Проверены")]

    def queryset(self, request, qs):
        if self.value() in ("yes", "no"):
            return qs.filter(matched_at__isnull=self.value() == "no")
        return qs


@admin.register(Article)
class ArticleAdmin(ModelAdmin):
    list_display = ("title_short", "source", "collector", "published_short", "text_status",
                    "matches_count", "open_link")
    list_filter = (("collector", ChoicesDropdownFilter), MatchedFilter, "text_status", "lang",
                   ("source", FieldTextFilter))
    list_filter_submit = True
    search_fields = ("title", "url", "summary", "source")
    ordering = ("-id",)
    list_per_page = 50
    inlines = [MatchInline]
    fieldsets = (
        (None, {"fields": ("title", "open_link", "source", "published", "lang", "country",
                           "collector", "query", "text_status", "collected_at", "matched_at")}),
        ("Текст", {"fields": ("summary", "text"), "classes": ["collapse"]}),
    )

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Article._meta.fields] + ["open_link"]

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_matches=Count("match"))

    @display(description="Заголовок", ordering="title")
    def title_short(self, obj):
        t = obj.title or obj.url
        return t[:110] + "…" if len(t) > 110 else t

    @display(description="Дата", ordering="published")
    def published_short(self, obj):
        return _local_date(obj.published)

    @display(description="Совпадений", ordering="n_matches")
    def matches_count(self, obj):
        return obj.n_matches or "-"

    @display(description="Ссылка")
    def open_link(self, obj):
        return _ext_link(obj.url, "открыть")


# --- Список получателей ---------------------------------------------------------
class SearchedFilter(admin.SimpleListFilter):
    title = "Ищется в новостях"
    parameter_name = "searched"

    def lookups(self, request, model_admin):
        return [("yes", "Ищется"), ("no", "Не ищется (пропущено)")]

    def queryset(self, request, qs):
        if self.value() == "yes":
            return qs.filter(skip_reason__isnull=True)
        if self.value() == "no":
            return qs.filter(skip_reason__isnull=False)
        return qs


class HasMatchesFilter(admin.SimpleListFilter):
    title = "Найдено в новостях"
    parameter_name = "found"

    def lookups(self, request, model_admin):
        return [("yes", "Есть совпадения"), ("relevant", "Есть релевантные"),
                ("no", "Совпадений нет")]

    def queryset(self, request, qs):
        # Exists, а не join: иначе счётчик «Совпадений» в списке задваивается
        matches = Match.objects.filter(entity_id=OuterRef("pk"))
        if self.value() == "yes":
            return qs.filter(Exists(matches))
        if self.value() == "relevant":
            return qs.filter(Exists(matches.filter(review_status="relevant")))
        if self.value() == "no":
            return qs.filter(~Exists(matches))
        return qs


@admin.register(Entity)
class EntityAdmin(ModelAdmin):
    list_display = ("name", "kind_badge", "variants_short", "skip_reason", "matches_count")
    list_filter = (HasMatchesFilter, SearchedFilter, ("kind", ChoicesDropdownFilter),
                   "skip_reason")
    actions = ["export_xlsx"]
    actions_list = ["export_filtered", "export_skipped"]
    search_fields = ("name", "variants", "core")
    ordering = ("name",)
    list_per_page = 50
    fields = ("name", "kind", "variants", "core", "pattern", "skip_reason", "matches_link")

    def get_readonly_fields(self, request, obj=None):
        return list(self.fields)

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_matches=Count("match"))

    @display(description="Тип", label={"Физлицо": "primary", "Юрлицо": "info"})
    def kind_badge(self, obj):
        return obj.get_kind_display()

    @display(description="Другие написания")
    def variants_short(self, obj):
        v = obj.variants or ""
        return v[:80] + "…" if len(v) > 80 else v

    @display(description="Совпадений", ordering="n_matches")
    def matches_count(self, obj):
        return obj.n_matches or "-"

    def _entities_df(self, qs):
        df = pd.DataFrame(list(qs.values("name", "kind", "variants", "skip_reason")),
                          columns=["name", "kind", "variants", "skip_reason"])
        df["kind"] = df["kind"].map({"org": "Юрлицо", "person": "Физлицо"})
        return df.rename(columns={"name": "Имя / организация", "kind": "Тип",
                                  "variants": "Другие написания",
                                  "skip_reason": "Почему не ищем"})

    @admin.action(description="Выгрузить выбранные в Excel")
    def export_xlsx(self, request, qs):
        return _xlsx_response(self._entities_df(qs), "recipients.xlsx")

    @action(description="Выгрузить в Excel (по фильтрам)", icon="download")
    def export_filtered(self, request):
        return _xlsx_response(self._entities_df(_filtered_queryset(self, request)), "recipients.xlsx")

    @action(description="Какие имена не ищутся", icon="block")
    def export_skipped(self, request):
        qs = Entity.objects.filter(skip_reason__isnull=False).order_by("skip_reason", "name")
        return _xlsx_response(self._entities_df(qs), "skipped.xlsx")

    @display(description="Совпадения")
    def matches_link(self, obj):
        url = reverse("admin:screening_match_changelist") + f"?entity__id__exact={obj.pk}"
        return format_html('<a href="{}" class="text-primary-600 underline">Показать все</a>', url)

    # --- Профиль: вместо формы с техническими полями открывается сводка по имени
    def change_view(self, request, object_id, form_url="", extra_context=None):
        if request.GET.get("_details"):
            return super().change_view(request, object_id, form_url, extra_context)
        return self.profile_view(request, object_id)

    def profile_view(self, request, object_id):
        ent = get_object_or_404(Entity, pk=object_id)
        base = Match.objects.filter(entity_id=ent.pk)

        status = request.GET.get("status", "")
        order = request.GET.get("o", "date")
        qs = base.filter(review_status=status) if status in dict(REVIEW_CHOICES) else base
        qs = qs.order_by(*(("-score", "-published") if order == "score" else ("-published", "-score")))

        export = request.GET.get("export")
        if export in ("xlsx", "json"):
            safe = re.sub(r"[^\w\- ]+", "", ent.name)[:60].strip() or f"entity_{ent.pk}"
            if export == "xlsx":
                df = self.admin_site._registry[Match]._matches_df(qs)
                return _xlsx_response(df, f"{safe}.xlsx")
            data = {
                "entity": {"id": ent.pk, "name": ent.name, "kind": ent.kind,
                           "variants": [v.strip() for v in (ent.variants or "").split("|")
                                        if v.strip()],
                           "skip_reason": ent.skip_reason},
                "matches": list(qs.values(
                    "id", "article_id", "score", "title", "url", "source", "published", "lang",
                    "matched_text", "categories", "keywords", "in_title", "frequent", "snippet",
                    "review_status", "review_note")),
            }
            resp = JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
            resp["Content-Disposition"] = content_disposition_header(True, f"{safe}.json")
            return resp

        stats = base.aggregate(
            total=Count("id"), max_score=Max("score"), first=Min("published"),
            last=Max("published"), sources=Count("source", distinct=True),
            new=Count("id", filter=Q(review_status="new")),
            relevant=Count("id", filter=Q(review_status="relevant")),
            false=Count("id", filter=Q(review_status="false_positive")),
            in_title=Count("id", filter=Q(in_title=True)),
        )
        cats: dict[str, int] = {}
        for line in base.exclude(review_status="false_positive").values_list("categories", flat=True):
            for c in (line or "").split(";"):
                if c.strip():
                    cats[c.strip()] = cats.get(c.strip(), 0) + 1
        top = max(cats.values(), default=1)
        categories = [{"name": k, "n": v, "pct": round(100 * v / top)}
                      for k, v in sorted(cats.items(), key=lambda x: -x[1])]

        # кто ещё упоминается в тех же новостях
        related = (Match.objects.filter(article_id__in=base.values("article_id"))
                   .exclude(entity_id=ent.pk).exclude(review_status="false_positive")
                   .values("entity_id", "entity_name", "entity_kind")
                   .annotate(n=Count("id")).order_by("-n", "entity_name")[:15])

        page = Paginator(qs, 30).get_page(request.GET.get("p"))
        rows = [{"m": m, "snippet": _highlight(m.snippet, m.matched_text),
                 "cats": [CAT_SHORT.get(c.strip(), c.strip())
                          for c in (m.categories or "").split(";") if c.strip()]}
                for m in page]

        def qs_url(**kw):
            params = {"status": status, "o": order}
            params.update(kw)
            return "?" + urlencode({k: v for k, v in params.items() if v})

        tabs = [("", "Все", stats["total"]), ("new", "Не проверено", stats["new"]),
                ("relevant", "Релевантно", stats["relevant"]),
                ("false_positive", "Ложные", stats["false"])]
        context = {
            **self.admin_site.each_context(request),
            "opts": Entity._meta, "original": ent, "title": ent.name,
            "ent": ent, "stats": stats, "categories": categories, "related": related,
            "variants": [v.strip() for v in (ent.variants or "").split("|") if v.strip()],
            "rows": rows, "page": page, "status": status, "order": order,
            "tabs": [{"key": k, "label": lbl, "n": n, "url": qs_url(status=k, p=""),
                      "active": k == status} for k, lbl, n in tabs],
            "order_date_url": qs_url(o="date", p=""), "order_score_url": qs_url(o="score", p=""),
            "prev_url": qs_url(p=page.previous_page_number()) if page.has_previous() else "",
            "next_url": qs_url(p=page.next_page_number()) if page.has_next() else "",
            "export_url": qs_url(export="xlsx", p=""),
            "export_json_url": qs_url(export="json", p=""),
            "details_url": "?_details=1",
        }
        return TemplateResponse(request, "admin/screening/entity/profile.html", context)


# --- Запуски --------------------------------------------------------------------
@admin.register(Job)
class JobAdmin(ModelAdmin):
    list_display = ("id", "command", "status_badge", "trigger", "created_at", "started_at",
                    "finished_at", "duration")
    list_display_links = ("id", "command")
    actions_list = ["run_pipeline", "run_neg", "run_collect", "run_telegram", "run_fetch",
                    "run_match", "run_govkz"]
    actions_detail = ["stop_job", "rerun_job"]
    warn_unsaved_form = True

    add_fieldsets = (
        (None, {"fields": ("command",)}),
        # классы job-* — по ним panel.css прячет блоки, не относящиеся к выбранной команде
        ("Сбор новостей", {"fields": ("use_gdelt", "use_gnews", "use_rss", "use_telegram", "days",
                                      "slice_days"),
                           "classes": ["job-collect"]}),
        ("Матчинг", {"fields": ("match_all", "keep_weak"), "classes": ["job-match"]}),
        ("Загрузка списка", {"fields": ("names_file", "names_column"),
                             "classes": ["job-names"],
                             "description": "Если приложить файл к «Полному циклу» или "
                                            "«Матчингу», список сначала загрузится, а затем "
                                            "будут перепроверены все статьи."}),
        ("Документы gov.kz", {"fields": ("source_url", "doc_langs"), "classes": ["job-govkz"]}),
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        if obj.command == "govkz-docs":
            return (
                (None, {"fields": ("command", "status_text", "trigger", "created_at",
                                   "started_at", "finished_at", "returncode")}),
                ("Документы gov.kz", {"fields": ("source_link", "doc_langs", "docs_result")}),
                ("Лог", {"fields": ("log_view",)}),
            )
        return (
            (None, {"fields": ("command", "status_text", "trigger", "created_at", "started_at",
                               "finished_at", "returncode")}),
            ("Параметры", {"fields": ("use_gdelt", "use_gnews", "use_rss", "use_telegram",
                                      "neg_source_id", "days",
                                      "slice_days", "match_all", "keep_weak", "names_file_name",
                                      "names_column"), "classes": ["collapse"]}),
            ("Лог", {"fields": ("log_view",)}),
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return [f.name for f in Job._meta.fields] + ["status_text", "log_view", "names_file_name",
                                                     "source_link", "docs_result"]

    # --- результат «Скачать документы с gov.kz»: архив и опись ---
    _DOC_FILES = {"zip": lambda job: jobs.docs_folder(job).with_suffix(".zip"),
                  "index": lambda job: jobs.docs_folder(job) / "документы.xlsx"}

    def get_urls(self):
        return [path("<int:job_id>/docs/<str:kind>/", self.admin_site.admin_view(self.docs_file),
                     name="screening_job_docs")] + super().get_urls()

    def docs_file(self, request, job_id, kind):
        job = get_object_or_404(Job, pk=job_id, command="govkz-docs")
        if kind not in self._DOC_FILES or not self.has_view_permission(request, job):
            raise Http404
        f = self._DOC_FILES[kind](job)
        if not f.is_file():
            raise Http404("Файл ещё не готов")
        name = f"govkz_job_{job.pk}" + (".zip" if kind == "zip" else ".xlsx")
        return FileResponse(open(f, "rb"), as_attachment=True, filename=name)

    def delete_model(self, request, obj):
        jobs.remove_docs(obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            jobs.remove_docs(obj)
        super().delete_queryset(request, queryset)

    @display(description="Ссылка")
    def source_link(self, obj):
        if not obj.source_url:
            return "-"
        return format_html('<a href="{}" target="_blank" rel="noopener" class="text-primary-600 '
                           'break-all">{}</a>', obj.source_url, obj.source_url)

    @display(description="Результат")
    def docs_result(self, obj):
        folder = jobs.docs_folder(obj)
        zpath, index = folder.with_suffix(".zip"), folder / "документы.xlsx"
        if not zpath.is_file():
            if obj.status in ("queued", "running"):
                return "Скачивается — архив появится здесь, когда запуск закончится"
            if folder.is_dir():
                return f"Архив не собран, файлы лежат в папке {folder}"
            return "-"
        n = sum(1 for p in folder.rglob("*") if p.is_file() and p.suffix != ".part"
                and p.name != index.name)
        size = zpath.stat().st_size / 1024 / 1024
        url = lambda kind: reverse("admin:screening_job_docs", args=[obj.pk, kind])
        return format_html(
            '<div class="pf-row"><a class="pf-btn primary" href="{}"><span class="material-symbols-outlined">'
            'download</span>Скачать архив ({} файлов, {} МБ)</a>'
            '<a class="pf-btn" href="{}"><span class="material-symbols-outlined">table_view</span>'
            'Опись документов (xlsx)</a></div><div class="pf-muted pf-small" style="margin-top:6px">'
            'Папка на диске: {}</div>', url("zip"), n, f"{size:.1f}", url("index"), folder)

    @display(description="Файл со списком")
    def names_file_name(self, obj):
        return os.path.basename(obj.names_file.name) if obj.names_file else "-"

    def has_change_permission(self, request, obj=None):
        return obj is None   # уже созданный запуск только просматривается

    def changelist_view(self, request, extra_context=None):
        jobs.reconcile()
        return super().changelist_view(request, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        jobs.reconcile()
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            busy = jobs.is_busy()
            transaction.on_commit(lambda: jobs.submit(obj))
            messages.info(request, "Поставлено в очередь — сейчас выполняется другой запуск"
                          if busy else "Запущено")

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context.update({"show_save_and_add_another": False, "show_save_and_continue": False,
                        "show_save_and_view": False})
        return super().render_change_form(request, context, add, change, form_url, obj)

    def response_add(self, request, obj, post_url_continue=None):
        return redirect(reverse("admin:screening_job_change", args=[obj.pk]))

    @display(description="Статус", label={"queued": "warning", "running": "primary",
                                           "done": "success", "failed": "danger",
                                           "stopped": "info", "interrupted": "danger"})
    def status_badge(self, obj):
        return obj.status, obj.get_status_display()

    @display(description="Статус")
    def status_text(self, obj):
        color = {"done": "text-green-600", "failed": "text-red-600", "interrupted": "text-red-600",
                 "running": "text-primary-600", "queued": "text-orange-600"}.get(obj.status, "")
        return format_html('<span class="font-semibold {}">{}</span>', color, obj.get_status_display())

    @display(description="Длительность")
    def duration(self, obj):
        if not obj.started_at:
            return "-"
        sec = int(((obj.finished_at or timezone.now()) - obj.started_at).total_seconds())
        return f"{sec // 60} мин {sec % 60} с"

    @display(description="Лог")
    def log_view(self, obj):
        text = jobs.read_log(obj) or "(пока пусто)"
        refresh = ("<script>setTimeout(function(){location.reload()}, 4000);"
                   "window.addEventListener('load',function(){var p=document.getElementById('joblog');"
                   "if(p)p.scrollTop=p.scrollHeight;});</script>"
                   if obj.status in ("queued", "running") else "")
        return mark_safe(f'<pre id="joblog">{escape(text)}</pre>{refresh}')

    # быстрые кнопки над списком
    def _quick_job(self, request, **kw):
        job, busy = jobs.enqueue(**kw)
        messages.info(request, "Поставлено в очередь" if busy else "Запущено")
        return redirect(reverse("admin:screening_job_change", args=[job.pk]))

    @action(description="Полный цикл", icon="rocket_launch", variant="primary")
    def run_pipeline(self, request):
        return self._quick_job(request, command="pipeline")

    @action(description="Негативные новости РК", icon="campaign")
    def run_neg(self, request):
        return self._quick_job(request, command="neg-pipeline", days=7)

    @action(description="Собрать новости", icon="travel_explore")
    def run_collect(self, request):
        return self._quick_job(request, command="collect")

    @action(description="Только Telegram", icon="send")
    def run_telegram(self, request):
        return self._quick_job(request, command="collect", use_gdelt=False, use_gnews=False,
                               use_rss=False, use_telegram=True, days=7)

    @action(description="Докачать тексты", icon="download")
    def run_fetch(self, request):
        return self._quick_job(request, command="fetch-text")

    @action(description="Матчинг", icon="manage_search")
    def run_match(self, request):
        return self._quick_job(request, command="match")

    @action(description="Документы gov.kz", icon="folder_zip")
    def run_govkz(self, request):
        # нужна ссылка — открываем форму с уже выбранной командой
        return redirect(reverse("admin:screening_job_add") + "?command=govkz-docs")

    # кнопки на странице запуска: «Остановить» — пока идёт, «Запустить ещё раз» — когда закончился
    def _is_active(self, object_id) -> bool:
        return Job.objects.filter(pk=object_id, status__in=("queued", "running")).exists()

    def has_stop_job_permission(self, request, object_id=None):
        return self._is_active(object_id)

    def has_rerun_job_permission(self, request, object_id=None):
        return not self._is_active(object_id)

    @action(description="Остановить", icon="stop_circle", variant="danger",
            permissions=["stop_job"])
    def stop_job(self, request, object_id):
        job = Job.objects.get(pk=object_id)
        if jobs.stop(job):
            messages.warning(request, "Остановка запрошена")
        else:
            messages.info(request, "Запуск уже не выполняется")
        return redirect(reverse("admin:screening_job_change", args=[object_id]))

    @action(description="Запустить ещё раз", icon="replay", permissions=["rerun_job"])
    def rerun_job(self, request, object_id):
        old = Job.objects.get(pk=object_id)
        fields = {f.name: getattr(old, f.name) for f in Job._meta.fields
                  if f.name not in ("id", "status", "trigger", "created_at", "started_at",
                                    "finished_at", "returncode", "log_file")}
        return self._quick_job(request, **fields)


# --- Автозапуск -----------------------------------------------------------------
@admin.register(Schedule)
class ScheduleAdmin(ModelAdmin):
    fieldsets = (
        (None, {"fields": ("enabled", "every_hours", "command", "next_run_view")}),
        ("Сбор новостей", {"fields": ("use_gdelt", "use_gnews", "use_rss", "use_telegram",
                                      "days")}),
        ("История", {"fields": ("last_run_at", "last_job_link")}),
    )
    readonly_fields = ("next_run_view", "last_run_at", "last_job_link")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj, _ = Schedule.objects.get_or_create(pk=1)
        return redirect(reverse("admin:screening_schedule_change", args=[obj.pk]))

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context.update({"show_save_and_add_another": False, "show_save_and_view": False,
                        "show_delete": False})
        return super().render_change_form(request, context, add, change, form_url, obj)

    def response_change(self, request, obj):
        messages.success(request, "Сохранено. " + (
            "Автозапуск включён — работает, пока открыта панель." if obj.enabled
            else "Автозапуск выключен."))
        return redirect(reverse("admin:screening_schedule_change", args=[obj.pk]))

    @display(description="Следующий запуск")
    def next_run_view(self, obj):
        nxt = obj.next_run_at()
        if not nxt:
            return "выключен"
        if nxt <= timezone.now():
            return "в ближайшую минуту"
        return date_format(timezone.localtime(nxt), "j E Y, H:i")

    @display(description="Последний запуск")
    def last_job_link(self, obj):
        if not obj.last_job_id:
            return "-"
        url = reverse("admin:screening_job_change", args=[obj.last_job_id])
        return format_html('<a href="{}" class="text-primary-600 underline">#{} — {}</a>',
                           url, obj.last_job_id, obj.last_job.get_status_display())


# Telegram-каналы и аккаунт — в отдельном модуле, чтобы этот файл не разрастался
from . import admin_telegram  # noqa: E402,F401
# Негативные новости о Казахстане (регистрирует свои модели в admin.site)
from . import neg_admin  # noqa: E402,F401
