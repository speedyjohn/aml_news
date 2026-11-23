"""
Настройки: ключевые слова, запросы для сбора новостей, источники, словари.
Всё, что здесь написано обычным текстом, при запуске прогоняется через
normalize(), так что писать можно как удобно — кириллицей или латиницей.
"""

DB_PATH = "aml_news.sqlite"

# Сколько символов вокруг найденного имени смотреть на ключевые слова
KEYWORD_WINDOW = 250
# Имя, найденное в стольких разных статьях и больше, помечается как «частое»
# (скорее всего, это обычное слово: TENNIS, AFM, NOMAD) и получает штраф к баллу
FREQUENT_ENTITY_ARTICLES = 25
FREQUENT_PENALTY = 25
# Длина сниппета вокруг совпадения (в каждую сторону)
SNIPPET_CHARS = 250

# ---------------------------------------------------------------------------
# Ключевые слова нарушений: категория -> основы слов.
# Совпадение ищется с начала слова: "отмыв" ловит "отмывание", "отмыванию" и т.д.
# ---------------------------------------------------------------------------
KEYWORDS = {
    "ОД (отмывание)": [
        "money launder", "launder", "AML violation",
        "отмыв", "легализац", "обналич", "обнал", "незаконн вывод",
        "жылыстат", "заңсыз",
    ],
    "ФТ / экстремизм": [
        "terrorist financ", "terror financ", "financing of terror", "extremis",
        "финансирован террор", "терроризм", "террорист", "экстремис",
        "лаңкест", "экстремист",
    ],
    "Санкции": [
        "sanction", "OFAC", "SDN list", "asset freeze", "blacklist", "export control",
        "санкц", "черный список", "заморож", "обход санкц",
        "санкция",
    ],
    "Мошенничество / хищения": [
        "fraud", "scam", "ponzi", "embezzl", "misappropriat",
        "мошенни", "пирамид", "хищени", "растрат", "присвоени", "аферист",
        "алаяқ", "ұрла",
    ],
    "Коррупция": [
        "brib", "corrupt", "kickback",
        "взятк", "взяточ", "коррупц", "откат",
        "сыбайлас", "пара алу", "пара беру",
    ],
    "Уголовное преследование": [
        "indict", "convicted", "sentenced", "arrested", "charged with", "prosecut",
        "criminal case", "guilty plea", "seized",
        "уголовн", "задержан", "арестован", "обвинени", "приговор", "осужден",
        "обыск", "конфиск", "розыск",
        "қылмыстық", "ұсталды", "сотталды",
    ],
    "Контрабанда / налоги": [
        "smuggl", "tax evasion", "drug traffick", "illicit",
        "контрабанд", "уклонени от уплат", "наркотрафик", "лжепредприят",
        "фиктивн", "контрабанда", "салықтан жалтару",
    ],
    "Регулятор / финмониторинг": [
        "AFM", "АФМ", "финмониторинг", "финансового мониторинга", "FATF", "FinCEN",
        "қаржылық мониторинг",
    ],
}

# ---------------------------------------------------------------------------
# Сбор: GDELT (бесплатно, без ключа, ~3 последних месяца, до 250 статей на запрос)
# ---------------------------------------------------------------------------
GDELT_KEYWORDS_EN = [
    '"money laundering"', '"terrorist financing"', '"terrorism financing"',
    '"sanctions evasion"', 'sanctions', 'fraud', 'embezzlement', 'bribery',
    '"tax evasion"', 'smuggling', '"ponzi scheme"', '"shell company"',
]
GDELT_KEYWORDS_RU = [
    '"отмывание денег"', '"легализация доходов"', '"финансирование терроризма"',
    'санкции', 'мошенничество', 'хищение', 'взятка', 'контрабанда',
    '"уголовное дело"', '"финансового мониторинга"', '"обход санкций"',
]
# Каждое ключевое слово комбинируется с каждым «срезом»
GDELT_SCOPES_EN = ["sourcecountry:KZ", "sourcelang:english"]
GDELT_SCOPES_RU = ["sourcecountry:KZ", "sourcelang:russian"]
GDELT_PAUSE_SEC = 8          # GDELT просит не чаще 1 запроса в 5 секунд; берём с запасом
GDELT_MAX_RETRIES = 6        # повторов при ответе 429 (ждём 15, 30, 60, 120, 240, 300 с)

# ---------------------------------------------------------------------------
# Сбор: Google News RSS (бесплатно, без ключа, ~100 результатов на запрос)
# ---------------------------------------------------------------------------
GNEWS_LOCALES_RU = [("ru", "KZ", "KZ:ru"), ("ru", "RU", "RU:ru")]
GNEWS_LOCALES_EN = [("en", "US", "US:en"), ("en", "GB", "GB:en")]
GNEWS_QUERIES_RU = [
    "отмывание денег", "легализация преступных доходов", "финансирование терроризма",
    "АФМ задержан", "АФМ уголовное дело", "обналичивание",
    "ТОО мошенничество", "ТОО хищение", "ТОО уголовное дело",
    "лжепредпринимательство", "санкции компания Казахстан", "обход санкций Казахстан",
    "взятка задержан", "контрабанда Казахстан", "финансовая пирамида",
    # казахский
    "қылмыстық жолмен алынған кірістерді заңдастыру", "қаржылық мониторинг агенттігі",
    "лаңкестікті қаржыландыру", "алаяқтық ЖШС",
]
GNEWS_QUERIES_EN = [
    "money laundering", "terrorist financing", "sanctions evasion",
    "OFAC sanctions company", "EU sanctions company", "fraud charged company",
    "embezzlement arrested", "bribery charges company", "shell companies laundering",
    "Kazakhstan money laundering", "Kazakhstan sanctions company",
    "Central Asia sanctions evasion",
]
GNEWS_PAUSE_SEC = 2

# ---------------------------------------------------------------------------
# Сбор: обычные RSS-ленты.
# ВНИМАНИЕ: адреса лент — кандидаты, проверить их из моей среды не получилось.
# Команда collect --rss выведет, какие ленты не открылись; поправьте их тут.
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    # Казахстан
    ("tengrinews.kz", "https://tengrinews.kz/news.rss"),
    ("kursiv.media", "https://kz.kursiv.media/feed/"),
    ("informburo.kz", "https://informburo.kz/rss"),
    ("zakon.kz", "https://www.zakon.kz/rss/"),
    ("inbusiness.kz", "https://inbusiness.kz/ru/rss"),
    # Международные расследования / регуляторы
    ("icij.org", "https://www.icij.org/feed/"),
    ("occrp.org", "https://www.occrp.org/en/feed"),
    ("justice.gov", "https://www.justice.gov/news/rss?type=press_release&m=1"),
]
# Сохранять из RSS только записи, где есть хотя бы одно ключевое слово
RSS_ONLY_WITH_KEYWORDS = True

# ---------------------------------------------------------------------------
# Сбор: Telegram-каналы. Список каналов и аккаунт задаются в панели
# (Telegram → Каналы / Аккаунт), здесь — только параметры сбора.
# ---------------------------------------------------------------------------
# Сохранять только посты, где есть хотя бы одно ключевое слово (как для RSS)
TELEGRAM_ONLY_WITH_KEYWORDS = True
# Не больше стольких постов с одного канала за запуск (защита от очень болтливых каналов)
TELEGRAM_MAX_POSTS_PER_CHANNEL = 2000
# Пауза между страницами веб-версии t.me/s (по 20 постов на странице)
TELEGRAM_WEB_PAUSE_SEC = 1.5
# Пауза между каналами при чтении через аккаунт — чтобы Telegram не ограничил аккаунт
TELEGRAM_ACCOUNT_PAUSE_SEC = 3
# Если Telegram просит подождать (FloodWait) не дольше стольких секунд — ждём молча
TELEGRAM_FLOOD_SLEEP_MAX = 120

HTTP_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Словари для разбора имён
# ---------------------------------------------------------------------------

# Правовые формы: выкидываются из названия перед поиском
LEGAL_PHRASES = [
    "товарищество с ограниченной ответственностью", "общество с ограниченной ответственностью",
    "акционерное общество", "индивидуальный предприниматель", "жауапкершілігі шектеулі серіктестік",
    "limited liability company", "limited liability partnership", "joint stock company",
    "private limited", "public limited company", "sp z o o", "s de rl", "s a s",
    "ltd sti", "san ve tic", "dis ticaret", "ic ve dis",
    "страховая компания", "микрофинансовая организация", "кредитное товарищество",
    "крестьянское хозяйство", "фермерское хозяйство", "частная компания",
    "компания специального назначения", "производственный кооператив", "потребительский кооператив",
]
LEGAL_TOKENS = [
    "ltd", "limited", "llc", "llp", "lp", "inc", "incorporated", "corp", "corporation",
    "co", "company", "gmbh", "ag", "kg", "ug", "sa", "sas", "sarl", "srl", "spa", "bv", "nv",
    "plc", "pvt", "pte", "bhd", "sdn", "jsc", "ojsc", "cjsc", "pjsc", "fze", "fzco", "fzc",
    "fzllc", "dmcc", "oy", "ab", "as", "aps", "kft", "sro", "zao", "oao", "pao", "ao", "ooo",
    "too", "ip", "tov", "doo", "dooel", "eood", "ood", "ltda", "sl", "sti", "tic", "mchj",
    "xk", "ojsc", "ltdco", "coltd", "chp", "kh", "jshs", "ttd",
    "тоо", "ооо", "ао", "ип", "зао", "оао", "пао", "чп", "кх", "жшс", "филиал",
    "компания", "фирма", "мфо", "пк", "kompaniya", "firma",
]

# Общие слова — по ним одним искать бессмысленно (шум)
GENERIC_WORDS = [
    "trade", "trading", "group", "global", "international", "intl", "holding", "holdings",
    "solutions", "solution", "services", "service", "logistics", "logistic", "import",
    "export", "imp", "exp", "and", "the", "of", "for", "industrial", "industry", "industries",
    "technology", "technologies", "tech", "systems", "system", "development", "invest",
    "investment", "investments", "capital", "management", "consulting", "partners",
    "enterprise", "enterprises", "commercial", "general", "union", "world", "asia", "euro",
    "europe", "central", "east", "west", "north", "south", "kazakhstan", "china", "russia",
    "best", "new", "plus", "pro", "star", "center", "centre", "market", "construction",
    "energy", "oil", "gas", "transport", "auto", "motors", "machinery", "equipment",
    "supply", "products", "manufacturing", "chemical", "foods", "food", "agro", "steel",
    "metal", "electric", "electronics", "digital", "media", "network", "bank", "finance",
    "financial", "insurance", "hong", "kong", "hk", "shanghai", "beijing",
    "shenzhen", "guangzhou", "dubai", "uae", "turkey", "istanbul", "almaty", "astana",
    "company", "firm", "corporate", "treasury", "notes", "bonds", "fund", "securities", "business", "office", "branch", "partner", "line",
    "торговый", "дом", "групп", "компания", "строй", "сервис", "транс", "логистик",
    "казахстан", "азия", "импорт", "экспорт", "алматы", "астана", "страховая",
    "trans", "stroy", "servis", "grupp", "kompaniya", "torgovyy", "dom",
]

# Окончания отчеств (после normalize)
PATRONYMIC_SUFFIXES = ("ovich", "evich", "ovna", "evna", "ichna", "ilich",
                       "uli", "kizi", "ogli", "oglu", "vich", "vna")
