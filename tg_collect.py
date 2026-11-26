"""
Сбор постов из Telegram-каналов.

Два способа чтения:
  * веб-версия https://t.me/s/<канал> — открытые каналы, без аккаунта;
  * через подключённый аккаунт (Telethon, «юзербот») — закрытые каналы,
    в которых состоит аккаунт, каналы по инвайт-ссылкам и открытые каналы,
    у которых веб-версия отключена.

Список каналов и данные аккаунта хранятся в базе (таблицы панели
screening_tgchannel и screening_tgaccount), посты пишутся в общую таблицу
articles с collector='telegram' и дальше идут через тот же матчинг.

Здесь же — функции входа в аккаунт, которые вызывает панель.
"""
import asyncio
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

import config
import keywords
from db import insert_article, now

CHANNELS_TABLE = "screening_tgchannel"
ACCOUNT_TABLE = "screening_tgaccount"
WEB_BASE = "https://t.me/s/"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Разбор ссылок на каналы
# ---------------------------------------------------------------------------
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", re.I)
# Служебные пути t.me, которые не являются каналами
_RESERVED = {"s", "c", "joinchat", "addstickers", "addemoji", "share", "proxy", "socks",
             "iv", "login", "setlanguage", "addtheme", "bg", "invoice", "boost", "contact"}


@dataclass
class ChannelRef:
    ref: str                     # ключ в базе: username / +hash / c/123
    username: str = ""
    invite_hash: str = ""
    channel_id: int | None = None

    @property
    def is_private(self) -> bool:
        return not self.username


def parse_channel_ref(raw: str, allow_bare: bool = True) -> ChannelRef | None:
    """Понимает @name, name, t.me/name, t.me/s/name, t.me/name/123,
    t.me/+HASH, t.me/joinchat/HASH, t.me/c/123456/7 и -100123456.
    allow_bare=False — голое слово без @ и t.me не считается каналом
    (в таблицах рядом со ссылками лежат названия, они не должны стать каналами).
    Возвращает None, если строка не похожа на канал."""
    s = (raw or "").strip().strip("\"'<>«»").strip()
    if not s:
        return None

    m = re.fullmatch(r"-100(\d{5,})", s)
    if m:
        cid = int(m.group(1))
        return ChannelRef(ref=f"c/{cid}", channel_id=cid)

    had_domain = bool(_DOMAIN_RE.match(s))
    had_at = s.startswith("@")
    s = _DOMAIN_RE.sub("", s)
    if had_at:
        s = s[1:]
    s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in s.split("/") if p]
    if not parts:
        return None
    if parts[0] == "s" and len(parts) > 1:
        parts = parts[1:]
    head = parts[0]

    if head.startswith("+") and _INVITE_RE.match(head[1:]):
        return ChannelRef(ref="+" + head[1:], invite_hash=head[1:])
    if head == "joinchat" and len(parts) > 1 and _INVITE_RE.match(parts[1]):
        return ChannelRef(ref="+" + parts[1], invite_hash=parts[1])
    if head == "c" and len(parts) > 1 and parts[1].isdigit():
        cid = int(parts[1])
        return ChannelRef(ref=f"c/{cid}", channel_id=cid)
    # голое слово без домена и без @ принимаем только если похоже на username
    if _USERNAME_RE.match(head) and head.lower() not in _RESERVED:
        if not had_domain and (len(parts) > 1 or not (had_at or allow_bare)):
            return None
        return ChannelRef(ref=head.lower(), username=head)
    return None


def extract_refs(text: str, allow_bare: bool = True) -> tuple[list[ChannelRef], list[str]]:
    """Достаёт ссылки на каналы из произвольного текста (по строке/слову).
    Возвращает (найденные без повторов, нераспознанные куски)."""
    found: dict[str, ChannelRef] = {}
    bad: list[str] = []
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip()
        if not token:
            continue
        r = parse_channel_ref(token, allow_bare)
        if r:
            found.setdefault(r.ref, r)
        else:
            bad.append(token)
    return list(found.values()), bad


def post_url(ref_username: str | None, channel_id: int | None, msg_id: int) -> str:
    if ref_username:
        return f"https://t.me/{ref_username}/{msg_id}"
    return f"https://t.me/c/{channel_id}/{msg_id}"


def make_title(text: str, limit: int = 160) -> str:
    """Первая непустая строка поста — как заголовок новости."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            if len(line) <= limit:
                return line
            cut = line[:limit].rsplit(" ", 1)[0]
            return (cut or line[:limit]) + "…"
    return ""


# ---------------------------------------------------------------------------
# Веб-версия t.me/s/<канал>
# ---------------------------------------------------------------------------
class NoWebPreview(Exception):
    """У канала нет веб-версии: он закрытый, не существует или превью отключено."""


@dataclass
class Post:
    msg_id: int
    date: datetime | None
    text: str
    summary: str = ""            # заголовок/описание ссылки из превью


_session = None


def _http():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = config.HTTP_USER_AGENT
        _session.headers["Accept-Language"] = "ru,en;q=0.8"
    return _session


def _el_text(el) -> str:
    # <br> -> перенос строки, иначе text_content() склеит абзацы
    for br in el.iter("br"):
        br.tail = "\n" + (br.tail or "")
    text = el.text_content()
    return "\n".join(re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()).strip()


def parse_web_page(html: bytes | str) -> tuple[str, list[Post]]:
    """Разбирает страницу t.me/s/<канал>: (название канала, посты)."""
    import lxml.html

    if isinstance(html, bytes):
        # t.me отдаёт UTF-8; без явного декодирования lxml может принять байты за latin-1
        html = html.decode("utf-8", errors="replace")
    doc = lxml.html.fromstring(html)
    if not doc.xpath('//*[contains(@class,"tgme_channel_info")]'):
        raise NoWebPreview("веб-версия канала недоступна")
    title = ""
    t = doc.xpath('//meta[@property="og:title"]/@content')
    if t:
        title = t[0].strip()

    posts = []
    for m in doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), '
                       '" tgme_widget_message ") and @data-post]'):
        try:
            msg_id = int(m.get("data-post").rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        body = m.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), '
                       '" js-message_text ")]')
        text = _el_text(body[0]) if body else ""
        prev = m.xpath('.//*[contains(@class,"link_preview_title") or '
                       'contains(@class,"link_preview_description")]')
        summary = " — ".join(x for x in (_el_text(p) for p in prev) if x)
        date = None
        d = m.xpath('.//a[contains(@class,"tgme_widget_message_date")]/time/@datetime')
        if d:
            try:
                date = datetime.fromisoformat(d[0]).astimezone(timezone.utc)
            except ValueError:
                pass
        posts.append(Post(msg_id, date, text, summary))
    return title, posts


def fetch_web_page(username: str, before: int | None = None) -> tuple[str, list[Post]]:
    url = WEB_BASE + username + (f"?before={before}" if before else "")
    for attempt in range(4):
        r = _http().get(url, timeout=30)
        if r.status_code == 429:
            wait = 20 * (attempt + 1)
            log(f"[tg] t.me просит притормозить (429), жду {wait} с")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        # закрытые/несуществующие каналы и каналы без превью t.me
        # перенаправляет с /s/<имя> на страницу-заглушку /<имя>
        if "/s/" not in r.url:
            raise NoWebPreview("веб-версия канала недоступна (закрытый, не существует "
                               "или превью отключено)")
        return parse_web_page(r.content)
    raise RuntimeError("t.me не отвечает (429)")


def iter_web_posts(username: str, cutoff: datetime, max_posts: int):
    """Посты канала от новых к старым, пока не дойдём до cutoff.
    Первым значением отдаёт (название канала, самый свежий пост или None)."""
    title, posts = fetch_web_page(username)
    yield title, max(posts, key=lambda x: x.msg_id) if posts else None
    seen = 0
    while posts:
        for p in sorted(posts, key=lambda x: -x.msg_id):
            if p.date and p.date < cutoff:
                return
            yield p
            seen += 1
            if seen >= max_posts:
                return
        oldest = min(p.msg_id for p in posts)
        if oldest <= 1:
            return
        time.sleep(config.TELEGRAM_WEB_PAUSE_SEC)
        _, posts = fetch_web_page(username, before=oldest)
        posts = [p for p in posts if p.msg_id < oldest]


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------
def _utc(dt: datetime | None) -> datetime | None:
    return dt.astimezone(timezone.utc) if dt else None


def _save_post(conn, ch, url: str, post: Post, source: str) -> tuple[bool, bool]:
    """(прошёл фильтр по ключевым словам, новый)"""
    if not (post.text or post.summary):
        return False, False
    full = "\n".join(x for x in (post.text, post.summary) if x)
    if config.TELEGRAM_ONLY_WITH_KEYWORDS and not keywords.has_any(full):
        return False, False
    title = make_title(post.text) or make_title(post.summary) or f"Пост {post.msg_id}"
    new = insert_article(
        conn, url=url, title=title, source=source, published=_utc(post.date),
        collector="telegram", query=ch["ref"], summary=post.summary or None,
        text=post.text or None, text_status="ok",
    )
    return True, new


def _update_channel(conn, ch_id: int, **f):
    f["last_checked_at"] = now()
    sets = ", ".join(f"{k}=%s" for k in f if k != "posts_add")
    vals = [v for k, v in f.items() if k != "posts_add"]
    if "posts_add" in f:
        sets += ", posts_saved = COALESCE(posts_saved, 0) + %s"
        vals.append(f["posts_add"])
    conn.execute(f"UPDATE {CHANNELS_TABLE} SET {sets} WHERE id=%s", (*vals, ch_id))
    conn.commit()


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]


def load_account(conn) -> dict | None:
    if not _table_exists(conn, ACCOUNT_TABLE):
        return None
    r = conn.execute(f"SELECT * FROM {ACCOUNT_TABLE} WHERE status='connected' "
                     "AND session != '' ORDER BY id LIMIT 1").fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Сбор: веб-версия
# ---------------------------------------------------------------------------
def _collect_web(conn, ch, cutoff) -> tuple[int, int, int]:
    """(просмотрено, с ключевыми словами, новых)"""
    username = ch["username"]
    it = iter_web_posts(username, cutoff, config.TELEGRAM_MAX_POSTS_PER_CHANNEL)
    title, newest = next(it)
    source = f"Telegram: {title or '@' + username}"
    seen = kept = new = 0
    top_id, top_date = ch["last_post_id"] or 0, None
    if newest and newest.msg_id > top_id:
        top_id, top_date = newest.msg_id, newest.date
    for post in it:
        seen += 1
        k, n = _save_post(conn, ch, post_url(username, None, post.msg_id), post, source)
        kept += k
        new += n
        if seen % 100 == 0:
            conn.commit()
    conn.commit()
    upd = dict(title=title or ch["title"], last_via="web", last_error="", posts_add=new,
               last_post_id=top_id or None)
    if top_date:
        upd["last_post_at"] = _utc(top_date)
    _update_channel(conn, ch["id"], **upd)
    return seen, kept, new


# ---------------------------------------------------------------------------
# Telethon: клиент, вход в аккаунт, чтение каналов
# ---------------------------------------------------------------------------
def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        return False


def _client(api_id, api_hash, session: str | None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or None), int(api_id), api_hash,
                          device_model="AML screening panel", system_version="1.0",
                          app_version="1.0", lang_code="ru", system_lang_code="ru",
                          flood_sleep_threshold=config.TELEGRAM_FLOOD_SLEEP_MAX)


def run(coro):
    """Выполнить корутину Telethon из обычного (синхронного) кода."""
    return asyncio.run(coro)


def describe_me(me) -> str:
    name = " ".join(x for x in (me.first_name, me.last_name) if x) or "без имени"
    extra = ", ".join(x for x in ((f"@{me.username}" if me.username else ""),
                                  (f"+{me.phone}" if me.phone else "")) if x)
    return f"{name} ({extra})" if extra else name


def explain_error(e: Exception) -> str:
    """Понятный текст для типовых ошибок Telegram."""
    from telethon import errors as E
    if isinstance(e, E.FloodWaitError):
        return f"Telegram просит подождать {e.seconds} с (слишком много запросов)"
    table = [
        (E.ApiIdInvalidError, "Неверные api_id / api_hash — проверьте на my.telegram.org"),
        (E.PhoneNumberInvalidError, "Неверный номер телефона (нужен формат +77001234567)"),
        (E.PhoneNumberBannedError, "Этот номер заблокирован в Telegram"),
        (E.PhoneCodeInvalidError, "Неверный код"),
        (E.PhoneCodeExpiredError, "Код устарел — запросите новый"),
        (E.PasswordHashInvalidError, "Неверный пароль двухэтапной проверки"),
        (E.AuthKeyUnregisteredError, "Сессия больше не действует — подключите аккаунт заново"),
        (E.SessionRevokedError, "Сессию завершили из настроек Telegram — подключите заново"),
        (E.UserDeactivatedError, "Аккаунт удалён или отключён"),
        (E.InviteHashExpiredError, "Инвайт-ссылка устарела"),
        (E.InviteHashInvalidError, "Инвайт-ссылка недействительна"),
        (E.ChannelPrivateError, "Канал закрыт, а аккаунт в нём не состоит (или его исключили)"),
        (E.UsernameNotOccupiedError, "Канала с таким именем нет"),
        (E.UsernameInvalidError, "Некорректное имя канала"),
        (E.InviteRequestSentError, "Отправлена заявка на вступление — ждём одобрения админом"),
        (E.ChannelsTooMuchError, "Аккаунт состоит в слишком большом числе каналов"),
    ]
    for cls, text in table:
        if isinstance(e, cls):
            return text
    return f"{type(e).__name__}: {e}"


async def send_code(api_id, api_hash, phone) -> tuple[str, str]:
    """Отправляет код входа. Возвращает (session, phone_code_hash)."""
    client = _client(api_id, api_hash, None)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return client.session.save(), sent.phone_code_hash
    finally:
        await client.disconnect()


async def sign_in_code(api_id, api_hash, session, phone, code, phone_code_hash):
    """Возвращает ('connected', session, me) или ('password', session, None),
    если на аккаунте включена двухэтапная проверка."""
    from telethon.errors import SessionPasswordNeededError
    client = _client(api_id, api_hash, session)
    await client.connect()
    try:
        try:
            me = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return "password", client.session.save(), None
        return "connected", client.session.save(), describe_me(me)
    finally:
        await client.disconnect()


async def sign_in_password(api_id, api_hash, session, password):
    client = _client(api_id, api_hash, session)
    await client.connect()
    try:
        me = await client.sign_in(password=password)
        return client.session.save(), describe_me(me)
    finally:
        await client.disconnect()


async def whoami(api_id, api_hash, session) -> str | None:
    client = _client(api_id, api_hash, session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return None
        return describe_me(await client.get_me())
    finally:
        await client.disconnect()


async def log_out(api_id, api_hash, session):
    client = _client(api_id, api_hash, session)
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
    finally:
        await client.disconnect()


async def my_channels(api_id, api_hash, session) -> list[dict]:
    """Каналы, в которых состоит аккаунт (только каналы, без групп и личных чатов)."""
    client = _client(api_id, api_hash, session)
    await client.connect()
    try:
        out = []
        async for d in client.iter_dialogs():
            ent = d.entity
            if not (d.is_channel and getattr(ent, "broadcast", False)):
                continue
            out.append({"id": ent.id, "username": ent.username or "", "title": d.name or ""})
        return out
    finally:
        await client.disconnect()


async def _resolve(client, ch, auto_join: bool):
    """Находит канал по записи из базы. Возвращает сущность Telethon."""
    from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
    from telethon.tl.types import ChatInviteAlready, ChatInvitePeek, PeerChannel

    if ch["channel_id"]:
        try:
            return await client.get_entity(PeerChannel(int(ch["channel_id"])))
        except (ValueError, TypeError):
            if not ch["invite_hash"] and not ch["username"]:
                raise RuntimeError("аккаунт не состоит в этом канале (или он удалён)")
    if ch["username"]:
        return await client.get_entity(ch["username"])
    if ch["invite_hash"]:
        info = await client(CheckChatInviteRequest(ch["invite_hash"]))
        if isinstance(info, (ChatInviteAlready, ChatInvitePeek)):
            return info.chat
        if not auto_join:
            raise RuntimeError("аккаунт не состоит в канале. Вступите по ссылке вручную или "
                               "включите «Вступать по инвайт-ссылкам» в настройках аккаунта")
        upd = await client(ImportChatInviteRequest(ch["invite_hash"]))
        log(f"[tg] {ch['ref']}: вступили в канал по инвайт-ссылке")
        return upd.chats[0]
    raise RuntimeError("не указан канал")


async def _collect_account(conn, acc: dict, channels: list, cutoff: datetime) -> int:
    client = _client(acc["api_id"], acc["api_hash"], acc["session"])
    await client.connect()
    total_new = 0
    try:
        if not await client.is_user_authorized():
            msg = "сессия аккаунта больше не действует — подключите аккаунт заново в панели"
            log(f"[tg] {msg}")
            conn.execute(f"UPDATE {ACCOUNT_TABLE} SET status='error', last_error=%s WHERE id=%s",
                         (msg, acc["id"]))
            conn.commit()
            for ch in channels:
                _update_channel(conn, ch["id"], last_error=msg)
            return 0
        # кэш сущностей: без него канал по одному числовому id не найти
        if any(ch["channel_id"] and not ch["username"] for ch in channels):
            await client.get_dialogs()

        for i, ch in enumerate(channels):
            if i:
                await asyncio.sleep(config.TELEGRAM_ACCOUNT_PAUSE_SEC)
            try:
                ent = await _resolve(client, ch, bool(acc.get("auto_join")))
                title = getattr(ent, "title", "") or ch["title"]
                username = getattr(ent, "username", None)
                source = f"Telegram: {title or ch['ref']}"
                seen = kept = new = 0
                top_id, top_date = ch["last_post_id"] or 0, None
                async for msg in client.iter_messages(
                        ent, limit=config.TELEGRAM_MAX_POSTS_PER_CHANNEL, wait_time=1):
                    if msg.date and msg.date < cutoff:
                        break
                    seen += 1
                    if msg.id > top_id:
                        top_id, top_date = msg.id, msg.date
                    post = Post(msg.id, msg.date, (msg.message or "").strip())
                    k, n = _save_post(conn, ch, post_url(username, ent.id, msg.id), post, source)
                    kept += k
                    new += n
                    if seen % 100 == 0:
                        conn.commit()
                conn.commit()
                upd = dict(title=title, channel_id=ent.id, last_via="account", last_error="",
                           posts_add=new, last_post_id=top_id or None)
                if top_date:
                    upd["last_post_at"] = _utc(top_date)
                _update_channel(conn, ch["id"], **upd)
                total_new += new
                log(f"[tg] {ch['ref']} (аккаунт): постов {seen}, с ключевыми словами {kept}, "
                    f"новых {new}")
            except Exception as e:  # один канал не должен ронять весь сбор
                err = error_text(e)
                log(f"[tg] {ch['ref']} (аккаунт): ОШИБКА {err}")
                _update_channel(conn, ch["id"], last_error=err)
    finally:
        await client.disconnect()
    return total_new


def probe_web(username: str) -> dict:
    """Быстрая проверка открытого канала: первая страница веб-версии."""
    title, posts = fetch_web_page(username)
    last = max(posts, key=lambda p: p.msg_id) if posts else None
    return {"title": title, "last_post_id": last.msg_id if last else None,
            "last_post_at": last.date if last else None}


async def probe_account(api_id, api_hash, session, auto_join: bool,
                        channels: list[dict]) -> list[dict | Exception]:
    """Проверка каналов через аккаунт: находим канал и его последний пост.
    Для каждого канала — словарь с данными или исключение."""
    client = _client(api_id, api_hash, session)
    await client.connect()
    out = []
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("сессия аккаунта больше не действует — подключите заново")
        if any(ch["channel_id"] and not ch["username"] for ch in channels):
            await client.get_dialogs()
        for ch in channels:
            try:
                ent = await _resolve(client, ch, auto_join)
                msgs = await client.get_messages(ent, limit=1)
                last = msgs[0] if msgs else None
                out.append({"title": getattr(ent, "title", "") or "", "channel_id": ent.id,
                            "last_post_id": last.id if last else None,
                            "last_post_at": last.date if last else None})
            except Exception as e:
                out.append(e)
    finally:
        await client.disconnect()
    return out


def error_text(e: Exception) -> str:
    if type(e).__module__.startswith("telethon"):
        return explain_error(e)
    return str(e) or type(e).__name__


# ---------------------------------------------------------------------------
# Точка входа для main.py collect --telegram
# ---------------------------------------------------------------------------
def collect_telegram(conn, days: int = 30) -> int:
    if not _table_exists(conn, CHANNELS_TABLE):
        log("[tg] таблицы каналов нет — запустите панель (start.bat) или "
            "`python manage.py migrate` и добавьте каналы")
        return 0
    channels = [dict(r) for r in conn.execute(
        f"SELECT * FROM {CHANNELS_TABLE} WHERE is_active ORDER BY id")]
    if not channels:
        log("[tg] нет включённых каналов — добавьте их в панели: Telegram → Каналы")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    acc = load_account(conn)
    log(f"[tg] каналов: {len(channels)}, за {days} дн.; аккаунт: "
        f"{acc['me'] or 'подключён' if acc else 'не подключён'}")

    total_new = 0
    via_account = []
    web = [ch for ch in channels if ch["username"] and ch["method"] != "account"]
    for ch in channels:
        if not ch["username"] or ch["method"] == "account":
            via_account.append(ch)
    for i, ch in enumerate(web):
        if i:
            time.sleep(config.TELEGRAM_WEB_PAUSE_SEC)
        try:
            seen, kept, new = _collect_web(conn, ch, cutoff)
            total_new += new
            log(f"[tg] @{ch['username']}: постов {seen}, с ключевыми словами {kept}, новых {new}")
        except NoWebPreview as e:
            if ch["method"] == "auto" and acc:
                log(f"[tg] @{ch['username']}: нет веб-версии, читаю через аккаунт")
                via_account.append(ch)
            else:
                hint = "" if acc else " Подключите аккаунт, чтобы читать такие каналы."
                log(f"[tg] @{ch['username']}: {e}.{hint}")
                _update_channel(conn, ch["id"], last_error=f"{e}.{hint}")
        except Exception as e:
            log(f"[tg] @{ch['username']}: ОШИБКА {type(e).__name__}: {e}")
            _update_channel(conn, ch["id"], last_error=f"{type(e).__name__}: {e}")

    if via_account:
        if not acc:
            msg = "нужен подключённый Telegram-аккаунт (панель: Telegram → Аккаунт)"
            for ch in via_account:
                log(f"[tg] {ch['ref']}: пропущен — {msg}")
                _update_channel(conn, ch["id"], last_error=msg)
        elif not telethon_available():
            log("[tg] не установлен telethon: pip install -r requirements.txt")
        else:
            total_new += run(_collect_account(conn, acc, via_account, cutoff))
    return total_new
