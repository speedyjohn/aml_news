"""Админка Telegram: список каналов (с загрузкой файлом) и подключение аккаунта-юзербота."""
import os
from urllib.parse import urlencode

import pandas as pd
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from unfold.widgets import (UnfoldAdminFileFieldWidget, UnfoldAdminPasswordWidget,
                            UnfoldAdminSelectWidget, UnfoldAdminTextareaWidget,
                            UnfoldAdminTextInputWidget, UnfoldBooleanSwitchWidget)

import tg_collect as tg

from . import jobs
from .admin import _ext_link, _xlsx_response
from .models import Job, TgAccount, TgChannel

MAX_PROBE = 30          # сколько каналов за раз проверять кнопкой «Проверить»


def _dt(value):
    return date_format(timezone.localtime(value), "j E Y, H:i") if value else "-"


def _apply_ref(obj: TgChannel, r: tg.ChannelRef):
    obj.ref, obj.username, obj.invite_hash = r.ref, r.username, r.invite_hash
    if r.channel_id:
        obj.channel_id = r.channel_id


def get_account() -> TgAccount:
    acc, _ = TgAccount.objects.get_or_create(pk=1)
    return acc


# --- Каналы ---------------------------------------------------------------------
class TgChannelForm(forms.ModelForm):
    class Meta:
        model = TgChannel
        fields = ("ref", "title", "method", "is_active", "note")

    def clean_ref(self):
        r = tg.parse_channel_ref(self.cleaned_data["ref"])
        if not r:
            raise ValidationError("Не похоже на канал. Примеры: @tengrinews, "
                                  "https://t.me/tengrinews, https://t.me/+AbCdEf123, "
                                  "https://t.me/c/1234567890/5")
        self._parsed = r
        return r.ref

    def clean(self):
        data = super().clean()
        r = getattr(self, "_parsed", None)
        if r:
            _apply_ref(self.instance, r)
            if r.is_private and data.get("method") == "web":
                self.add_error("method", "Закрытый канал читается только через аккаунт")
        return data


class ChannelTypeFilter(admin.SimpleListFilter):
    title = "Тип"
    parameter_name = "type"

    def lookups(self, request, model_admin):
        return [("public", "Открытые"), ("private", "Закрытые")]

    def queryset(self, request, qs):
        if self.value() == "public":
            return qs.exclude(username="")
        if self.value() == "private":
            return qs.filter(username="")
        return qs


class ChannelErrorFilter(admin.SimpleListFilter):
    title = "Последний сбор"
    parameter_name = "health"

    def lookups(self, request, model_admin):
        return [("error", "С ошибкой"), ("ok", "Без ошибок"), ("never", "Ещё не собирался")]

    def queryset(self, request, qs):
        if self.value() == "error":
            return qs.exclude(last_error="")
        if self.value() == "ok":
            return qs.filter(last_error="", last_checked_at__isnull=False)
        if self.value() == "never":
            return qs.filter(last_checked_at__isnull=True)
        return qs


class UploadForm(forms.Form):
    file = forms.FileField(
        label="Файл со списком", required=False, widget=UnfoldAdminFileFieldWidget,
        help_text="txt, csv или xlsx. Каналы ищутся во всех ячейках: @username, t.me/…, "
                  "инвайт-ссылки t.me/+… (в csv/xlsx — только с @ или ссылкой, чтобы "
                  "названия из соседних колонок не стали каналами)")
    text = forms.CharField(
        label="…или вставьте список", required=False,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 8, "placeholder":
                                                "@tengrinews\nhttps://t.me/zakonkz\n"
                                                "https://t.me/+AbCdEfGh123"}))
    method = forms.ChoiceField(label="Как читать", choices=TgChannel.METHODS, initial="auto",
                               widget=UnfoldAdminSelectWidget)
    is_active = forms.BooleanField(label="Сразу включить сбор", required=False, initial=True,
                                   widget=UnfoldBooleanSwitchWidget)

    def clean(self):
        data = super().clean()
        if not data.get("file") and not (data.get("text") or "").strip():
            raise ValidationError("Приложите файл или вставьте список каналов")
        return data


def _file_text(f) -> str:
    """Весь текст из загруженного файла: строки txt или все ячейки csv/xlsx."""
    ext = os.path.splitext(f.name)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(f, header=None, sheet_name=None, dtype=str)
        frames = list(sheets.values())
    elif ext == ".csv":
        raw = f.read()
        import io
        for enc in ("utf-8-sig", "cp1251"):
            try:
                frames = [pd.read_csv(io.StringIO(raw.decode(enc)), header=None, dtype=str,
                                      sep=None, engine="python")]
                break
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        else:
            raise ValidationError("Не удалось прочитать CSV")
    else:
        raw = f.read()
        for enc in ("utf-8-sig", "cp1251"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        raise ValidationError("Не удалось прочитать файл — сохраните его в UTF-8")
    cells = []
    for df in frames:
        cells += [str(v) for v in df.to_numpy().ravel() if isinstance(v, str) and v.strip()]
    return "\n".join(cells)


@admin.register(TgChannel)
class TgChannelAdmin(ModelAdmin):
    form = TgChannelForm
    list_display = ("channel_cell", "type_badge", "method_short", "is_active", "last_post_cell",
                    "posts_saved", "state_cell")
    list_display_links = ("channel_cell",)
    list_filter = ("is_active", ChannelTypeFilter, "method", ChannelErrorFilter)
    search_fields = ("ref", "username", "title", "note")
    search_help_text = "Поиск по имени канала, названию и заметке"
    list_per_page = 100
    actions = ["enable", "disable", "probe", "clear_errors"]
    actions_list = ["upload_list", "collect_now", "export_list"]
    actions_detail = ["probe_one", "open_channel"]

    def get_fieldsets(self, request, obj=None):
        main = (None, {"fields": ("ref", "title", "method", "is_active", "note")})
        if obj is None:
            return (main,)
        return (main, ("Сбор", {"fields": ("type_view", "link_view", "channel_id",
                                           "last_post_view", "last_checked_at", "last_via",
                                           "posts_saved", "last_error", "articles_link")}))

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return ("type_view", "link_view", "channel_id", "last_post_view", "last_checked_at",
                "last_via", "posts_saved", "last_error", "articles_link")

    # колонки
    @display(description="Канал", ordering="title")
    def channel_cell(self, obj):
        return format_html('<div style="min-width:200px"><span class="font-medium">{}</span>'
                           '<div class="text-subtle text-xs mt-0.5">{}{}</div></div>',
                           obj.title or obj.display_ref, obj.display_ref,
                           f" · {obj.note}" if obj.note else "")

    @display(description="Тип", label={"Открытый": "info", "Закрытый": "warning"})
    def type_badge(self, obj):
        return "Закрытый" if obj.is_private else "Открытый"

    @display(description="Как читать", ordering="method")
    def method_short(self, obj):
        return {"auto": "авто", "web": "веб-версия", "account": "аккаунт"}.get(obj.method,
                                                                             obj.method)

    @display(description="Последний пост", ordering="last_post_at")
    def last_post_cell(self, obj):
        if not obj.last_post_at:
            return "-"
        return timezone.localtime(obj.last_post_at).strftime("%d.%m.%Y %H:%M")

    @display(description="Состояние", ordering="last_checked_at")
    def state_cell(self, obj):
        if obj.last_error:
            text = obj.last_error if len(obj.last_error) < 90 else obj.last_error[:90] + "…"
            return format_html('<span class="text-red-600" title="{}">{}</span>',
                               obj.last_error, text)
        if not obj.last_checked_at:
            return format_html('<span class="text-subtle">ещё не собирался</span>')
        via = {"web": "веб", "account": "аккаунт"}.get(obj.last_via, obj.last_via)
        return format_html('<span class="text-green-600">ок</span>'
                           '<span class="text-subtle text-xs"> · {} · {}</span>',
                           via, timezone.localtime(obj.last_checked_at).strftime("%d.%m %H:%M"))

    @display(description="Тип")
    def type_view(self, obj):
        return ("Закрытый — читается только через подключённый аккаунт" if obj.is_private
                else "Открытый")

    @display(description="Ссылка")
    def link_view(self, obj):
        return _ext_link(obj.link)

    @display(description="Последний пост")
    def last_post_view(self, obj):
        if not obj.last_post_id:
            return "-"
        url = tg.post_url(obj.username or None, obj.channel_id, obj.last_post_id)
        return format_html('{} — {}', _ext_link(url, f"#{obj.last_post_id}"),
                           _dt(obj.last_post_at))

    @display(description="Посты в базе")
    def articles_link(self, obj):
        url = (reverse("admin:screening_article_changelist") + "?"
               + urlencode({"collector": "telegram", "query": obj.ref}))
        return format_html('<a href="{}" class="text-primary-600 underline">Показать</a>', url)

    # массовые действия
    @admin.action(description="Включить сбор")
    def enable(self, request, qs):
        self.message_user(request, f"Включено: {qs.update(is_active=True)}", messages.SUCCESS)

    @admin.action(description="Выключить сбор")
    def disable(self, request, qs):
        self.message_user(request, f"Выключено: {qs.update(is_active=False)}", messages.SUCCESS)

    @admin.action(description="Сбросить ошибки")
    def clear_errors(self, request, qs):
        self.message_user(request, f"Сброшено: {qs.update(last_error='')}", messages.SUCCESS)

    @admin.action(description=f"Проверить доступ (до {MAX_PROBE} каналов)")
    def probe(self, request, qs):
        self._probe(request, list(qs[:MAX_PROBE]))

    def _probe(self, request, channels: list[TgChannel]):
        """Пробует открыть каналы прямо сейчас и сохраняет результат."""
        acc = get_account()
        connected = acc.status == "connected" and acc.session
        via_acc, ok, bad = [], 0, []

        def done(ch, info, via):
            ch.title = info.get("title") or ch.title
            if info.get("channel_id"):
                ch.channel_id = info["channel_id"]
            if info.get("last_post_id"):
                ch.last_post_id, ch.last_post_at = info["last_post_id"], info["last_post_at"]
            ch.last_error, ch.last_via, ch.last_checked_at = "", via, timezone.now()
            ch.save()

        def fail(ch, err):
            ch.last_error, ch.last_checked_at = err, timezone.now()
            ch.save(update_fields=["last_error", "last_checked_at"])
            bad.append(f"{ch.display_ref}: {err}")

        for ch in channels:
            if ch.is_private or ch.method == "account":
                via_acc.append(ch)
                continue
            try:
                done(ch, tg.probe_web(ch.username), "web")
                ok += 1
            except tg.NoWebPreview as e:
                if ch.method == "auto" and connected:
                    via_acc.append(ch)
                else:
                    fail(ch, str(e) + ("" if connected else
                                       ". Подключите аккаунт, чтобы читать такие каналы."))
            except Exception as e:
                fail(ch, f"{type(e).__name__}: {e}")

        if via_acc:
            if not connected:
                for ch in via_acc:
                    fail(ch, "нужен подключённый Telegram-аккаунт")
            elif not tg.telethon_available():
                messages.error(request, "Не установлен telethon: pip install -r requirements.txt")
            else:
                rows = [{"ref": c.ref, "username": c.username, "invite_hash": c.invite_hash,
                         "channel_id": c.channel_id, "title": c.title} for c in via_acc]
                try:
                    res = tg.run(tg.probe_account(acc.api_id, acc.api_hash, acc.session,
                                                  acc.auto_join, rows))
                except Exception as e:
                    res = [e] * len(via_acc)
                for ch, r in zip(via_acc, res):
                    if isinstance(r, Exception):
                        fail(ch, tg.error_text(r))
                    else:
                        done(ch, r, "account")
                        ok += 1

        if ok:
            messages.success(request, f"Доступны: {ok}")
        for line in bad[:15]:
            messages.error(request, line)
        if len(bad) > 15:
            messages.error(request, f"…и ещё {len(bad) - 15} с ошибками (см. колонку «Состояние»)")

    # кнопки над списком
    @action(description="Загрузить список", icon="upload_file", variant="primary")
    def upload_list(self, request):
        return redirect(reverse("admin:screening_tgchannel_upload"))

    @action(description="Собрать сейчас", icon="send")
    def collect_now(self, request):
        if not TgChannel.objects.filter(is_active=True).exists():
            messages.warning(request, "Нет включённых каналов")
            return redirect(reverse("admin:screening_tgchannel_changelist"))
        job = Job.objects.create(command="collect", use_gdelt=False, use_gnews=False,
                                 use_rss=False, use_telegram=True, days=7)
        busy = jobs.is_busy()
        transaction.on_commit(lambda: jobs.submit(job))
        messages.info(request, "Сбор за 7 дней поставлен в очередь" if busy
                      else "Сбор за 7 дней запущен")
        return redirect(reverse("admin:screening_job_change", args=[job.pk]))

    @action(description="Выгрузить список", icon="download")
    def export_list(self, request):
        qs = TgChannel.objects.all().order_by("ref")
        df = pd.DataFrame([{
            "Канал": c.display_ref, "Ссылка": c.link, "Название": c.title,
            "Тип": "закрытый" if c.is_private else "открытый",
            "Как читать": c.get_method_display(), "Собирать": "да" if c.is_active else "нет",
            "Сохранено постов": c.posts_saved, "Ошибка": c.last_error, "Заметка": c.note,
        } for c in qs])
        return _xlsx_response(df, "telegram_channels.xlsx")

    # кнопки на странице канала
    @action(description="Проверить доступ", icon="wifi_tethering")
    def probe_one(self, request, object_id):
        self._probe(request, [TgChannel.objects.get(pk=object_id)])
        return redirect(reverse("admin:screening_tgchannel_change", args=[object_id]))

    @action(description="Открыть в Telegram", icon="open_in_new")
    def open_channel(self, request, object_id):
        return redirect(TgChannel.objects.get(pk=object_id).link)

    # загрузка списка
    def get_urls(self):
        return [path("upload/", self.admin_site.admin_view(self.upload_view),
                     name="screening_tgchannel_upload")] + super().get_urls()

    def upload_view(self, request):
        result = None
        if request.method == "POST":
            form = UploadForm(request.POST, request.FILES)
            if form.is_valid():
                text, table_text = form.cleaned_data.get("text") or "", ""
                try:
                    f = form.cleaned_data.get("file")
                    if f:
                        content = _file_text(f)
                        if os.path.splitext(f.name)[1].lower() in (".csv", ".xlsx", ".xlsm",
                                                                   ".xls"):
                            table_text = content
                        else:
                            text += "\n" + content
                except ValidationError as e:
                    form.add_error("file", e)
                except Exception as e:
                    form.add_error("file", f"Не удалось прочитать файл: {e}")
                if form.is_valid():
                    result = self._import(text, form.cleaned_data["method"],
                                          form.cleaned_data["is_active"], table_text)
                    if result["added"]:
                        messages.success(request, f"Добавлено каналов: {len(result['added'])}")
                    form = UploadForm(initial={"method": form.cleaned_data["method"],
                                               "is_active": form.cleaned_data["is_active"]})
        else:
            form = UploadForm()
        context = {
            **self.admin_site.each_context(request),
            "opts": TgChannel._meta, "title": "Загрузить список Telegram-каналов",
            "form": form, "result": result,
            "list_url": reverse("admin:screening_tgchannel_changelist"),
            "account_url": reverse("admin:screening_tgaccount_changelist"),
        }
        return TemplateResponse(request, "admin/screening/tgchannel/upload.html", context)

    @staticmethod
    def _import(text: str, method: str, is_active: bool, table_text: str = "") -> dict:
        refs, bad = tg.extract_refs(text)
        if table_text:
            # в таблицах голые слова — это названия, а не каналы
            t_refs, t_bad = tg.extract_refs(table_text, allow_bare=False)
            seen = {r.ref for r in refs}
            refs += [r for r in t_refs if r.ref not in seen]
            bad += t_bad
        existing = set(TgChannel.objects.filter(ref__in=[r.ref for r in refs])
                       .values_list("ref", flat=True))
        added, skipped = [], []
        for r in refs:
            if r.ref in existing:
                skipped.append(r.ref)
                continue
            ch = TgChannel(method="account" if r.is_private and method == "web" else method,
                           is_active=is_active)
            _apply_ref(ch, r)
            ch.save()
            added.append(ch.display_ref)
        return {"added": added, "skipped": skipped,
                # мусор вроде заголовков колонок не показываем целиком
                "bad": [b for b in bad if len(b) < 120][:50], "bad_total": len(bad),
                "private": sum(1 for r in refs if r.is_private and r.ref not in existing)}


# --- Аккаунт --------------------------------------------------------------------
class TgAccountForm(forms.ModelForm):
    code = forms.CharField(label="Код из Telegram", required=False, max_length=10,
                           widget=UnfoldAdminTextInputWidget(attrs={"autocomplete": "off"}),
                           help_text="Пришёл в чат «Telegram» в приложении. Введите и нажмите "
                                     "«Сохранить»")
    password = forms.CharField(label="Пароль двухэтапной проверки", required=False,
                               widget=UnfoldAdminPasswordWidget(),
                               help_text="Облачный пароль аккаунта. Нигде не сохраняется")

    class Meta:
        model = TgAccount
        fields = ("api_id", "api_hash", "phone", "auto_join")

    def clean_phone(self):
        p = "".join(ch for ch in self.cleaned_data.get("phone", "") if ch.isdigit() or ch == "+")
        if p and not p.startswith("+"):
            p = "+" + p
        return p

    def clean_api_hash(self):
        return (self.cleaned_data.get("api_hash") or "").strip()


HELP_HTML = """
<ol style="list-style:decimal;padding-left:18px;line-height:1.7">
  <li>Откройте <a href="https://my.telegram.org/apps" target="_blank" rel="noopener"
      class="text-primary-600 underline">my.telegram.org</a>, войдите по номеру телефона,
      раздел <b>API development tools</b> — создайте приложение (название любое).</li>
  <li>Скопируйте сюда <b>api_id</b> и <b>api_hash</b>, укажите номер телефона и нажмите
      «Сохранить».</li>
  <li>Нажмите <b>«Получить код»</b> вверху страницы. Код придёт в Telegram — введите его
      в поле «Код из Telegram» и сохраните. Если на аккаунте стоит облачный пароль, панель
      попросит и его.</li>
  <li>Готово: закрытые каналы, где состоит аккаунт, теперь собираются. Добавить их в список
      можно кнопкой <b>«Добавить мои каналы»</b>.</li>
</ol>
<p class="text-subtle" style="margin-top:8px">Лучше подключать отдельный, а не личный аккаунт:
Telegram может ограничить аккаунт за слишком активное чтение. Сессия хранится в базе панели
и даёт полный доступ к аккаунту — не передавайте файл базы посторонним. Отключить доступ
можно кнопкой «Отключить» или в Telegram: Настройки → Устройства.</p>
"""


@admin.register(TgAccount)
class TgAccountAdmin(ModelAdmin):
    form = TgAccountForm
    readonly_fields = ("help_view", "status_view", "me", "connected_at", "last_error")
    actions_detail = ["send_code", "check_conn", "import_channels", "disconnect"]

    def get_fieldsets(self, request, obj=None):
        sets = [
            ("Как подключить", {"fields": ("help_view",)}),
            ("Данные приложения Telegram", {"fields": ("api_id", "api_hash", "phone",
                                                       "auto_join")}),
        ]
        if obj and obj.status == "code_sent":
            sets.insert(0, ("Вход", {"fields": ("code",)}))
        elif obj and obj.status == "password":
            sets.insert(0, ("Вход", {"fields": ("password",)}))
        sets.insert(0, ("Подключение", {"fields": ("status_view", "me", "connected_at",
                                                   "last_error")}))
        return sets

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:screening_tgaccount_change", args=[get_account().pk]))

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context.update({"show_save_and_add_another": False, "show_save_and_view": False,
                        "show_delete": False, "show_save_and_continue": False})
        return super().render_change_form(request, context, add, change, form_url, obj)

    def response_change(self, request, obj):
        return redirect(reverse("admin:screening_tgaccount_change", args=[obj.pk]))

    @display(description="Инструкция")
    def help_view(self, obj):
        from django.utils.safestring import mark_safe
        return mark_safe(HELP_HTML)

    @display(description="Статус")
    def status_view(self, obj):
        color = {"connected": "green", "code_sent": "orange", "password": "orange",
                 "error": "red"}.get(obj.status, "gray")
        return format_html('<span class="pf-chip {}" style="font-size:12px">{}</span>',
                           color, obj.get_status_display())

    # сохранение: смена данных сбрасывает сессию, введённый код/пароль — завершает вход
    def save_model(self, request, obj, form, change):
        if {"api_id", "api_hash", "phone"} & set(form.changed_data) and obj.status != "off":
            self._forget(obj)
            messages.warning(request, "Данные приложения или номер изменены — подключите "
                                      "аккаунт заново кнопкой «Получить код»")
        super().save_model(request, obj, form, change)

        code = (form.cleaned_data.get("code") or "").strip().replace(" ", "")
        password = form.cleaned_data.get("password") or ""
        if obj.status == "code_sent" and code:
            self._finish_code(request, obj, code)
        elif obj.status == "password" and password:
            self._finish_password(request, obj, password)
        elif change:
            messages.success(request, "Сохранено")

    def _ready(self, request, obj) -> bool:
        if not tg.telethon_available():
            messages.error(request, "Не установлен telethon: выполните "
                                    "pip install -r requirements.txt и перезапустите панель")
            return False
        if not obj.has_credentials:
            messages.error(request, "Заполните api_id, api_hash и номер телефона и сохраните")
            return False
        return True

    def _fail(self, request, obj, e: Exception, status=None):
        obj.last_error = tg.error_text(e)
        fields = ["last_error"]
        if status:
            obj.status = status
            fields.append("status")
        obj.save(update_fields=fields)
        messages.error(request, obj.last_error)

    def _connected(self, request, obj, session, me):
        obj.session, obj.me, obj.status = session, me, "connected"
        obj.phone_code_hash, obj.last_error, obj.connected_at = "", "", timezone.now()
        obj.save()
        messages.success(request, f"Аккаунт подключён: {me}. Закрытые каналы будут "
                                  "собираться при следующем запуске.")

    def _finish_code(self, request, obj, code):
        if not self._ready(request, obj):
            return
        try:
            status, session, me = tg.run(tg.sign_in_code(
                obj.api_id, obj.api_hash, obj.session, obj.phone, code, obj.phone_code_hash))
        except Exception as e:
            return self._fail(request, obj, e)
        if status == "password":
            obj.session, obj.status, obj.last_error = session, "password", ""
            obj.save()
            messages.warning(request, "На аккаунте включена двухэтапная проверка — "
                                      "введите облачный пароль")
            return
        self._connected(request, obj, session, me)

    def _finish_password(self, request, obj, password):
        if not self._ready(request, obj):
            return
        try:
            session, me = tg.run(tg.sign_in_password(obj.api_id, obj.api_hash, obj.session,
                                                     password))
        except Exception as e:
            return self._fail(request, obj, e)
        self._connected(request, obj, session, me)

    @staticmethod
    def _forget(obj):
        obj.session = obj.phone_code_hash = obj.me = ""
        obj.status, obj.connected_at = "off", None

    # кнопки вверху страницы
    def has_send_code_permission(self, request, object_id=None):
        return get_account().status != "connected"

    def has_connected_permission(self, request, object_id=None):
        return get_account().status == "connected"

    def has_session_permission(self, request, object_id=None):
        return bool(get_account().session) or get_account().status != "off"

    @action(description="Получить код", icon="sms", variant="primary",
            permissions=["send_code"])
    def send_code(self, request, object_id):
        obj = get_account()
        back = redirect(reverse("admin:screening_tgaccount_change", args=[obj.pk]))
        if not self._ready(request, obj):
            return back
        try:
            session, code_hash = tg.run(tg.send_code(obj.api_id, obj.api_hash, obj.phone))
        except Exception as e:
            self._fail(request, obj, e)
            return back
        obj.session, obj.phone_code_hash = session, code_hash
        obj.status, obj.last_error, obj.me = "code_sent", "", ""
        obj.save()
        messages.success(request, "Код отправлен в Telegram (чат «Telegram» на ваших устройствах"
                                  ", иногда — SMS). Введите его ниже и нажмите «Сохранить».")
        return back

    @action(description="Проверить", icon="verified_user", permissions=["connected"])
    def check_conn(self, request, object_id):
        obj = get_account()
        back = redirect(reverse("admin:screening_tgaccount_change", args=[obj.pk]))
        if not self._ready(request, obj):
            return back
        try:
            me = tg.run(tg.whoami(obj.api_id, obj.api_hash, obj.session))
        except Exception as e:
            self._fail(request, obj, e)
            return back
        if me:
            obj.me, obj.last_error = me, ""
            obj.save(update_fields=["me", "last_error"])
            messages.success(request, f"Аккаунт работает: {me}")
        else:
            obj.status, obj.last_error = "error", "Сессия больше не действует"
            obj.save(update_fields=["status", "last_error"])
            messages.error(request, "Сессия больше не действует — подключите аккаунт заново")
        return back

    @action(description="Добавить мои каналы", icon="playlist_add", permissions=["connected"])
    def import_channels(self, request, object_id):
        obj = get_account()
        if not self._ready(request, obj):
            return redirect(reverse("admin:screening_tgaccount_change", args=[obj.pk]))
        try:
            chans = tg.run(tg.my_channels(obj.api_id, obj.api_hash, obj.session))
        except Exception as e:
            self._fail(request, obj, e)
            return redirect(reverse("admin:screening_tgaccount_change", args=[obj.pk]))
        known = set(TgChannel.objects.values_list("ref", flat=True))
        known_ids = set(TgChannel.objects.exclude(channel_id=None)
                        .values_list("channel_id", flat=True))
        added = 0
        for c in chans:
            ref = c["username"].lower() if c["username"] else f"c/{c['id']}"
            if ref in known or c["id"] in known_ids:
                continue
            TgChannel.objects.create(
                ref=ref, username=c["username"], channel_id=c["id"], title=c["title"][:300],
                method="auto" if c["username"] else "account", is_active=False,
                note="из аккаунта")
            added += 1
        messages.success(request, f"Каналов в аккаунте: {len(chans)}, добавлено новых: {added}. "
                                  "Они добавлены выключенными — включите нужные.")
        return redirect(reverse("admin:screening_tgchannel_changelist")
                        + "?is_active__exact=0")

    @action(description="Отключить", icon="logout", variant="danger", permissions=["session"])
    def disconnect(self, request, object_id):
        obj = get_account()
        if obj.session and obj.status == "connected" and tg.telethon_available():
            try:
                tg.run(tg.log_out(obj.api_id, obj.api_hash, obj.session))
            except Exception as e:
                messages.warning(request, f"Не удалось завершить сессию в Telegram "
                                          f"({tg.error_text(e)}) — удалите её вручную: "
                                          "Настройки → Устройства")
        self._forget(obj)
        obj.last_error = ""
        obj.save()
        messages.success(request, "Аккаунт отключён, сессия удалена")
        return redirect(reverse("admin:screening_tgaccount_change", args=[obj.pk]))
