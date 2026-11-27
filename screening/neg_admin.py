"""Панель: негативные новости о Казахстане и их источники."""
import re
from datetime import timedelta

from django import forms
from django.contrib import admin, messages
from django.db.models import Q
from django.db.models.expressions import RawSQL
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import escape, format_html
from django.utils.http import content_disposition_header
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (ChoicesDropdownFilter, FieldTextFilter,
                                          RangeDateFilter, RangeNumericFilter)
from unfold.decorators import action, display

import config as aml_config
import export as aml_export

from . import jobs
from .admin import _filtered_queryset
from .models import NegNews, NegSource

NEG_CAT_SHORT = {
    "Срыв сроков / долгострой": "Сроки", "Удорожание / бюджет": "Бюджет",
    "Коррупция / хищения": "Коррупция", "Качество работ / нарушения": "Нарушения",
    "Аварии / ЧС / коммуналка": "Аварии / ЧС", "Недовольство / критика": "Критика",
    "Проверки / суды / наказания": "Суды / проверки", "Экология": "Экология",
    "Экономика / соцсфера": "Экономика",
}
STRONG = aml_config.NEG_STRONG_SCORE


def local_dt(dt, fmt="d.m.Y H:i"):
    return date_format(timezone.localtime(dt), fmt) if dt else ""


# --- Фильтры --------------------------------------------------------------------
class NegCategoryFilter(admin.SimpleListFilter):
    title = "Категория"
    parameter_name = "cat"

    def lookups(self, request, model_admin):
        return [(c, c) for c in aml_config.NEG_KEYWORDS]

    def queryset(self, request, qs):
        return qs.filter(categories__contains=self.value()) if self.value() else qs


class NegRegionFilter(admin.SimpleListFilter):
    title = "Регион"
    parameter_name = "region"

    def lookups(self, request, model_admin):
        return [(r, r) for r in aml_config.NEG_REGIONS] + [("-", "Регион не определён")]

    def queryset(self, request, qs):
        if self.value() == "-":
            return qs.filter(regions__isnull=True)
        return qs.filter(regions__contains=self.value()) if self.value() else qs


class NegScoreFilter(admin.SimpleListFilter):
    title = "Сила негатива"
    parameter_name = "strength"

    def lookups(self, request, model_admin):
        return [("strong", f"Сильные (балл ≥ {STRONG})"), ("mid", f"Средние (45–{STRONG - 1})"),
                ("weak", "Слабые (< 45)")]

    def queryset(self, request, qs):
        v = self.value()
        if v == "strong":
            return qs.filter(score__gte=STRONG)
        if v == "mid":
            return qs.filter(score__gte=45, score__lt=STRONG)
        if v == "weak":
            return qs.filter(score__lt=45)
        return qs


class NegPeriodFilter(admin.SimpleListFilter):
    title = "Период публикации"
    parameter_name = "period"

    def lookups(self, request, model_admin):
        return [("1", "За сутки"), ("7", "За 7 дней"), ("30", "За 30 дней"), ("90", "За 90 дней")]

    def queryset(self, request, qs):
        if self.value() in ("1", "7", "30", "90"):
            since = timezone.now() - timedelta(days=int(self.value()))
            return qs.filter(published__gte=since)
        return qs


class DateRangeFilter(RangeDateFilter):
    """Как RangeDateFilter, но включительно по дням для полей с датой и временем."""

    def queryset(self, request, queryset):
        f = {}
        a = self.used_parameters.get(f"{self.parameter_name}_from")
        b = self.used_parameters.get(f"{self.parameter_name}_to")
        try:
            if a:
                f[f"{self.parameter_name}__date__gte"] = forms.DateField().clean(a)
            if b:
                f[f"{self.parameter_name}__date__lte"] = forms.DateField().clean(b)
        except forms.ValidationError:
            return None
        return queryset.filter(**f)


# --- Форма ручного добавления -------------------------------------------------------
class NegNewsAddForm(forms.ModelForm):
    class Meta:
        model = NegNews
        fields = ("url", "title", "source", "published", "text", "review_status", "review_note")
        labels = {"text": "Текст (если ссылки нет или сайт не открывается)"}
        help_texts = {"url": "Если указать ссылку, название, издание, дату и текст панель "
                             "попробует взять со страницы сама"}

    def clean_url(self):
        from negative import canonical_url
        url = canonical_url(self.cleaned_data.get("url") or None)
        if url and NegNews.objects.filter(url=url).exists():
            raise forms.ValidationError("Эта новость уже есть в базе")
        return url

    def clean(self):
        data = super().clean()
        if not (data.get("url") or data.get("text")):
            raise forms.ValidationError("Укажите ссылку или вставьте текст новости")
        return data


def _rows_for_export(qs) -> list[dict]:
    fields = list(aml_export.NEG_COLUMNS) + [f for f in aml_export.NEG_JSON_FIELDS
                                             if f not in aml_export.NEG_COLUMNS]
    return list(qs.values(*fields))


def neg_xlsx_response(qs, filename="negative_news.xlsx", with_text=True):
    data = aml_export.neg_xlsx_bytes(_rows_for_export(qs), with_text)
    resp = HttpResponse(data, content_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    resp["Content-Disposition"] = content_disposition_header(True, filename)
    return resp


def neg_json_response(qs, filename="negative_news.json", with_text=True):
    fields = [f for f in aml_export.NEG_JSON_FIELDS if with_text or f != "text"]
    items = list(qs.values(*fields))
    resp = HttpResponse(
        aml_export.json_dumps({"exported_at": timezone.now(), "count": len(items),
                               "items": items}, indent=2),
        content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = content_disposition_header(True, filename)
    return resp


def _stamp(prefix):
    return f"{prefix}_{timezone.localtime():%Y-%m-%d_%H%M}"


def _highlight_words(text: str | None, words: list[str]):
    """Текст с подсвеченными найденными словами (подсветка — по началу слова)."""
    if not text:
        return "-"
    html = escape(text)
    # слова хранятся так, как написаны в тексте (в нижнем регистре)
    phrases = sorted({escape(w) for w in words if len(w) >= 3}, key=len, reverse=True)
    if phrases:
        rx = re.compile(r"(?<![\w])(" + "|".join(
            r"\s+".join(re.escape(p) for p in ph.split()) for ph in phrases) + r")(?![\w])",
            re.I | re.U)
        html = rx.sub(lambda m: f'<mark class="neg-mark">{m.group(0)}</mark>', html)
    html = html.replace("\n", "<br>")
    return mark_safe(f'<div class="neg-text">{html}</div>')


# --- Новости ----------------------------------------------------------------------
@admin.register(NegNews)
class NegNewsAdmin(ModelAdmin):
    list_display = ("published_cell", "news_cell", "score_badge", "cat_badges", "region_cell",
                    "status_badge")
    list_display_links = None
    list_filter = (("review_status", ChoicesDropdownFilter), NegScoreFilter, NegPeriodFilter,
                   NegCategoryFilter, NegRegionFilter, ("collector", ChoicesDropdownFilter),
                   ("source", FieldTextFilter), ("published", DateRangeFilter),
                   ("score", RangeNumericFilter))
    list_filter_submit = True
    search_fields = ("title",)        # сам поиск — полнотекстовый, см. get_search_results
    search_help_text = ("Поиск по названию и тексту (с учётом окончаний). Фраза — в кавычках, "
                        "«или» — OR, исключить слово — минус: \"Бахты Аягоз\" -тендер")
    ordering = ("-published", "-id")
    list_per_page = 50
    actions = ["mark_relevant", "mark_irrelevant", "mark_new", "export_xlsx", "export_json"]
    actions_list = ["export_filtered", "export_filtered_json", "collect_now"]
    actions_row = ["row_relevant", "row_irrelevant", "row_open"]
    actions_detail = ["detail_relevant", "detail_irrelevant", "detail_open"]

    readonly_fields = ("title", "link_view", "source", "published_view", "score", "categories",
                       "keywords", "regions", "kz_signal", "copies_view", "collector_view",
                       "text_view", "reviewed_view")

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return ((None, {"fields": ("url", "title", "source", "published", "text")}),
                    ("Проверка", {"fields": ("review_status", "review_note")}))
        return (
            ("Новость", {"fields": ("title", "link_view", "source", "published_view",
                                    "collector_view", "copies_view")}),
            ("Разметка", {"fields": ("score", "categories", "regions", "keywords",
                                     "kz_signal")}),
            ("Проверка", {"fields": ("review_status", "review_note", "reviewed_view")}),
            ("Текст", {"fields": ("text_view",)}),
        )

    def get_form(self, request, obj=None, change=False, **kwargs):
        if obj is None:
            kwargs["form"] = NegNewsAddForm
        return super().get_form(request, obj, change, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        return () if obj is None else self.readonly_fields

    def get_search_results(self, request, queryset, search_term):
        term = (search_term or "").strip()
        if not term:
            return queryset, False
        fts = RawSQL("SELECT id FROM neg_news WHERE search @@ websearch_to_tsquery('russian', %s)",
                     [term])
        return queryset.filter(Q(id__in=fts) | Q(title__icontains=term) |
                               Q(source__icontains=term) | Q(url__icontains=term)), False

    def save_model(self, request, obj, form, change):
        if not change:
            self._create_manual(request, obj, form)
            return
        # меняется только проверка: остальное могло обновиться запуском, пока форма была открыта
        changed = {"review_status", "review_note"} & set(form.changed_data)
        if not changed:
            return
        obj.reviewed_by, obj.reviewed_at = request.user.get_username(), timezone.now()
        obj.save(update_fields=list(changed) + ["reviewed_by", "reviewed_at"])

    def _create_manual(self, request, obj, form):
        from negative import (analyze, domain_of, fetch_page, is_kz_domain, make_title,
                              source_name, title_key)
        meta, status = {}, ("ok" if obj.text else None)
        if obj.url and not (obj.text and obj.title):
            text, status, meta = fetch_page(obj.url, True)
            obj.text = obj.text or text
            if not text and not obj.text:
                messages.warning(request, f"Страницу не удалось прочитать ({status}) — "
                                          "новость сохранена без текста")
        domain = domain_of(obj.url)
        obj.title = obj.title or meta.get("title") or (make_title(obj.text) if obj.text else obj.url)
        obj.source = obj.source or source_name(domain, meta.get("sitename"))
        obj.published = obj.published or meta.get("date")
        obj.source_domain = domain
        obj.collector = "manual"
        obj.text_status = status or ("ok" if obj.text else "empty")
        obj.kz_outlet = is_kz_domain(domain)
        obj.title_key = title_key(obj.title)
        v = analyze(obj.title, None, obj.text, kz_outlet=obj.kz_outlet)
        for k, val in v.as_row().items():
            setattr(obj, k, val)
        obj.analyzed_at = obj.collected_at = timezone.now()
        if obj.review_status != "new":
            obj.reviewed_by, obj.reviewed_at = request.user.get_username(), timezone.now()
        obj.save()

    def get_changeform_initial_data(self, request):
        return {"review_status": "relevant"}

    # --- колонки
    @display(description="Дата", ordering="published")
    def published_cell(self, obj):
        return format_html('<span style="white-space:nowrap">{}</span><div class="text-subtle '
                           'text-[11px]">{}</div>', local_dt(obj.published, "d.m.Y"),
                           local_dt(obj.published, "H:i"))

    @display(description="Новость", ordering="title")
    def news_cell(self, obj):
        t = obj.title or obj.url or "(без названия)"
        t = t[:140] + "…" if len(t) > 140 else t
        url = reverse("admin:screening_negnews_change", args=[obj.pk])
        meta = [obj.source or obj.source_domain or ""]
        if obj.copies > 1:
            meta.append(f"ещё в {obj.copies - 1} изд.")
        meta.append(obj.get_collector_display())
        ext = (format_html(' · <a href="{}" target="_blank" rel="noopener" '
                           'class="text-primary-600 hover:underline">оригинал ↗</a>', obj.url)
               if obj.url else "")
        return format_html('<div style="min-width:320px;max-width:560px"><a href="{}" '
                           'class="font-medium text-important hover:underline">{}</a>'
                           '<div class="text-subtle text-xs mt-0.5">{}{}</div></div>',
                           url, t, " · ".join(m for m in meta if m), ext)

    @display(description="Балл", ordering="score",
             label={"danger": "danger", "warning": "warning", "info": "info"})
    def score_badge(self, obj):
        kind = "danger" if obj.score >= STRONG else "warning" if obj.score >= 45 else "info"
        return kind, str(obj.score)

    @display(description="Категории", label=True)
    def cat_badges(self, obj):
        return [NEG_CAT_SHORT.get(c, c) for c in obj.category_list[:3]] or "-"

    @display(description="Регион")
    def region_cell(self, obj):
        regs = obj.region_list
        if not regs:
            return "-"
        more = f" +{len(regs) - 2}" if len(regs) > 2 else ""
        return format_html('<span title="{}">{}{}</span>', ", ".join(regs),
                           ", ".join(regs[:2]), more)

    @display(description="Статус", ordering="review_status",
             label={"new": "warning", "relevant": "danger", "irrelevant": "info"})
    def status_badge(self, obj):
        return obj.review_status, obj.get_review_status_display()

    # --- поля карточки
    @display(description="Ссылка")
    def link_view(self, obj):
        parts = []
        if obj.url:
            parts.append(format_html('<a href="{}" target="_blank" rel="noopener" '
                                     'class="text-primary-600 underline">{}</a>', obj.url, obj.url))
        if obj.post_url and obj.post_url != obj.url:
            parts.append(format_html('<a href="{}" target="_blank" rel="noopener" '
                                     'class="text-primary-600 underline">пост в Telegram</a>',
                                     obj.post_url))
        return mark_safe("<br>".join(parts)) if parts else "-"

    @display(description="Дата публикации")
    def published_view(self, obj):
        return local_dt(obj.published) or "неизвестна"

    @display(description="Откуда собрано")
    def collector_view(self, obj):
        q = f" — {obj.query}" if obj.query else ""
        return f"{obj.get_collector_display()}{q}; собрано {local_dt(obj.collected_at)}"

    @display(description="Перепечатки")
    def copies_view(self, obj):
        if obj.copies <= 1:
            return "только в этом издании"
        also = f": {obj.also_in}" if obj.also_in else ""
        return f"сюжет встретился {obj.copies} раз(а){also}"

    @display(description="Текст")
    def text_view(self, obj):
        words = [w.strip() for w in (obj.keywords or "").split(";") if w.strip()]
        parts = []
        # описание часто — просто начало текста статьи; тогда второй раз не показываем
        summary = (obj.summary or "").strip()
        if summary and summary[:120].rstrip(".… ") not in (obj.text or ""):
            parts.append(_highlight_words(summary, words))
        if obj.text:
            parts.append(_highlight_words(obj.text, words))
        if not parts:
            status = {"skipped": "у Google News полный текст недоступен",
                      "empty": "на странице не нашлось текста"}.get(obj.text_status or "",
                                                                    obj.text_status or "ещё не скачан")
            return f"Текста нет ({status})"
        return mark_safe('<hr class="my-3">'.join(parts))

    @display(description="Проверено")
    def reviewed_view(self, obj):
        if not obj.reviewed_at:
            return "-"
        return f"{obj.reviewed_by or '—'}, {local_dt(obj.reviewed_at)}"

    # --- массовые действия
    @staticmethod
    def _set_status(request, qs, status) -> int:
        return qs.update(review_status=status, reviewed_by=request.user.get_username(),
                         reviewed_at=timezone.now())

    def _set(self, request, qs, status):
        n = self._set_status(request, qs, status)
        self.message_user(request, f"Обновлено: {n}", messages.SUCCESS)

    @admin.action(description="Отметить: актуально")
    def mark_relevant(self, request, qs):
        self._set(request, qs, "relevant")

    @admin.action(description="Отметить: не актуально")
    def mark_irrelevant(self, request, qs):
        self._set(request, qs, "irrelevant")

    @admin.action(description="Сбросить статус проверки")
    def mark_new(self, request, qs):
        n = qs.update(review_status="new", reviewed_by=None, reviewed_at=None)
        self.message_user(request, f"Обновлено: {n}", messages.SUCCESS)

    @admin.action(description="Выгрузить выбранные в Excel")
    def export_xlsx(self, request, qs):
        return neg_xlsx_response(qs.order_by("-published", "-id"), _stamp("negative") + ".xlsx")

    @admin.action(description="Выгрузить выбранные в JSON")
    def export_json(self, request, qs):
        return neg_json_response(qs.order_by("-published", "-id"), _stamp("negative") + ".json")

    @action(description="Excel (по фильтрам)", icon="download")
    def export_filtered(self, request):
        return neg_xlsx_response(_filtered_queryset(self, request), _stamp("negative") + ".xlsx")

    @action(description="JSON (по фильтрам)", icon="data_object")
    def export_filtered_json(self, request):
        return neg_json_response(_filtered_queryset(self, request), _stamp("negative") + ".json")

    @action(description="Собрать сейчас", icon="travel_explore", variant="primary")
    def collect_now(self, request):
        job, busy = jobs.enqueue(command="neg-pipeline", days=7)
        messages.info(request, "Сбор поставлен в очередь" if busy else "Сбор запущен")
        return redirect(reverse("admin:screening_job_change", args=[job.pk]))

    # --- кнопки в строке и в карточке
    def _quick(self, request, object_id, status):
        self._set_status(request, NegNews.objects.filter(pk=object_id), status)

    def _row(self, request, object_id, status):
        self._quick(request, object_id, status)
        return redirect(request.META.get("HTTP_REFERER") or
                        reverse("admin:screening_negnews_changelist"))

    @action(description="Актуально", icon="flag")
    def row_relevant(self, request, object_id):
        return self._row(request, object_id, "relevant")

    @action(description="Не актуально", icon="block")
    def row_irrelevant(self, request, object_id):
        return self._row(request, object_id, "irrelevant")

    @action(description="Открыть оригинал", icon="open_in_new")
    def row_open(self, request, object_id):
        obj = NegNews.objects.get(pk=object_id)
        return redirect(obj.url or obj.post_url or
                        reverse("admin:screening_negnews_change", args=[object_id]))

    @action(description="Актуально", icon="flag", variant="danger")
    def detail_relevant(self, request, object_id):
        self._quick(request, object_id, "relevant")
        messages.success(request, "Отмечено как актуальное")
        return redirect(reverse("admin:screening_negnews_change", args=[object_id]))

    @action(description="Не актуально", icon="block")
    def detail_irrelevant(self, request, object_id):
        self._quick(request, object_id, "irrelevant")
        messages.success(request, "Отмечено как неактуальное")
        return redirect(reverse("admin:screening_negnews_change", args=[object_id]))

    @action(description="Открыть оригинал", icon="open_in_new")
    def detail_open(self, request, object_id):
        return self.row_open(request, object_id)


# --- Источники ----------------------------------------------------------------------
@admin.register(NegSource)
class NegSourceAdmin(ModelAdmin):
    list_display = ("name", "kind_badge", "handle_link", "enabled_badge", "health",
                    "last_item_cell", "last_kept", "total_kept")
    list_display_links = ("name",)
    list_filter = (("kind", ChoicesDropdownFilter), "enabled", "kz")
    search_fields = ("name", "handle", "note")
    ordering = ("kind", "name")
    list_per_page = 100
    actions = ["enable", "disable"]
    actions_detail = ["read_now"]
    fieldsets = (
        (None, {"fields": ("kind", "name", "handle", "enabled", "kz", "region", "note")}),
        ("Статистика", {"fields": ("last_run_at", "last_status", "last_found", "last_kept",
                                   "last_item_at", "total_kept", "news_link")}),
    )
    readonly_fields = ("last_run_at", "last_status", "last_found", "last_kept", "last_item_at",
                       "total_kept", "news_link")

    def get_fieldsets(self, request, obj=None):
        return self.fieldsets[:1] if obj is None else self.fieldsets

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in ("name", "handle", "region"):
            kwargs["widget"] = forms.TextInput(attrs={"size": 60})
        if db_field.name == "region":
            choices = [("", "—")] + [(r, r) for r in aml_config.NEG_REGIONS]
            return forms.ChoiceField(label="Регион издания", choices=choices, required=False,
                                     help_text=db_field.help_text,
                                     widget=forms.Select(attrs={"class": "unfold-select"}))
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        obj.region = obj.region or None
        super().save_model(request, obj, form, change)

    @display(description="Тип", label={"telegram": "primary", "rss": "info"})
    def kind_badge(self, obj):
        return obj.kind, "Telegram" if obj.kind == "telegram" else "RSS"

    @display(description="Адрес")
    def handle_link(self, obj):
        url = f"https://t.me/{obj.handle}" if obj.kind == "telegram" else obj.handle
        text = "@" + obj.handle if obj.kind == "telegram" else obj.handle
        text = text if len(text) <= 50 else text[:50] + "…"
        return format_html('<a href="{}" target="_blank" rel="noopener" '
                           'class="text-primary-600 hover:underline">{}</a>', url, text)

    @display(description="Вкл.", label={"Да": "success", "Нет": "info"})
    def enabled_badge(self, obj):
        return "Да" if obj.enabled else "Нет"

    @display(description="Последнее чтение")
    def health(self, obj):
        if not obj.last_run_at:
            return format_html('<span class="text-subtle">ещё не читали</span>')
        when = local_dt(obj.last_run_at)
        if (obj.last_status or "").startswith("error"):
            return format_html('<span class="text-red-600" title="{}">ошибка</span>'
                               '<div class="text-subtle text-[11px]">{}</div>',
                               obj.last_status, when)
        return format_html('<span class="text-green-700">ок</span> · {} зап.'
                           '<div class="text-subtle text-[11px]">{}</div>',
                           obj.last_found or 0, when)

    @display(description="Последняя публикация", ordering="last_item_at")
    def last_item_cell(self, obj):
        if not obj.last_item_at:
            return "-"
        stale = obj.last_item_at < timezone.now() - timedelta(days=14)
        return format_html('<span class="{}" title="{}">{}</span>',
                           "text-orange-600" if stale else "",
                           "Давно не обновлялся — возможно, канал/лента заброшены" if stale else "",
                           local_dt(obj.last_item_at, "d.m.Y"))

    @display(description="Новости из источника")
    def news_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse("admin:screening_negnews_changelist") + f"?source_ref__id__exact={obj.pk}"
        return format_html('<a href="{}" class="text-primary-600 underline">Показать</a>', url)

    @admin.action(description="Включить")
    def enable(self, request, qs):
        self.message_user(request, f"Включено: {qs.update(enabled=True)}", messages.SUCCESS)

    @admin.action(description="Выключить")
    def disable(self, request, qs):
        self.message_user(request, f"Выключено: {qs.update(enabled=False)}", messages.SUCCESS)

    @action(description="Прочитать сейчас", icon="sync")
    def read_now(self, request, object_id):
        src = NegSource.objects.get(pk=object_id)
        job, _ = jobs.enqueue(command="neg-pipeline", days=7, neg_source_id=src.pk,
                              use_telegram=src.kind == "telegram", use_rss=src.kind == "rss",
                              use_gnews=False, use_gdelt=False)
        messages.info(request, f"Чтение источника «{src.name}» поставлено в очередь")
        return redirect(reverse("admin:screening_job_change", args=[job.pk]))
