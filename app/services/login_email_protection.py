from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient
from telethon.errors import FloodError

from app.config import settings
from app.db.models import (
    AccountSecurity,
    LoginEmailProtectionEvent,
    LoginEmailWhitelist,
    RuntimeSetting,
    ServiceMessage,
    TgAccount,
)
from app.services.crypto import decrypt_text, encrypt_text
from app.tg import account_ops

logger = logging.getLogger(__name__)

SELECTED_DOMAIN_KEY = "login_email_protection.selected_domain"
DOMAINS_KEY = "login_email_protection.domains"
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
LOGIN_CODE_VALUE_PATTERN = re.compile(r"(?<!\d)\d{5,8}(?!\d)")
LOGIN_CODE_ALERT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:your\s+)?login\s+code\b",
        r"登录(?:验证)?码|登录代码",
        r"код\s+для\s+входа",
        r"c[oó]digo\s+(?:de\s+)?(?:inicio\s+de\s+sesi[oó]n|login)",
        r"code\s+de\s+connexion",
        r"anmeldecode",
        r"codice\s+(?:di\s+)?accesso",
        r"giri[sş]\s+kodu",
        r"رمز\s+تسجيل\s+الدخول",
        r"کد\s+ورود",
        r"kode\s+login",
        r"m[aã]\s+(?:đăng\s+nhập|login)",
        r"로그인\s*코드",
        r"ログインコード",
    )
)
EMAIL_BODY_CODE_PATTERN = re.compile(
    r"\byour\s+code\s+is\s*:\s*(\d{5,8})\b",
    re.IGNORECASE,
)
EMAIL_SUBJECT_CODE_PATTERN = re.compile(
    r"\byour\s+code\s*[-:]\s*(\d{5,8})\b",
    re.IGNORECASE,
)
EMAIL_LOGIN_PURPOSE_PATTERN = re.compile(
    r"verify\s+your\s+email\s+for\s+login",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TelegramLoginEmailCode:
    code: str
    recipient: str
    sent_at: datetime | None


@dataclass(frozen=True)
class LoginEmailWindowNotice:
    event_id: int
    window_ends_at: datetime
    window_hours: int
    alert_count: int
    starts_new_window: bool


def format_wait_deadline(seconds: float, *, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local_timezone = ZoneInfo("Asia/Shanghai")
    local_now = current.astimezone(local_timezone)
    deadline = (current + timedelta(seconds=max(0, seconds))).astimezone(local_timezone)
    if deadline.date() == local_now.date():
        return deadline.strftime("%H:%M:%S")
    return deadline.strftime("%m-%d %H:%M:%S")


def login_email_wait_remaining(
    requested_at: datetime | None,
    *,
    now: datetime | None = None,
) -> float:
    if requested_at is None:
        return float(settings.login_email_poll_timeout_seconds)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(
        0,
        (
            requested_at + timedelta(seconds=settings.login_email_poll_timeout_seconds) - current
        ).total_seconds(),
    )


def parse_login_email_window_hours(raw_hours: object) -> int:
    if isinstance(raw_hours, bool):
        raise ValueError("时长必须是 0–720 之间的整数小时")
    try:
        numeric_hours = float(raw_hours)
        hours = int(numeric_hours)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("时长必须是 0–720 之间的整数小时") from exc
    if not numeric_hours.is_integer() or not 0 <= hours <= 720:
        raise ValueError("时长必须是 0–720 之间的整数小时")
    return hours


def is_login_code_alert(text: str) -> bool:
    """Recognize the 777000 login-code alert without depending on its full wording."""
    value = text or ""
    return bool(
        LOGIN_CODE_VALUE_PATTERN.search(value)
        and any(pattern.search(value) for pattern in LOGIN_CODE_ALERT_PATTERNS)
    )


async def recover_incomplete_events(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> int:
    """Interrupt pre-email requests while preserving resumable email waits."""
    async with sessionmaker() as session:
        result = await session.execute(
            update(LoginEmailProtectionEvent)
            .where(LoginEmailProtectionEvent.status.in_({"detected", "requesting"}))
            .values(status="interrupted", error="服务曾异常停止，可从保护事件中重新发起换绑")
        )
        await session.commit()
    return int(result.rowcount or 0)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for fragment, charset in decode_header(value):
        if isinstance(fragment, bytes):
            parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts)


def _message_text(message: Message) -> str:
    if not message.is_multipart():
        payload = message.get_payload(decode=True)
        if payload is None:
            return str(message.get_payload() or "")
        return payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is not None:
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def parse_telegram_login_email(
    raw_message: bytes,
    target_email: str,
    expected_sender: str = "noreply@telegram.org",
) -> TelegramLoginEmailCode | None:
    """Return a code only when sender, recipient and Login purpose all match."""
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    senders = {address.lower() for _, address in getaddresses([message.get("From", "")]) if address}
    if expected_sender.lower() not in senders:
        return None

    # Newer Python patch releases reject the whole getaddresses() input when it
    # contains empty header values. Only pass headers that actually exist so a
    # normal To-only message remains valid with strict address parsing.
    recipient_headers = [
        value
        for name in ("To", "Delivered-To", "X-Original-To", "Envelope-To")
        if (value := message.get(name))
    ]
    recipients = {address.lower() for _, address in getaddresses(recipient_headers) if address}
    normalized_target = target_email.lower()
    if normalized_target not in recipients:
        return None

    subject = _decode_header(message.get("Subject"))
    body = _message_text(message)
    combined = f"{subject}\n{body}"
    if not EMAIL_LOGIN_PURPOSE_PATTERN.search(combined):
        return None
    body_match = EMAIL_BODY_CODE_PATTERN.search(body)
    subject_match = EMAIL_SUBJECT_CODE_PATTERN.search(subject)
    if body_match is None:
        return None
    if subject_match is not None and subject_match.group(1) != body_match.group(1):
        return None

    sent_at: datetime | None = None
    try:
        sent_at = parsedate_to_datetime(message.get("Date", ""))
        if sent_at is not None and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        sent_at = None
    return TelegramLoginEmailCode(body_match.group(1), normalized_target, sent_at)


class GmailCodeReader:
    def validate_connection_sync(self) -> None:
        connection = imaplib.IMAP4_SSL(
            settings.login_email_imap_host,
            settings.login_email_imap_port,
            timeout=30,
        )
        try:
            connection.login(
                settings.login_email_gmail_username,
                settings.login_email_gmail_app_password,
            )
            status, _ = connection.select(settings.login_email_imap_folder, readonly=True)
            if status != "OK":
                raise RuntimeError("无法打开 Gmail IMAP 邮箱目录")
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    async def validate_connection(self) -> None:
        await asyncio.to_thread(self.validate_connection_sync)

    def wait_for_code_sync(
        self,
        target_email: str,
        requested_at: datetime,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        timeout = (
            settings.login_email_poll_timeout_seconds
            if timeout_seconds is None
            else max(0, timeout_seconds)
        )
        deadline = time.monotonic() + timeout
        earliest = requested_at.astimezone(UTC) - timedelta(minutes=2)
        since = earliest.strftime("%d-%b-%Y")
        connection = imaplib.IMAP4_SSL(
            settings.login_email_imap_host,
            settings.login_email_imap_port,
            timeout=30,
        )
        seen_uids: set[bytes] = set()
        try:
            connection.login(
                settings.login_email_gmail_username,
                settings.login_email_gmail_app_password,
            )
            while time.monotonic() < deadline and not (
                cancel_event is not None and cancel_event.is_set()
            ):
                status, _ = connection.select(settings.login_email_imap_folder, readonly=True)
                if status != "OK":
                    raise RuntimeError("无法打开 Gmail IMAP 邮箱目录")
                status, data = connection.uid(
                    "search",
                    None,
                    f'(FROM "{settings.login_email_sender}" SINCE "{since}")',
                )
                if status != "OK":
                    raise RuntimeError("Gmail IMAP 搜索失败")
                uids = (data[0] or b"").split()
                for uid in reversed(uids[-100:]):
                    if uid in seen_uids:
                        continue
                    status, fetched = connection.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK":
                        continue
                    raw = next(
                        (
                            item[1]
                            for item in fetched
                            if isinstance(item, tuple) and isinstance(item[1], bytes)
                        ),
                        None,
                    )
                    if raw is None:
                        continue
                    seen_uids.add(uid)
                    parsed = parse_telegram_login_email(
                        raw,
                        target_email,
                        settings.login_email_sender,
                    )
                    if parsed is None:
                        continue
                    if parsed.sent_at is not None and parsed.sent_at.astimezone(UTC) < earliest:
                        continue
                    return parsed.code
                if cancel_event is None:
                    time.sleep(settings.login_email_poll_interval_seconds)
                elif cancel_event.wait(settings.login_email_poll_interval_seconds):
                    break
        finally:
            try:
                connection.logout()
            except Exception:
                pass
        raise TimeoutError("等待 Telegram 登录邮箱验证码超时")

    async def wait_for_code(
        self,
        target_email: str,
        requested_at: datetime,
        timeout_seconds: float | None = None,
    ) -> str:
        cancel_event = threading.Event()
        try:
            return await asyncio.to_thread(
                self.wait_for_code_sync,
                target_email,
                requested_at,
                timeout_seconds,
                cancel_event,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise


def normalize_domain(domain: str) -> str:
    normalized = domain.strip().lower().lstrip("@")
    if not DOMAIN_PATTERN.fullmatch(normalized):
        raise ValueError("邮箱域名格式无效")
    return normalized


async def get_available_domains(session: AsyncSession) -> tuple[str, ...]:
    row = await session.get(RuntimeSetting, DOMAINS_KEY)
    if row is not None:
        try:
            values = json.loads(row.value)
            domains = tuple(normalize_domain(str(item)) for item in values)
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("Stored login email domain list is invalid; using environment defaults")
        else:
            if domains and len(domains) == len(set(domains)):
                return domains
    return settings.login_email_alias_domains


async def _store_available_domains(session: AsyncSession, domains: tuple[str, ...]) -> None:
    if not domains:
        raise ValueError("至少需要保留一个邮箱域名")
    row = await session.get(RuntimeSetting, DOMAINS_KEY)
    payload = json.dumps(domains, ensure_ascii=True)
    if row is None:
        session.add(RuntimeSetting(key=DOMAINS_KEY, value=payload))
    else:
        row.value = payload


async def add_available_domain(session: AsyncSession, domain: str) -> str:
    normalized = normalize_domain(domain)
    domains = await get_available_domains(session)
    if normalized in domains:
        raise ValueError("该邮箱域名已经存在")
    await _store_available_domains(session, (*domains, normalized))
    await session.commit()
    return normalized


async def delete_available_domain(session: AsyncSession, domain: str) -> None:
    normalized = normalize_domain(domain)
    domains = await get_available_domains(session)
    if normalized not in domains:
        raise ValueError("该邮箱域名不存在")
    remaining = tuple(item for item in domains if item != normalized)
    if not remaining:
        raise ValueError("至少需要保留一个邮箱域名")
    await _store_available_domains(session, remaining)
    selected = await session.get(RuntimeSetting, SELECTED_DOMAIN_KEY)
    if selected is not None and selected.value == normalized:
        selected.value = remaining[0]
    await session.commit()


async def get_selected_domain(session: AsyncSession) -> str | None:
    domains = await get_available_domains(session)
    if not domains:
        return None
    row = await session.get(RuntimeSetting, SELECTED_DOMAIN_KEY)
    if row is not None and row.value in domains:
        return row.value
    return domains[0]


async def set_selected_domain(session: AsyncSession, domain: str) -> None:
    domain = normalize_domain(domain)
    if domain not in await get_available_domains(session):
        raise ValueError("邮箱域名不在当前允许列表中")
    row = await session.get(RuntimeSetting, SELECTED_DOMAIN_KEY)
    if row is None:
        session.add(RuntimeSetting(key=SELECTED_DOMAIN_KEY, value=domain))
    else:
        row.value = domain
    await session.commit()


async def get_whitelist_ids(session: AsyncSession) -> set[int]:
    return set((await session.scalars(select(LoginEmailWhitelist.account_id))).all())


async def set_whitelisted(session: AsyncSession, account_id: int, enabled: bool) -> None:
    account = await session.get(TgAccount, account_id)
    if account is None:
        raise ValueError("账号不存在")
    if enabled:
        if await session.get(LoginEmailWhitelist, account_id) is None:
            session.add(LoginEmailWhitelist(account_id=account_id))
    else:
        await session.execute(
            delete(LoginEmailWhitelist).where(LoginEmailWhitelist.account_id == account_id)
        )
    await session.commit()


def build_alias(phone: str, domain: str, timestamp: int | None = None) -> str:
    digits = "".join(character for character in phone if character.isdigit())
    if not digits:
        raise ValueError("账号手机号不可用")
    local_part = f"{timestamp or int(time.time())}_{digits}"
    if len(local_part) > 64:
        raise ValueError("生成的邮箱前缀超过 64 字符")
    return f"{local_part}@{domain}"


class LoginEmailProtector:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        reader: GmailCodeReader | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.reader = reader or GmailCodeReader()
        self._account_locks: dict[int, asyncio.Lock] = {}
        self._change_locks: dict[int, asyncio.Lock] = {}
        self._window_waiters: dict[int, asyncio.Task[None]] = {}
        self._gmail_slots = asyncio.Semaphore(3)

    def has_window_waiter(self, event_id: int) -> bool:
        task = self._window_waiters.get(event_id)
        return task is not None and not task.done()

    def has_change_in_progress(self, account_id: int) -> bool:
        lock = self._change_locks.get(account_id)
        return lock is not None and lock.locked()

    async def cancel_account_tasks(self, account_id: int, event_ids: set[int]) -> None:
        """Cancel protection waiters and forget per-account synchronization state."""
        tasks = {
            task
            for event_id, task in self._window_waiters.items()
            if event_id in event_ids and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event_id in event_ids:
            self._window_waiters.pop(event_id, None)
        self._account_locks.pop(account_id, None)
        self._change_locks.pop(account_id, None)

    async def _notify(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        for admin_id in settings.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, reply_markup=reply_markup)
            except TelegramAPIError:
                logger.warning(
                    "Failed to send login email protection notice to %s", admin_id, exc_info=True
                )
            except Exception:
                logger.exception("Unexpected login email protection notification failure")

    @staticmethod
    def _retry_markup(event_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="快捷换绑登录邮箱",
                        callback_data=f"emailguard:retrymenu:{event_id}",
                    )
                ]
            ]
        )

    async def _set_event_status(
        self,
        event_id: int,
        status: str,
        *,
        error: str | None = None,
        email_requested_at: datetime | None = None,
        confirmed_at: datetime | None = None,
    ) -> None:
        async with self.sessionmaker() as session:
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None:
                return
            event.status = status
            event.error = error[:2000] if error else None
            if email_requested_at is not None:
                event.email_requested_at = email_requested_at
            if confirmed_at is not None:
                event.confirmed_at = confirmed_at
            await session.commit()

    async def handle(
        self,
        account_id: int,
        service_message_id: int,
        text: str,
        client: TelegramClient,
    ) -> None:
        if not is_login_code_alert(text):
            return
        notice = await self.record_alert(account_id, service_message_id)
        if notice is not None and notice.starts_new_window:
            await self.wait_for_window(notice.event_id, client)

    async def record_alert(
        self,
        account_id: int,
        service_message_id: int,
    ) -> LoginEmailWindowNotice | None:
        lock = self._account_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            return await self._record_alert_locked(account_id, service_message_id)

    async def _record_alert_locked(
        self,
        account_id: int,
        service_message_id: int,
    ) -> LoginEmailWindowNotice | None:
        async with self.sessionmaker() as session:
            existing = await session.scalar(
                select(LoginEmailProtectionEvent).where(
                    LoginEmailProtectionEvent.service_message_id == service_message_id
                )
            )
            if existing is not None:
                return None
            service_message = await session.get(ServiceMessage, service_message_id)
            if service_message is None:
                return None
            alert_at = service_message.received_at
            if alert_at.tzinfo is None:
                alert_at = alert_at.replace(tzinfo=UTC)
            account = await session.get(TgAccount, account_id)
            if account is None:
                return None
            whitelisted = await session.get(LoginEmailWhitelist, account_id) is not None
            domain = await get_selected_domain(session)
            event = LoginEmailProtectionEvent(
                account_id=account_id,
                service_message_id=service_message_id,
                status="detected",
                selected_domain=domain,
                detected_at=alert_at,
                last_detected_at=alert_at,
                alert_count=1,
            )
            if whitelisted:
                event.status = "whitelisted"
                session.add(event)
                await session.commit()
                await self._notify(
                    f"登录邮箱保护\n账号 #{account_id} 在白名单中：仅转发 777000 登录提醒，未更改登录邮箱。"
                )
                return None
            if not settings.login_email_protection_enabled:
                event.status = "disabled"
                session.add(event)
                await session.commit()
                await self._notify(
                    f"登录邮箱保护\n账号 #{account_id} 检测到登录提醒，但自动换绑尚未启用。"
                )
                return None
            active_window = await session.scalar(
                select(LoginEmailProtectionEvent)
                .where(
                    LoginEmailProtectionEvent.account_id == account_id,
                    LoginEmailProtectionEvent.parent_event_id.is_(None),
                    or_(
                        LoginEmailProtectionEvent.status.in_({"requesting", "waiting_email"}),
                        and_(
                            LoginEmailProtectionEvent.status == "waiting_window",
                            LoginEmailProtectionEvent.detected_at <= alert_at,
                            LoginEmailProtectionEvent.window_ends_at > alert_at,
                        ),
                    ),
                )
                .order_by(LoginEmailProtectionEvent.window_ends_at.desc())
                .limit(1)
            )
            if active_window is not None:
                event.status = "merged"
                event.parent_event_id = active_window.id
                event.window_ends_at = active_window.window_ends_at
                session.add(event)
                active_window.alert_count += 1
                if (
                    active_window.last_detected_at is None
                    or alert_at > active_window.last_detected_at
                ):
                    active_window.last_detected_at = alert_at
                await session.commit()
                return LoginEmailWindowNotice(
                    event_id=active_window.id,
                    window_ends_at=active_window.window_ends_at,
                    window_hours=max(
                        0,
                        round(
                            (
                                active_window.window_ends_at - active_window.detected_at
                            ).total_seconds()
                            / 3600
                        ),
                    ),
                    alert_count=active_window.alert_count,
                    starts_new_window=False,
                )

            window_hours = max(
                0,
                min(int(getattr(account, "login_email_window_hours", 0) or 0), 720),
            )
            event.status = "waiting_window"
            event.window_ends_at = alert_at + timedelta(hours=window_hours)
            session.add(event)
            await session.flush()
            event_id = event.id
            await session.commit()
            return LoginEmailWindowNotice(
                event_id=event_id,
                window_ends_at=event.window_ends_at,
                window_hours=window_hours,
                alert_count=event.alert_count,
                starts_new_window=True,
            )

    async def wait_for_window(
        self,
        event_id: int,
        client: TelegramClient,
    ) -> None:
        current_task = asyncio.current_task()
        if current_task is None:
            return
        existing_waiter = self._window_waiters.get(event_id)
        if existing_waiter is not None and existing_waiter is not current_task:
            return
        self._window_waiters[event_id] = current_task
        try:
            while True:
                async with self.sessionmaker() as session:
                    event = await session.get(LoginEmailProtectionEvent, event_id)
                    if event is None or event.status != "waiting_window":
                        return
                    window_ends_at = event.window_ends_at
                    account_id = event.account_id
                if window_ends_at is None:
                    await self._set_event_status(event_id, "failed", error="聚合窗口结束时间缺失")
                    return
                if window_ends_at.tzinfo is None:
                    window_ends_at = window_ends_at.replace(tzinfo=UTC)
                delay = (window_ends_at - datetime.now(UTC)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue

                change_lock = self._change_locks.setdefault(account_id, asyncio.Lock())
                async with change_lock:
                    lock = self._account_locks.setdefault(account_id, asyncio.Lock())
                    async with lock:
                        prepared = await self._prepare_window_change(event_id)
                    if prepared is None:
                        return
                    account_id, target_email, domain = prepared
                    await self._execute_change(event_id, account_id, target_email, domain, client)
                return
        finally:
            if self._window_waiters.get(event_id) is current_task:
                self._window_waiters.pop(event_id, None)

    async def _prepare_window_change(
        self,
        event_id: int,
    ) -> tuple[int, str, str] | None:
        async with self.sessionmaker() as session:
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None or event.status != "waiting_window":
                return None
            window_ends_at = event.window_ends_at
            if window_ends_at is None:
                event.status = "failed"
                event.error = "聚合窗口结束时间缺失"
                await session.commit()
                return None
            if window_ends_at.tzinfo is None:
                window_ends_at = window_ends_at.replace(tzinfo=UTC)
            if window_ends_at > datetime.now(UTC):
                return None
            account = await session.get(TgAccount, event.account_id)
            if account is None:
                event.status = "failed"
                event.error = "账号不存在"
                await session.commit()
                return None
            domain = await get_selected_domain(session)
            if domain is None:
                event.status = "failed"
                event.error = "未配置登录邮箱域名"
                await session.commit()
                await self._notify(
                    f"登录邮箱保护汇总失败\n账号 #{event.account_id}\n原因：未配置邮箱域名。"
                )
                return None
            target_email = build_alias(decrypt_text(account.phone_encrypted), domain)
            event.selected_domain = domain
            event.target_email_encrypted = encrypt_text(target_email)
            event.status = "requesting"
            event.attempt_count += 1
            await session.commit()
            return event.account_id, target_email, domain

    async def _window_summary(self, event_id: int) -> str:
        async with self.sessionmaker() as session:
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None:
                return "窗口内登录提醒：未知"
            first_at = event.detected_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            last_at = (
                (event.last_detected_at or event.detected_at)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S %Z")
            )
            duration_seconds = 0
            if event.window_ends_at is not None:
                duration_seconds = max(
                    0,
                    int((event.window_ends_at - event.detected_at).total_seconds()),
                )
            window_label = (
                "即时保护触发的登录提醒"
                if duration_seconds == 0
                else f"{duration_seconds // 3600} 小时窗口内登录提醒"
            )
            return f"{window_label}：{event.alert_count} 次\n首次：{first_at}\n最后：{last_at}"

    async def retry(
        self,
        event_id: int,
        domain: str,
        client: TelegramClient,
    ) -> None:
        domain = normalize_domain(domain)
        async with self.sessionmaker() as session:
            if domain not in await get_available_domains(session):
                raise ValueError("邮箱域名不在当前允许列表中")
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None:
                raise ValueError("保护事件不存在")
            account_id = event.account_id
        change_lock = self._change_locks.setdefault(account_id, asyncio.Lock())
        if change_lock.locked():
            raise ValueError("该账号正在等待换绑邮件，请勿重复请求验证码")
        async with change_lock:
            lock = self._account_locks.setdefault(account_id, asyncio.Lock())
            async with lock:
                async with self.sessionmaker() as session:
                    event = await session.get(LoginEmailProtectionEvent, event_id)
                    if event is None:
                        raise ValueError("保护事件不存在")
                    if event.status in {"requesting", "waiting_email"}:
                        raise ValueError("该账号正在等待换绑邮件，请勿重复请求验证码")
                    account = await session.get(TgAccount, account_id)
                    if account is None:
                        raise ValueError("账号不存在")
                    target_email = build_alias(decrypt_text(account.phone_encrypted), domain)
                    event.selected_domain = domain
                    event.target_email_encrypted = encrypt_text(target_email)
                    event.status = "requesting"
                    event.error = None
                    event.email_requested_at = None
                    event.confirmed_at = None
                    event.attempt_count += 1
                    await session.commit()
            await self._notify(
                f"已开始快捷换绑\n账号：#{account_id}\n目标域名：@{domain}\n"
                "系统将等待邮件转发完成，期间请勿重复请求验证码。"
            )
            await self._execute_change(event_id, account_id, target_email, domain, client)

    @staticmethod
    def _friendly_failure_reason(exc: Exception) -> str:
        if isinstance(exc, FloodError):
            seconds = int(getattr(exc, "seconds", 0) or 0)
            if seconds:
                return (
                    f"Telegram 限制尝试次数，请在 {format_wait_deadline(seconds)} 后再试；"
                    "本次未发送验证码，请勿连续重试"
                )
            return "Telegram 限制尝试次数，请稍后再试；本次未发送验证码，请勿连续重试"
        if isinstance(exc, TimeoutError):
            minutes = max(1, settings.login_email_poll_timeout_seconds // 60)
            return (
                f"卡点：Telegram 已受理发码，但 Gmail 在 {minutes} 分钟内未收到验证码邮件。"
                "请打开 Cloudflare Email Routing 控制台 → Activity，查看目标地址的"
                "投递记录；若出现 Gmail 421/4.7.28，表示 Gmail 正在临时限流。"
                "本次请求未自动重发，请等待投递恢复后再手动重试"
            )
        return str(exc)

    async def resume_waiting_email(
        self,
        event_id: int,
        client: TelegramClient,
    ) -> None:
        async with self.sessionmaker() as session:
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None or event.status != "waiting_email":
                return
            if event.target_email_encrypted is None or event.email_requested_at is None:
                event.status = "failed"
                event.error = "等待邮件记录不完整，无法恢复原换绑流程"
                account_id = event.account_id
                await session.commit()
                await self._notify(
                    "登录邮箱保护汇总：换绑失败\n"
                    f"账号：#{account_id}\n"
                    "原因：等待邮件记录不完整，无法恢复原换绑流程。"
                )
                return
            account_id = event.account_id
            target_email = decrypt_text(event.target_email_encrypted)
            domain = event.selected_domain or target_email.rsplit("@", 1)[-1]
            requested_at = event.email_requested_at
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=UTC)
        expires_at = requested_at + timedelta(seconds=settings.login_email_poll_timeout_seconds)
        remaining = max(0, (expires_at - datetime.now(UTC)).total_seconds())
        logger.info(
            "Resuming original login email wait for event %s account %s with %.0f seconds remaining",
            event_id,
            account_id,
            remaining,
        )
        change_lock = self._change_locks.setdefault(account_id, asyncio.Lock())
        if change_lock.locked():
            return
        async with change_lock:
            async with self.sessionmaker() as session:
                current = await session.get(LoginEmailProtectionEvent, event_id)
                if current is None or current.status != "waiting_email":
                    return
            await self._execute_change(
                event_id,
                account_id,
                target_email,
                domain,
                client,
                requested_at=requested_at,
                wait_timeout_seconds=remaining,
            )

    async def _execute_change(
        self,
        event_id: int,
        account_id: int,
        target_email: str,
        domain: str,
        client: TelegramClient,
        *,
        requested_at: datetime | None = None,
        wait_timeout_seconds: float | None = None,
    ) -> None:
        summary = await self._window_summary(event_id)
        try:
            if requested_at is None:
                await account_ops.send_login_email_code(client, target_email)
                requested_at = datetime.now(UTC)
                logger.info(
                    "Requested one login email verification code for event %s account %s",
                    event_id,
                    account_id,
                )
                await self._set_event_status(
                    event_id,
                    "waiting_email",
                    email_requested_at=requested_at,
                )
            async with self._gmail_slots:
                code = await self.reader.wait_for_code(
                    target_email,
                    requested_at,
                    wait_timeout_seconds,
                )
            result = await account_ops.confirm_login_email(client, code)
            confirmed_email = str(getattr(result, "email", "") or "")
            if confirmed_email and confirmed_email.lower() != target_email.lower():
                raise RuntimeError("Telegram 返回的确认邮箱与目标邮箱不一致")
            confirmed_at = datetime.now(UTC)
            async with self.sessionmaker() as session:
                security = await session.get(AccountSecurity, account_id)
                if security is None:
                    security = AccountSecurity(account_id=account_id, has_2fa=False)
                    session.add(security)
                security.login_email_encrypted = encrypt_text(target_email)
                event = await session.get(LoginEmailProtectionEvent, event_id)
                if event is not None:
                    event.status = "succeeded"
                    event.error = None
                    event.confirmed_at = confirmed_at
                await session.commit()
            await self._notify(
                "登录邮箱保护汇总：换绑成功\n"
                f"账号：#{account_id}\n"
                f"{summary}\n"
                f"新登录邮箱：{target_email}\n"
                f"完成时间：{confirmed_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
            )
        except asyncio.CancelledError:
            async with self.sessionmaker() as session:
                event = await session.get(LoginEmailProtectionEvent, event_id)
                if event is not None and event.status == "waiting_email":
                    event.error = "服务重启后将继续等待原验证码邮件，不会重复发码"
                    await session.commit()
                elif event is not None:
                    event.status = "interrupted"
                    event.error = "服务停止，流程被中断"
                    await session.commit()
            raise
        except Exception as exc:
            logger.exception("Login email protection failed for account %s", account_id)
            reason = self._friendly_failure_reason(exc)
            await self._set_event_status(event_id, "failed", error=reason)
            await self._notify(
                "登录邮箱保护汇总：换绑失败\n"
                f"账号：#{account_id}\n"
                f"{summary}\n"
                f"本次域名：@{domain}\n"
                f"原因：{reason[:1000]}\n\n"
                "可点击下方按钮选择其他已配置域名重试。",
                reply_markup=self._retry_markup(event_id),
            )
