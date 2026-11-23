"""
Модели поверх таблиц, которые создаёт main.py (managed=False — Django их не трогает),
плюс таблица запусков Job, которой управляет сама панель.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

KIND_CHOICES = [("org", "Юрлицо"), ("person", "Физлицо")]
REVIEW_CHOICES = [("new", "Не проверено"), ("relevant", "Релевантно"),
                  ("false_positive", "Ложное срабатывание")]


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
    published = models.TextField("Дата", null=True, blank=True)
    lang = models.TextField("Язык", null=True, blank=True)
    country = models.TextField("Страна", null=True, blank=True)
    collector = models.TextField("Откуда собрано", null=True, blank=True)
    query = models.TextField("Запрос", null=True, blank=True)
    summary = models.TextField("Описание", null=True, blank=True)
    text = models.TextField("Полный текст", null=True, blank=True)
    text_status = models.TextField("Статус текста", null=True, blank=True)
    collected_at = models.TextField("Собрано", null=True, blank=True)
    matched_at = models.TextField("Проверено матчингом", null=True, blank=True)

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
    published = models.TextField("Дата", null=True)
    lang = models.TextField("Язык", null=True)
    matched_text = models.TextField("Как написано в новости", null=True)
    categories = models.TextField("Категории", null=True)
    keywords = models.TextField("Ключевые слова", null=True)
    in_title = models.BooleanField("Имя в заголовке", default=False)
    score = models.IntegerField("Балл", null=True)
    score_raw = models.IntegerField("Балл до штрафа", null=True)
    frequent = models.BooleanField("Частое имя", default=False)
    snippet = models.TextField("Фрагмент", null=True)
    created_at = models.TextField("Найдено", null=True)
    review_status = models.TextField("Статус проверки", choices=REVIEW_CHOICES, default="new")
    review_note = models.TextField("Комментарий", null=True, blank=True)

    class Meta:
        managed = False
        db_table = "matches"
        verbose_name = "совпадение"
        verbose_name_plural = "Совпадения"

    def __str__(self):
        return f"{self.entity_name} — {self.title or self.url}"


class Job(models.Model):
    COMMANDS = [
        ("pipeline", "Полный цикл: сбор → тексты → матчинг"),
        ("collect", "Сбор новостей"),
        ("fetch-text", "Докачать полный текст"),
        ("match", "Матчинг по списку"),
        ("load-names", "Загрузить список имён"),
    ]
    STATUSES = [("queued", "В очереди"), ("running", "Выполняется"), ("done", "Готово"),
                ("failed", "Ошибка"), ("stopped", "Остановлено"),
                ("interrupted", "Прервано (перезапуск панели)")]

    command = models.CharField("Что запустить", max_length=20, choices=COMMANDS, default="pipeline")
    use_gdelt = models.BooleanField("GDELT", default=True)
    use_gnews = models.BooleanField("Google News", default=True)
    use_rss = models.BooleanField("RSS-ленты", default=True)
    use_telegram = models.BooleanField("Telegram-каналы", default=True)
    days = models.PositiveIntegerField("За сколько дней", default=90,
                                       help_text="GDELT хранит ~90 дней, Google News — ~30")
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
        if self.command in ("collect", "pipeline"):
            if not (self.use_gdelt or self.use_gnews or self.use_rss or self.use_telegram):
                raise ValidationError("Выберите хотя бы один источник")
            if not self.days:
                raise ValidationError({"days": "Укажите хотя бы 1 день"})


class Schedule(models.Model):
    """Автозапуск по расписанию (одна запись на всю панель)."""
    COMMANDS = [("pipeline", "Полный цикл: сбор → тексты → матчинг"),
                ("collect", "Только сбор новостей")]

    enabled = models.BooleanField("Включён", default=False)
    every_hours = models.PositiveIntegerField("Запускать каждые (часов)", default=24)
    command = models.CharField("Что запускать", max_length=20, choices=COMMANDS,
                               default="pipeline")
    use_gdelt = models.BooleanField("GDELT", default=True)
    use_gnews = models.BooleanField("Google News", default=True)
    use_rss = models.BooleanField("RSS-ленты", default=True)
    use_telegram = models.BooleanField("Telegram-каналы", default=True)
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
