"""Настройки админ-панели. Панель работает с той же базой, что и main.py."""
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from django.templatetags.static import static
from django.urls import reverse_lazy

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config as aml_config  # noqa: E402  (читает .env)

# Локальный инструмент. Если будете выкладывать на сервер — задайте свой ключ
# в переменной окружения PANEL_SECRET_KEY и выключите DEBUG.
SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "local-dev-only-change-me")
DEBUG = os.environ.get("PANEL_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("PANEL_HOSTS", "127.0.0.1,localhost").split(",")

INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "screening",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "panel.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# Та же база PostgreSQL, в которую пишет main.py (адрес — AML_DATABASE_URL в .env)
def _db_from_url(url: str) -> dict:
    if not url:
        # Без .env панель всё равно должна запускаться (например, для setup), но
        # любое обращение к базе выдаст понятную ошибку подключения.
        url = "postgresql://aml@127.0.0.1:5432/aml_news"
    u = urlsplit(url)
    opts = dict(parse_qsl(u.query))
    opts.setdefault("connect_timeout", "10")
    opts.setdefault("application_name", "aml_panel")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(u.path.lstrip("/")) or "aml_news",
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname or "127.0.0.1",
        "PORT": str(u.port or 5432),
        "OPTIONS": opts,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }


DATABASES = {"default": _db_from_url(aml_config.DATABASE_URL)}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "ru"
LANGUAGES = [("ru", "Русский")]
TIME_ZONE = "Asia/Almaty"
USE_I18N = True
USE_TZ = True

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"
STATIC_URL = "static/"
MEDIA_ROOT = BASE_DIR / "panel_files"       # загруженные списки и логи запусков
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 200 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

UNFOLD = {
    "SITE_TITLE": "AML-скрининг",
    "SITE_HEADER": "AML-скрининг",
    "SITE_SUBHEADER": "Новости по ОД/ФТ",
    "SITE_SYMBOL": "policy",
    "DASHBOARD_CALLBACK": "screening.dashboard.dashboard_callback",
    "SHOW_HISTORY": False,
    "SHOW_VIEW_ON_SITE": False,
    # Светлая тема с нейтральным серым и синими акцентами (кнопки, ссылки)
    "THEME": "light",
    "BORDER_RADIUS": "6px",
    "STYLES": [lambda request: static("screening/panel.css")],
    "SCRIPTS": [lambda request: static("screening/theme.js")],
    "COLORS": {
        "base": {   # нейтральный серый (Tailwind neutral)
            "50": "oklch(98.5% 0 0)",
            "100": "oklch(97% 0 0)",
            "200": "oklch(92.2% 0 0)",
            "300": "oklch(87% 0 0)",
            "400": "oklch(70.8% 0 0)",
            "500": "oklch(55.6% 0 0)",
            "600": "oklch(43.9% 0 0)",
            "700": "oklch(37.1% 0 0)",
            "800": "oklch(26.9% 0 0)",
            "900": "oklch(20.5% 0 0)",
            "950": "oklch(14.5% 0 0)",
        },
        "primary": {   # синий (Tailwind blue)
            "50": "oklch(97% .014 254.604)",
            "100": "oklch(93.2% .032 255.585)",
            "200": "oklch(88.2% .059 254.128)",
            "300": "oklch(80.9% .105 251.813)",
            "400": "oklch(70.7% .165 254.624)",
            "500": "oklch(62.3% .214 259.815)",
            "600": "oklch(54.6% .245 262.881)",
            "700": "oklch(48.8% .243 264.376)",
            "800": "oklch(42.4% .199 265.638)",
            "900": "oklch(37.9% .146 265.522)",
            "950": "oklch(28.2% .091 267.935)",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Скрининг",
                "items": [
                    {"title": "Обзор", "icon": "dashboard", "link": reverse_lazy("admin:index")},
                    {"title": "Совпадения", "icon": "rule",
                     "link": reverse_lazy("admin:screening_match_changelist")},
                    {"title": "Новости", "icon": "newspaper",
                     "link": reverse_lazy("admin:screening_article_changelist")},
                    {"title": "Список получателей", "icon": "groups",
                     "link": reverse_lazy("admin:screening_entity_changelist")},
                ],
            },
            {
                "title": "Негативные новости РК",
                "items": [
                    {"title": "Обзор", "icon": "monitoring",
                     "link": reverse_lazy("neg_overview")},
                    {"title": "Новости", "icon": "campaign",
                     "link": reverse_lazy("admin:screening_negnews_changelist")},
                    {"title": "Источники", "icon": "rss_feed",
                     "link": reverse_lazy("admin:screening_negsource_changelist")},
                ],
            },
            {
                "title": "Управление",
                "items": [
                    {"title": "Запуски", "icon": "play_circle",
                     "link": reverse_lazy("admin:screening_job_changelist")},
                    {"title": "Автозапуск", "icon": "schedule",
                     "link": reverse_lazy("admin:screening_schedule_changelist")},
                    {"title": "Пользователи", "icon": "person",
                     "link": reverse_lazy("admin:auth_user_changelist")},
                ],
            },
        ],
    },
}
