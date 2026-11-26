"""
Модели поверх таблиц, которые создаёт db.py (managed=False — Django их не трогает),
плюс таблицы запусков и автозапуска, которыми управляет сама панель.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

KIND_CHOICES = [("org", "Юрлицо"), ("person", "Физлицо")]
REVIEW_CHOICES = [("new", "Не проверено"), ("relevant", "Релевантно"),
                  ("false_positive", "Ложное срабатывание")]
NEG_REVIEW_CHOICES = [("new", "Не проверено"), ("relevant", "Актуально"),
                      ("irrelevant", "Не актуально")]
NEG_COLLECTOR_CHOICES = [("telegram", "Telegram"), ("rss", "RSS"), ("gnews", "Google News"),
                         ("gdelt", "GDELT"), ("manual", "Вручную")]
NEG_SOURCE_KINDS = [("telegram", "Telegram-канал"), ("rss", "RSS-лента")]


class Entity(models.Model):
    name = models.TextField("Имя / организация")
    variants = models.TextField("Другие написания", null=True, blank=True)
    kind = models.TextField("Тип", choices=KIND_CHOICES)
    core = models.TextField("Нормализованное ядро", null=True, blank=True)
    pattern = models.TextField("Шаблон поиска", null=True, blank=True)
    tokens = models.TextField(null=True, blank=True)
    skip_reason = models.TextField("Почему не ищем", null=True, blank=True)

    class Meta:
        managed = False
        db_table = "entities"
        verbose_name = "получатель"
        verbose_name_plural = "Список получателей"

    def __str__(self):
        return self.name


class Article(models.Model):
    url = models.TextField("Ссылка", unique=True)
    title = models.TextField("Заголовок", null=True, blank=True)
    source = models.TextField("Источник", null=True, blank=True)
    published = models.DateTimeField("Дата", null=True, blank=True)
    lang = models.TextField("Язык", null=True, blank=True)
    country = models.TextField("Страна", null=True, blank=True)
    collector = models.TextField("Откуда собрано", null=True, blank=True)
    query = models.TextField("Запрос", null=True, blank=True)
    summary = models.TextField("Описание", null=True, blank=True)
    text = models.TextField("Полный текст", null=True, blank=True)
    text_status = models.TextField("Статус текста", null=True, blank=True)
    collected_at = models.DateTimeField("Собрано", null=True, blank=True)
    matched_at = models.DateTimeField("Проверено матчингом", null=True, blank=True)

    class Meta:
        managed = False
        db_table = "articles"
        verbose_name = "новость"
        verbose_name_plural = "Новости"

    def __str__(self):
        return self.title or self.url


class Match(models.Model):
    entity = models.ForeignKey(Entity, models.DO_NOTHING, db_column="entity_id",
                               verbose_name="Получатель")
    article = models.ForeignKey(Article, models.DO_NOTHING, db_column="article_id",
                                verbose_name="Новость")
    entity_name = models.TextField("Имя / организация", null=True)
    entity_kind = models.TextField("Тип", choices=KIND_CHOICES, null=True)
    source = models.TextField("Источник", null=True)
    title = models.TextField("Заголовок", null=True)
    url = models.TextField("Ссылка", null=True)
    published = models.DateTimeField("Дата", null=True)
    lang = models.TextField("Язык", null=True)
    matched_text = models.TextField("Как написано в новости", null=True)
    categories = models.TextField("Категории", null=True)
    keywords = models.TextField("Ключевые слова", null=True)
    in_title = models.BooleanField("Имя в заголовке", default=False)
    score = models.IntegerField("Балл", null=True)
    score_raw = models.IntegerField("Балл до штрафа", null=True)
    frequent = models.BooleanField("Частое имя", default=False)
    snippet = models.TextField("Фрагмент", null=True)
    created_at = models.DateTimeField("Найдено", null=True)
    review_status = models.TextField("Статус проверки", choices=REVIEW_CHOICES, default="new")
    review_note = models.TextField("Комментарий", null=True, blank=True)

    class Meta:
        managed = False
        db_table = "matches"
        verbose_name = "совпадение"
        verbose_name_plural = "Совпадения"

    def __str__(self):
        return f"{self.entity_name} — {self.title or self.url}"


# --- Негативные новости о Казахстане -------------------------------------------
class NegSource(models.Model):
    kind = models.TextField("Тип", choices=NEG_SOURCE_KINDS, default="telegram")
    name = models.TextField("Название издания / канала")
    handle = models.TextField(
        "Адрес", help_text="Для Telegram — имя публичного канала (например, tengrinews или "
                           "https://t.me/tengrinews), для RSS — полный адрес ленты")
    enabled = models.BooleanField("Включён", default=True)
    kz = models.BooleanField(
        "Казахстанское издание", default=True,
        help_text="Если выключено, новость берётся, только когда в ней упомянут Казахстан, "
                  "регион или город РК")
    region = models.TextField("Регион издания", null=True, blank=True,
                              help_text="Для региональных изданий: подставится, если в "
                                        "тексте регион не назван")
    note = models.TextField("Заметка", null=True, blank=True)
    last_run_at = models.DateTimeField("Последнее чтение", null=True, blank=True)
    last_status = models.TextField("Результат", null=True, blank=True)
    last_found = models.IntegerField("Записей в ленте", null=True, blank=True)
    last_kept = models.IntegerField("Новых негативных", null=True, blank=True)
    last_item_at = models.DateTimeField("Последняя публикация", null=True, blank=True)
    total_kept = models.IntegerField("Всего найдено", default=0)
    created_at = models.DateTimeField("Добавлен", default=timezone.now)

    class Meta:
        managed = False
        db_table = "neg_sources"
        verbose_name = "источник"
        verbose_name_plural = "Источники негативных новостей"

    def __str__(self):
        return self.name

    def clean(self):
        h = (self.handle or "").strip()
        if self.kind == "telegram":
            h = h.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
            if not h or not all(c.isascii() and (c.isalnum() or c == "_") for c in h):
                raise ValidationError({"handle": "Укажите имя канала: латиница, цифры, _"})
        elif not h.startswith(("http://", "https://")):
            raise ValidationError({"handle": "Адрес RSS должен начинаться с http:// или https://"})
        self.handle = h
        if NegSource.objects.filter(kind=self.kind, handle=h).exclude(pk=self.pk).exists():
            raise ValidationError({"handle": "Такой источник уже есть"})


class NegNews(models.Model):
    url = models.TextField("Ссылка", unique=True, null=True, blank=True)
    post_url = models.TextField("Пост в Telegram", null=True, blank=True)
    title = models.TextField("Название", null=True, blank=True)
    source = models.TextField("Издание", null=True, blank=True)
    source_domain = models.TextField("Домен", null=True, blank=True)
    source_ref = models.ForeignKey(NegSource, models.DO_NOTHING, db_column="source_id",
                                   null=True, blank=True, verbose_name="Источник")
    collector = models.TextField("Откуда собрано", choices=NEG_COLLECTOR_CHOICES)
    query = models.TextField("Запрос / лента", null=True, blank=True)
    published = models.DateTimeField("Дата публикации", null=True, blank=True)
    lang = models.TextField("Язык", null=True, blank=True)
    summary = models.TextField("Описание / текст поста", null=True, blank=True)
    text = models.TextField("Текст", null=True, blank=True)
    text_status = models.TextField("Статус текста", null=True, blank=True)
    title_key = models.TextField(null=True, blank=True)
    copies = models.IntegerField("Перепечаток", default=1)
    also_in = models.TextField("Также в изданиях", null=True, blank=True)
    categories = models.TextField("Категории", null=True, blank=True)
    keywords = models.TextField("Ключевые слова", null=True, blank=True)
    regions = models.TextField("Регион", null=True, blank=True)
    kz_outlet = models.BooleanField("Казахстанское издание", default=False)
    kz_signal = models.TextField("Почему о Казахстане", null=True, blank=True)
    score = models.IntegerField("Балл", default=0)
    tone = models.FloatField("Тональность GDELT", null=True, blank=True)
    review_status = models.TextField("Статус", choices=NEG_REVIEW_CHOICES, default="new")
    review_note = models.TextField("Комментарий", null=True, blank=True)
    reviewed_by = models.TextField("Проверил", null=True, blank=True)
    reviewed_at = models.DateTimeField("Когда проверено", null=True, blank=True)
    collected_at = models.DateTimeField("Собрано", default=timezone.now)
    analyzed_at = models.DateTimeField("Размечено", null=True, blank=True)
    # колонка search (tsvector) есть в таблице, но в модели её нет намеренно:
    # её вычисляет сам PostgreSQL, а поиск идёт через сырой SQL (см. admin)

    class Meta:
        managed = False
        db_table = "neg_news"
        verbose_name = "негативную новость"   # винительный: «Добавить…», «Удалить…»
        verbose_name_plural = "Негативные новости"

    def __str__(self):
        return self.title or self.url or f"#{self.pk}"

    @property
    def category_list(self):
        return [c.strip() for c in (self.categories or "").split(";") if c.strip()]

    @property
    def region_list(self):
        return [c.strip() for c in (self.regions or "").split(";") if c.strip()]


# --- Запуски и автозапуск (таблицы панели) ------------------------------------
class Job(models.Model):
    COMMANDS = [
        ("pipeline", "Полный цикл: сбор → тексты → матчинг"),
        ("collect", "Сбор новостей"),
        ("fetch-text", "Докачать полный текст"),
        ("match", "Матчинг по списку"),
        ("load-names", "Загрузить список имён"),
        ("neg-pipeline", "Негативные новости РК: сбор → тексты → разметка"),
        ("neg-analyze", "Негативные новости: пересчитать разметку"),
        ("both", "Всё сразу: скрининг + негативные новости"),
    ]
    STATUSES = [("queued", "В очереди"), ("running", "Выполняется"), ("done", "Готово"),
                ("failed", "Ошибка"), ("stopped", "Остановлено"),
                ("interrupted", "Прервано (перезапуск панели)")]

    command = models.CharField("Что запустить", max_length=20, choices=COMMANDS, default="pipeline")
    use_gdelt = models.BooleanField("GDELT", default=True)
    use_gnews = models.BooleanField("Google News", default=True)
    use_rss = models.BooleanField("RSS-ленты", default=True)
    use_telegram = models.BooleanField("Telegram-каналы", default=True,
                                       help_text="Каналы из разделов «Telegram» и «Негативные новости РК → Источники»")
    neg_source_id = models.PositiveIntegerField("Только источник №", null=True, blank=True)
    days = models.PositiveIntegerField("За сколько дней", default=90,
                                       help_text="GDELT хранит ~90 дней, Google News — ~30. "
                                                 "Для негативных новостей хватит 7")
    slice_days = models.PositiveIntegerField(
        "Резать GDELT на окна (дней)", null=True, blank=True,
        help_text="Например 7: соберёт в разы больше статей, но займёт ~час")
    match_all = models.BooleanField("Перепроверить все статьи", default=False,
                                    help_text="Нужно после загрузки нового списка имён")
    keep_weak = models.BooleanField("Сохранять совпадения без ключевых слов рядом", default=False)
    names_file = models.FileField("Файл со списком (xlsx/csv)", upload_to="uploads/",
                                  null=True, blank=True)
    names_column = models.CharField("Колонка с именами", max_length=200, blank=True,
                                    help_text="Пусто — первая колонка")

    trigger = models.CharField("Кто запустил", max_length=20, default="вручную")
    status = models.CharField("Статус", max_length=20, choices=STATUSES, default="queued")
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    started_at = models.DateTimeField("Начат", null=True, blank=True)
    finished_at = models.DateTimeField("Завершён", null=True, blank=True)
    returncode = models.IntegerField("Код завершения", null=True, blank=True)
    log_file = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "запуск"
        verbose_name_plural = "Запуски"
        ordering = ["-id"]

    def __str__(self):
        return f"#{self.pk} {self.get_command_display()}"

    def clean(self):
        if self.command == "load-names" and not self.names_file:
            raise ValidationError({"names_file": "Для загрузки списка нужен файл"})
        if self.command in ("collect", "pipeline", "neg-pipeline", "both"):
            sources = [self.use_gdelt, self.use_gnews, self.use_rss, self.use_telegram]
            if not any(sources):
                raise ValidationError("Выберите хотя бы один источник")
            if not self.days:
                raise ValidationError({"days": "Укажите хотя бы 1 день"})


class Schedule(models.Model):
    """Автозапуск по расписанию (одна запись на всю панель)."""
    COMMANDS = [("pipeline", "Скрининг: сбор → тексты → матчинг"),
                ("collect", "Скрининг: только сбор новостей"),
                ("neg-pipeline", "Негативные новости РК"),
                ("both", "Всё сразу: скрининг + негативные новости")]

    enabled = models.BooleanField("Включён", default=False)
    every_hours = models.PositiveIntegerField("Запускать каждые (часов)", default=24)
    command = models.CharField("Что запускать", max_length=20, choices=COMMANDS,
                               default="pipeline")
    use_gdelt = models.BooleanField("GDELT", default=True)
    use_gnews = models.BooleanField("Google News", default=True)
    use_rss = models.BooleanField("RSS-ленты", default=True)
    use_telegram = models.BooleanField("Telegram-каналы", default=True,
                                       help_text="Каналы из разделов «Telegram» и «Негативные новости РК → Источники»")
    days = models.PositiveIntegerField(
        "Собирать новости за (дней)", default=3,
        help_text="Для ежедневного запуска хватит 2–3 дней: старое уже в базе")
    last_run_at = models.DateTimeField("Последний автозапуск", null=True, blank=True)
    last_job = models.ForeignKey(Job, models.SET_NULL, null=True, blank=True,
                                 verbose_name="Последний запуск", related_name="+")

    class Meta:
        verbose_name = "автозапуск"
        verbose_name_plural = "Автозапуск"

    def __str__(self):
        return "Автозапуск"

    def next_run_at(self):
        if not self.enabled:
            return None
        if not self.last_run_at:
            return timezone.now()
        return self.last_run_at + timedelta(hours=max(self.every_hours, 1))


class TgChannel(models.Model):
    """Telegram-канал, из которого собираются посты (см. tg_collect.py)."""
    METHODS = [("auto", "Авто: веб-версия, если её нет — через аккаунт"),
               ("web", "Только веб-версия t.me/s (без аккаунта)"),
               ("account", "Только через подключённый аккаунт")]

    ref = models.CharField("Канал", max_length=200, unique=True,
                           help_text="@username, ссылка t.me/…, инвайт t.me/+… или t.me/c/…")
    username = models.CharField("Имя канала", max_length=64, blank=True)
    invite_hash = models.CharField("Инвайт", max_length=100, blank=True)
    channel_id = models.BigIntegerField("ID канала", null=True, blank=True)
    title = models.CharField("Название", max_length=300, blank=True)
    method = models.CharField("Как читать", max_length=10, choices=METHODS, default="auto")
    is_active = models.BooleanField("Собирать", default=True)
    note = models.CharField("Заметка", max_length=300, blank=True)

    last_post_id = models.BigIntegerField("Последний пост", null=True, blank=True)
    last_post_at = models.DateTimeField("Дата последнего поста", null=True, blank=True)
    last_checked_at = models.DateTimeField("Проверен", null=True, blank=True)
    last_via = models.CharField("Прочитан через", max_length=10, blank=True)
    last_error = models.TextField("Ошибка", blank=True)
    posts_saved = models.PositiveIntegerField("Сохранено постов", default=0)
    created_at = models.DateTimeField("Добавлен", auto_now_add=True)

    class Meta:
        verbose_name = "Telegram-канал"
        verbose_name_plural = "Telegram-каналы"
        ordering = ["-is_active", "title", "ref"]

    def __str__(self):
        return self.title or self.display_ref

    @property
    def is_private(self) -> bool:
        return not self.username

    @property
    def display_ref(self) -> str:
        if self.username:
            return "@" + self.username
        if self.invite_hash:
            return "t.me/+" + self.invite_hash
        return f"t.me/c/{self.channel_id}"

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}"
        if self.invite_hash:
            return f"https://t.me/+{self.invite_hash}"
        return f"https://t.me/c/{self.channel_id}/999999999"


class TgAccount(models.Model):
    """Подключённый Telegram-аккаунт (юзербот) для закрытых каналов. Одна запись."""
    STATUSES = [("off", "Не подключён"), ("code_sent", "Ждёт код из Telegram"),
                ("password", "Ждёт пароль двухэтапной проверки"),
                ("connected", "Подключён"), ("error", "Ошибка — подключите заново")]

    api_id = models.PositiveIntegerField("api_id", null=True, blank=True,
                                         help_text="Число с my.telegram.org → API development tools")
    api_hash = models.CharField("api_hash", max_length=64, blank=True,
                                help_text="Строка из 32 символов оттуда же")
    phone = models.CharField("Номер телефона", max_length=32, blank=True,
                             help_text="В международном формате: +77001234567")
    auto_join = models.BooleanField(
        "Вступать в каналы по инвайт-ссылкам", default=False,
        help_text="Если аккаунт ещё не состоит в канале из списка — вступить автоматически")

    status = models.CharField("Статус", max_length=20, choices=STATUSES, default="off")
    # Строка сессии Telethon: даёт полный доступ к аккаунту, наружу не показывается
    session = models.TextField(blank=True)
    phone_code_hash = models.CharField(max_length=100, blank=True)
    me = models.CharField("Аккаунт", max_length=200, blank=True)
    connected_at = models.DateTimeField("Подключён", null=True, blank=True)
    last_error = models.TextField("Последняя ошибка", blank=True)

    class Meta:
        verbose_name = "Telegram-аккаунт"
        verbose_name_plural = "Telegram-аккаунт"

    def __str__(self):
        return self.me or "Telegram-аккаунт"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash and self.phone)
