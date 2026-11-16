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
            if not (self.use_gdelt or self.use_gnews or self.use_rss):
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
