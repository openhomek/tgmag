from __future__ import annotations

import io
import shlex
import asyncio
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.errors import (
    FloodWaitError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
)
from telethon import functions

from app.bot.auth import AdminOnlyMiddleware
from app.bot.formatting import COMMANDS, account_line
from app.bot.keyboards import (
    account_actions_panel,
    accounts_panel,
    avatar_panel,
    batch_panel,
    bot_menu_button,
    cancel_inline,
    force_reply,
    login_email_account_panel,
    login_email_delete_confirm_panel,
    login_email_domains_panel,
    login_email_events_panel,
    login_email_guard_panel,
    login_email_retry_panel,
    login_email_whitelist_panel,
    login_phone_panel,
    main_menu,
    mini_app_panel,
    monitor_panel,
    post_login_security_panel,
    privacy_keys_panel,
    privacy_rules_panel,
    profile_edit_panel,
    settings_panel,
    twofa_panel,
)
from app.bot.states import (
    ExportSessionFlow,
    ImportSessionFlow,
    ImportSessionsFlow,
    LoginFlow,
    LoginEmailDomainFlow,
    LoginEmailWindowFlow,
    ProfileEditFlow,
    TwoFAEditFlow,
)
from app.config import settings
from app.db.models import (
    AccountSecurity,
    Admin,
    AllowedTarget,
    PrivacySettings,
    RateLimit,
    SpamCheck,
    TgAccount,
    TgSession,
    Job,
    LoginEmailProtectionEvent,
    LoginEmailWhitelist,
)
from app.services.crypto import decrypt_text
from app.services.audit import audit
from app.services.backups import create_database_backup_async
from app.services.jobs import add_job_item, create_job, finish_job
from app.services.login_email_protection import (
    add_available_domain,
    delete_available_domain,
    format_wait_deadline,
    get_available_domains,
    get_selected_domain,
    get_whitelist_ids,
    login_email_wait_remaining,
    parse_login_email_window_hours,
    set_selected_domain,
    set_whitelisted,
)
from app.services.pagination import ACCOUNT_PAGE_SIZE, account_page_window
from app.services.qr_code import login_qr_png
from app.services.rate_limit import RateGate, get_rate, validate_rate_values
from app.services.security_health import run_security_health_check
from app.services.targets import canonicalize_target_ref, require_allowed_target
from app.tg import account_ops, batch_ops
from app.tg.client_pool import ClientPool

router = Router()
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


MENU_TEXTS = {
    "系统状态": "status",
    "账号管理": "accounts",
    "登录账号": "login",
    "扫码登录": "qr_login",
    "导入Session": "import_session",
    "批量导入Session": "import_sessions",
    "导出Session": "export_session",
    "批量任务": "batch",
    "目标与速率": "settings",
    "安全防护": "security",
    "监控中心": "monitor",
    "内置应用": "mini_app",
    "帮助": "help",
}

CANCEL_TEXTS = {"取消", "取消当前操作", "/cancel"}

TEMPLATES = {
    "send": "/send <账号ID> <授权目标> <文本>",
    "subscribe": "/subscribe <账号ID> <授权目标>",
    "react": "/react <账号ID> <授权目标> <消息ID> <emoji>",
    "view_post": "/view_post <账号ID> <授权目标> <消息ID>",
    "forward": "/forward <账号ID> <源目标> <消息ID> <接收目标>",
    "target_add": "/target_allowlist add channel @your_test_channel 测试频道",
    "rate_set": "/rate set batch 5 60 2 6",
}

BOT_COMMANDS = [
    BotCommand(command="start", description="打开主菜单"),
    BotCommand(command="menu", description="重新显示主菜单键盘"),
    BotCommand(command="help", description="查看使用帮助"),
    BotCommand(command="status", description="查看系统状态"),
    BotCommand(command="accounts", description="管理 Telegram 账号"),
    BotCommand(command="login", description="登录并添加账号"),
    BotCommand(command="qr_login", description="扫码登录并添加账号"),
    BotCommand(command="app", description="打开内置管理应用"),
    BotCommand(command="settings", description="目标白名单与速率设置"),
    BotCommand(command="security", description="登录邮箱与账号安全防护"),
    BotCommand(command="cancel", description="取消当前操作"),
]


async def configure_bot_ui(bot: Bot) -> None:
    """Publish commands and a native Mini App launcher to Telegram clients."""
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_chat_menu_button(menu_button=bot_menu_button())

RANDOM_AVATAR_URLS = [
    "https://api.btstu.cn/sjbz/api.php?lx=dongman&format=images",
    "https://api.btstu.cn/sjbz/api.php?lx=meizi&format=images",
    "https://img.xjh.me/random_img.php?return=302&type=bg&ctype=acg",
    "https://picsum.photos/1200/1200.jpg",
    "https://picsum.photos/1024/1024.jpg",
]
MAX_IMAGE_SIZE = 5 * 1024 * 1024

class EmailCodeRequired(Exception):
    def __init__(self, code_length: int):
        self.code_length = code_length
        super().__init__(f"email confirmation code required: {code_length}")


def split_hint_email(parts: list[str], hint_index: int) -> tuple[str | None, str | None]:
    if len(parts) <= hint_index:
        return None, None
    tail = parts[hint_index:]
    email = None
    if tail and "@" in tail[-1]:
        email = tail.pop()
    hint = " ".join(tail) or None
    return hint, email


def require_email_code(code_length: int) -> str:
    raise EmailCodeRequired(code_length)


def args(message: Message) -> list[str]:
    text = message.text or ""
    try:
        return shlex.split(text)[1:]
    except ValueError:
        return text.split()[1:]


async def get_account(session: AsyncSession, account_id: int) -> TgAccount:
    account = await session.get(TgAccount, account_id)
    if not account:
        raise ValueError(f"账号 {account_id} 不存在")
    return account


async def resolve_account_id(session: AsyncSession, raw_id: str | int) -> int:
    value = int(raw_id)
    if -(2**31) <= value <= 2**31 - 1:
        account = await session.get(TgAccount, value)
        if account is not None:
            return account.id
    account = await session.scalar(select(TgAccount).where(TgAccount.user_id == value))
    if account is not None:
        return account.id
    raise ValueError(f"账号 {value} 不存在。本命令支持本地账号ID或 Telegram ID。")


async def account_ids_from_range(session: AsyncSession, start_id: int, count: int) -> list[int]:
    if start_id <= 0 or not 1 <= count <= 200:
        raise ValueError("start_id 必须为正整数，count 必须在 1 到 200 之间")
    rows = await session.scalars(
        select(TgAccount.id)
        .join(TgSession, TgSession.account_id == TgAccount.id)
        .where(TgAccount.id >= start_id, TgSession.is_active.is_(True))
        .order_by(TgAccount.id)
        .limit(count)
    )
    return list(rows.all())


def parse_account_selection(value: str) -> list[int]:
    tokens = [token.strip() for token in value.replace("，", ",").replace(" ", ",").split(",")]
    account_ids: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start_id, end_id = int(start_raw), int(end_raw)
            if start_id > end_id:
                start_id, end_id = end_id, start_id
            values = range(start_id, end_id + 1)
        else:
            values = [int(token)]
        for account_id in values:
            if account_id <= 0:
                raise ValueError("账号ID必须为正整数")
            if account_id not in seen:
                seen.add(account_id)
                account_ids.append(account_id)
                if len(account_ids) > 200:
                    raise ValueError("一次最多选择 200 个账号")
    if not account_ids:
        raise ValueError("没有识别到账号ID")
    return account_ids


async def build_session_export(session: AsyncSession, account_ids: list[int]) -> tuple[str, int, list[str]]:
    lines = [
        "# Telethon StringSession export",
        f"# generated_at={datetime.now(timezone.utc).isoformat()}",
        "# 警告：string_session 等同于账号登录凭证，请不要发给不可信的人。",
        "# 导入时使用 phone 和 string_session 两项。",
        "",
    ]
    exported = 0
    skipped: list[str] = []
    for account_id in account_ids:
        account = await session.get(TgAccount, account_id)
        if account is None:
            skipped.append(f"#{account_id}: 账号不存在")
            continue
        tg_session = await session.scalar(
            select(TgSession)
            .where(TgSession.account_id == account.id, TgSession.is_active.is_(True))
            .order_by(TgSession.id.desc())
            .limit(1)
        )
        if tg_session is None:
            skipped.append(f"#{account.id}: 没有 active session")
            continue
        try:
            phone = decrypt_text(account.phone_encrypted) or account.phone_masked
            session_str = decrypt_text(tg_session.session_encrypted)
        except Exception as exc:
            skipped.append(f"#{account.id}: 解密失败 {exc}")
            continue
        if not session_str:
            skipped.append(f"#{account.id}: session 为空")
            continue
        exported += 1
        lines.extend(
            [
                "[account]",
                f"account_id={account.id}",
                f"telegram_user_id={account.user_id or ''}",
                f"username={('@' + account.username) if account.username else ''}",
                f"phone={phone}",
                f"phone_masked={account.phone_masked}",
                "session_type=telethon_string",
                f"string_session={session_str}",
                "",
            ]
        )
    if skipped:
        lines.append("[skipped]")
        lines.extend(skipped)
        lines.append("")
    return "\n".join(lines), exported, skipped


async def send_session_export(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    account_ids: list[int],
) -> None:
    async with sessionmaker() as session:
        content, exported, skipped = await build_session_export(session, account_ids)
    if exported == 0:
        await message.answer("没有可导出的 active session。\n" + ("\n".join(skipped) if skipped else ""))
        return
    if len(account_ids) == 1:
        filename = f"tg_session_{account_ids[0]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    else:
        filename = f"tg_sessions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    document = BufferedInputFile(content.encode("utf-8"), filename=filename)
    caption = f"已导出 {exported} 个 active session。文件包含敏感登录凭证，请妥善保存。"
    if skipped:
        caption += f"\n跳过 {len(skipped)} 个账号，详情见文件底部。"
    await message.answer_document(document, caption=caption[:1024])


def parse_session_import_payload(content: str) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    current: dict[str, str] = {}

    def flush_current() -> None:
        phone = (current.get("phone") or "").strip()
        session_str = (current.get("string_session") or current.get("session") or "").strip()
        if phone and session_str:
            accounts.append((phone, session_str))

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower() == "[account]":
            flush_current()
            current = {}
            continue
        if line.startswith("[") and line.endswith("]"):
            flush_current()
            current = {}
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip().lower()] = value.strip()
    flush_current()

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for phone, session_str in accounts:
        marker = f"{phone}:{session_str}"
        if marker in seen:
            continue
        seen.add(marker)
        unique.append((phone, session_str))
    if not unique:
        raise ValueError("未检测到可导入的账号。请上传由 /export_sessions 导出的 txt 文件。")
    return unique


async def read_session_import_content(message: Message, bot: Bot) -> str:
    if message.document:
        if message.document.file_size and message.document.file_size > 5 * 1024 * 1024:
            raise ValueError("文件过大，请上传 5MB 以内的 txt 文件。")
        buffer = io.BytesIO()
        await bot.download(message.document, destination=buffer)
        return buffer.getvalue().decode("utf-8", errors="replace")
    if message.text:
        return message.text
    raise ValueError("请上传导出的 txt 文件，或直接粘贴文件内容。")


async def import_session_payload(
    sessionmaker: async_sessionmaker[AsyncSession],
    content: str,
) -> tuple[int, int, str]:
    entries = parse_session_import_payload(content)
    ok_lines: list[str] = []
    failed_lines: list[str] = []
    for index, (phone, session_str) in enumerate(entries, start=1):
        try:
            async with sessionmaker() as session:
                account = await account_ops.import_session(session, phone, session_str)
            ok_lines.append(f"{index}. OK #{account.id} {account.phone_masked} user_id={account.user_id or '-'}")
        except Exception as exc:
            masked = account_ops.mask_phone(phone)
            failed_lines.append(f"{index}. FAIL {masked}: {exc}")
    lines = [
        f"批量导入完成：成功 {len(ok_lines)}，失败 {len(failed_lines)}，总计 {len(entries)}",
        "",
        "[success]",
        *(ok_lines or ["无"]),
        "",
        "[failed]",
        *(failed_lines or ["无"]),
    ]
    return len(ok_lines), len(failed_lines), "\n".join(lines)


async def send_import_report(message: Message, report: str) -> None:
    if len(report) <= 3500:
        await message.answer(report)
        return
    filename = f"tg_session_import_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    await message.answer_document(
        BufferedInputFile(report.encode("utf-8"), filename=filename),
        caption="批量导入完成，详情见报告文件。",
    )


async def status_text(
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> str:
    async with sessionmaker() as session:
        total = await session.scalar(select(func.count()).select_from(TgAccount))
        active_account_ids = set(
            (await session.scalars(select(TgSession.account_id).where(TgSession.is_active.is_(True)))).all()
        )
        selected_domain = await get_selected_domain(session)
        whitelist_ids = await get_whitelist_ids(session)
    connected_active_ids = active_account_ids.intersection(client_pool.connected_account_ids)
    protected_connected_ids = connected_active_ids.difference(whitelist_ids)
    configuration_ready = bool(
        settings.login_email_gmail_username
        and settings.login_email_gmail_app_password
        and selected_domain
    )
    email_guard_status = login_email_runtime_status(
        configuration_ready=configuration_ready,
        monitor_enabled=client_pool.monitor_enabled,
        monitor_running=client_pool.service_monitor_running,
        active_count=len(active_account_ids),
        connected_count=len(connected_active_ids),
        protected_connected_count=len(protected_connected_ids),
        health_checked=client_pool.login_email_health_checked_at is not None,
        health_error=client_pool.login_email_health_error,
    )
    gmail_status = (
        "未检查"
        if client_pool.login_email_health_checked_at is None
        else "失败"
        if client_pool.login_email_health_error
        else "正常"
    )
    return (
        "系统状态\n"
        "Bot: OK\n"
        f"实时监听: {'开启' if client_pool.monitor_enabled else '关闭'}\n"
        f"账号: {total or 0}\n"
        f"可连接账号: {len(active_account_ids)}\n"
        f"已连接: {len(client_pool.connected_account_ids)}\n"
        f"登录邮箱保护开关: {'开启' if settings.login_email_protection_enabled else '关闭'}\n"
        f"登录邮箱保护状态: {email_guard_status}\n"
        f"Gmail IMAP: {gmail_status}\n"
        f"自动保护账号: {len(protected_connected_ids)}/{len(active_account_ids)}\n"
        f"保护域名: @{selected_domain or '-'}\n"
        f"保护白名单: {len(whitelist_ids)}\n"
        f"时间: {datetime.now(timezone.utc).isoformat()}"
    )


def login_email_runtime_status(
    *,
    configuration_ready: bool,
    monitor_enabled: bool,
    monitor_running: bool,
    active_count: int,
    connected_count: int,
    protected_connected_count: int,
    health_checked: bool,
    health_error: str | None,
) -> str:
    if not settings.login_email_protection_enabled:
        return "已停用"
    if not configuration_ready:
        return "配置不完整"
    if not monitor_enabled:
        return "实时监听已关闭"
    if not monitor_running:
        return "实时监听任务未运行"
    if active_count == 0:
        return "没有 active 账号"
    if connected_count == 0:
        return "账号均未连接"
    if protected_connected_count == 0:
        return "已连接账号均在白名单"
    if not health_checked:
        return "Gmail IMAP 尚未检查"
    if health_error:
        return "Gmail IMAP 不可用"
    if connected_count < active_count:
        return f"基础链路部分就绪（已连接 {connected_count}/{active_count}）"
    return "基础链路就绪（待全链路检测）"


async def accounts_text_and_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    requested_page: int = 1,
) -> tuple[str, list[TgAccount], int, int]:
    async with sessionmaker() as session:
        total = int(await session.scalar(select(func.count()).select_from(TgAccount)) or 0)
        page, pages, offset = account_page_window(total, requested_page)
        rows = await session.scalars(
            select(TgAccount)
            .order_by(TgAccount.id)
            .offset(offset)
            .limit(ACCOUNT_PAGE_SIZE)
        )
        accounts_list = list(rows.all())
    lines = [account_line(row) for row in accounts_list]
    heading = f"账号列表 · 第 {page}/{max(pages, 1)} 页 · 共 {total} 个"
    return heading + "\n" + ("\n".join(lines) if lines else "暂无账号"), accounts_list, page, pages


async def account_detail_text(session: AsyncSession, account_id: int) -> str:
    account = await get_account(session, account_id)
    security = await session.get(AccountSecurity, account.id)
    privacy = await session.get(PrivacySettings, account.id)
    return "\n".join(
        [
            account_line(account),
            f"user_id: {account.user_id or '-'}",
            f"2FA: {'yes' if security and security.has_2fa else 'no/unknown'}",
            f"privacy: {privacy.rules_json if privacy else '{}'}",
            f"last_error: {account.last_error or '-'}",
        ]
    )


def account_status_label(status: str | None) -> str:
    labels = {
        "normal": "正常",
        "limited": "限制",
        "banned": "封禁",
        "unknown": "未知",
        "active": "未检测",
        "new": "未检测",
        "session_invalid": "Session失效",
    }
    return labels.get(status or "unknown", status or "未知")


async def account_full_detail_text(
    session: AsyncSession,
    account_id: int,
    live_2fa: dict[str, str | bool | None] | None = None,
) -> str:
    account = await get_account(session, account_id)
    security = await session.get(AccountSecurity, account.id)
    privacy = await session.get(PrivacySettings, account.id)
    latest_spam = await session.scalar(
        select(SpamCheck)
        .where(SpamCheck.account_id == account.id)
        .order_by(desc(SpamCheck.checked_at), desc(SpamCheck.id))
        .limit(1)
    )
    active_session = await session.scalar(
        select(TgSession.id)
        .where(TgSession.account_id == account.id, TgSession.is_active.is_(True))
        .order_by(TgSession.id.desc())
        .limit(1)
    )
    local_login_email = (
        decrypt_text(security.login_email_encrypted)
        if security and security.login_email_encrypted
        else None
    )
    local_recovery_email = (
        decrypt_text(security.email_encrypted) if security and security.email_encrypted else None
    )
    live_login_email = (
        str(live_2fa.get("login_email_pattern"))
        if live_2fa and live_2fa.get("login_email_pattern")
        else None
    )
    has_2fa = bool(live_2fa.get("has_2fa")) if live_2fa and "has_2fa" in live_2fa else bool(security and security.has_2fa)
    twofa_hint = live_2fa.get("hint") if live_2fa else None
    spam_status = latest_spam.status_detected if latest_spam else account.status
    return "\n".join(
        [
            f"账号 #{account.id}",
            f"Telegram ID: {account.user_id or '-'}",
            f"用户名: @{account.username}" if account.username else "用户名: -",
            f"姓名: {' '.join(part for part in [account.first_name, account.last_name] if part) or '-'}",
            f"手机号: {account.phone_masked}",
            f"登录邮箱: {local_login_email or live_login_email or '-'}",
            f"2FA 恢复邮箱: {local_recovery_email or '-'}",
            f"本地 session: {'可用' if active_session else '不可用'}",
            f"账号状态: {account_status_label(spam_status)}",
            f"SpamBot 检测: {account_status_label(latest_spam.status_detected) if latest_spam else '未检测'}",
            f"SpamBot 时间: {latest_spam.checked_at.isoformat() if latest_spam else '-'}",
            f"SpamBot 原文: {(latest_spam.response_text or '-')[:1200] if latest_spam else '-'}",
            f"2FA: {'已启用' if has_2fa else '未启用/未知'}",
            f"2FA 提示: {twofa_hint or '-'}",
            f"隐私快照: {privacy.rules_json if privacy else '{}'}",
            f"最后登录: {account.last_login_at.isoformat() if account.last_login_at else '-'}",
            f"最后错误: {account.last_error or '-'}",
        ]
    )


async def answer_panel(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    await callback.answer()
    if not callback.message:
        return
    if isinstance(reply_markup, InlineKeyboardMarkup):
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    await callback.message.answer(text, reply_markup=reply_markup or main_menu())


async def dismiss_panel(callback: CallbackQuery) -> None:
    """Close an inline panel without adding a new message to the chat."""
    await callback.answer()
    if not callback.message:
        return
    try:
        await callback.message.delete()
    except TelegramAPIError:
        # Old messages may no longer be deletable; still avoid sending a
        # replacement message that would add more noise to the conversation.
        pass


async def ask_with_cancel(message: Message, text: str, placeholder: str) -> None:
    await message.answer(f"{text}\n\n可随时点击“取消当前操作”。", reply_markup=cancel_inline())


async def ask_callback_with_cancel(callback: CallbackQuery, text: str, placeholder: str) -> None:
    await callback.answer()
    if callback.message:
        await ask_with_cancel(callback.message, text, placeholder)


async def qr_flow_is_active(state: FSMContext, flow_id: str) -> bool:
    data = await state.get_data()
    return await state.get_state() == LoginFlow.qr_wait.state and data.get("flow_id") == flow_id


async def delete_sensitive_input(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass


def has_login_email(twofa_info: dict[str, str | bool | None]) -> bool:
    return bool(twofa_info.get("login_email_pattern"))


def post_login_security_text(account: TgAccount, twofa_info: dict[str, str | bool | None]) -> str | None:
    has_2fa = bool(twofa_info.get("has_2fa"))
    login_email_exists = has_login_email(twofa_info)
    if has_2fa and login_email_exists:
        return None
    header = f"账号 #{account.id} {account.phone_masked} 已成功添加进系统。"
    if not has_2fa and not login_email_exists:
        return (
            f"{header}\n"
            "检测到账号未开启 2FA，且没有配置登录邮箱。\n"
            "请打开 2FA 设置，自行设置每个账号唯一的强密码和恢复邮箱。"
        )
    if not has_2fa:
        return (
            f"{header}\n"
            "检测到账号未开启 2FA。\n"
            "请打开 2FA 设置，自行设置每个账号唯一的强密码。"
        )
    return f"{header}\n检测到账号已经开启 2FA，但没有配置登录邮箱，请在 2FA 设置中补充。"


async def prompt_post_login_security(
    message: Message,
    account: TgAccount,
    client_pool: ClientPool,
) -> None:
    try:
        client = await client_pool.get_client(account.id)
        twofa_info = await account_ops.get_2fa_info(client)
    except Exception as exc:
        await message.answer(f"登录后安全配置检查失败：{exc}")
        return
    text = post_login_security_text(account, twofa_info)
    if text:
        await message.answer(text, reply_markup=post_login_security_panel(account.id))


def download_url_to_file(url: str, path: Path) -> None:
    if url not in RANDOM_AVATAR_URLS or not url.startswith("https://"):
        raise ValueError("不允许的随机头像来源")
    request = urllib.request.Request(url, headers={"User-Agent": "tg-account-bot/0.1"})
    # URL is selected from the immutable HTTPS allowlist above, never user input.
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
        payload = response.read(MAX_IMAGE_SIZE + 1)
    if len(payload) > MAX_IMAGE_SIZE:
        raise ValueError("图片超过 5MB 限制")
    header = payload[:16]
    if not (
        header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"\x89PNG")
        or header.startswith(b"RIFF")
        or header.startswith(b"GIF8")
    ):
        raise ValueError("接口返回的不是可识别图片")
    path.write_bytes(payload)


@router.message(Command("start", "menu"))
async def start(message: Message) -> None:
    await message.answer(
        "Telegram 多账号管理 Bot\n\n"
        "主菜单可以用输入框旁的小键盘按钮随时展开或收起。"
        "这里只保留常用入口，Session、批量任务和高级设置请从输入框旁的“打开”菜单进入。",
        reply_markup=main_menu(),
    )


@router.message(Command("cancel"))
@router.message(F.text.in_(CANCEL_TEXTS))
async def cancel_current(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    client = data.get("client")
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
    await state.clear()
    await message.answer("已取消当前操作，主菜单仍可从输入框旁展开。", reply_markup=main_menu())


@router.callback_query(F.data == "flow:cancel")
async def cancel_current_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    client = data.get("client")
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
    await state.clear()
    await callback.answer("已取消")
    if callback.message:
        await callback.message.answer("已取消当前操作，主菜单仍可从输入框旁展开。", reply_markup=main_menu())


@router.message(Command("cmd", "help", "command"))
async def cmd(message: Message) -> None:
    await message.answer(COMMANDS[:4096], reply_markup=main_menu())
    if len(COMMANDS) > 4096:
        await message.answer(COMMANDS[4096:])


@router.message(Command("app", "mini_app"))
async def mini_app(message: Message) -> None:
    panel = mini_app_panel()
    if panel is None:
        await message.answer(
            "内置应用未配置公开 HTTPS 地址。请设置 MINI_APP_PUBLIC_URL，例如 https://your-domain.example/mini-app。"
        )
        return
    await message.answer("打开 Telegram 内置应用。", reply_markup=panel)


@router.message(Command("status"))
async def status(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    await message.answer(await status_text(sessionmaker, client_pool), reply_markup=main_menu())


@router.message(Command("settings"))
async def settings_command(message: Message) -> None:
    await message.answer("目标白名单与速率配置", reply_markup=settings_panel())


@router.message(Command("security"))
async def security_command(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    async with sessionmaker() as session:
        text, panel = await login_email_guard_view(session, client_pool)
    await message.answer(text, reply_markup=panel)


@router.message(Command("login"))
async def login_start(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    current_data = await state.get_data()
    if current_data.get("client") is not None and current_state in {
        LoginFlow.code.state,
        LoginFlow.password.state,
        LoginFlow.qr_wait.state,
    }:
        if current_state == LoginFlow.qr_wait.state:
            text = "当前二维码仍在等待扫码，本次没有重新生成。"
        elif current_state == LoginFlow.password.state:
            text = "当前登录已进入 2FA 密码校验，本次没有重新发码。"
        else:
            delivery = current_data.get("delivery") or {}
            text = (
                "当前验证码请求仍在等待输入，本次没有重复发码。\n"
                f"送达方式：{delivery.get('label') or 'Telegram 指定方式'}"
            )
        await ask_with_cancel(message, text, "继续当前登录流程")
        return
    await state.clear()
    await state.set_state(LoginFlow.phone)
    await message.answer(
        "请输入手机号，格式如 +8613800000000\n\n"
        "如果验证码收不到，可以改用二维码登录。",
        reply_markup=login_phone_panel(),
    )


@router.message(Command("qr_login"))
async def qr_login_start(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    previous = await state.get_data()
    previous_client = previous.get("client")
    if previous_client is not None:
        try:
            await previous_client.disconnect()
        except Exception:
            pass
    await state.clear()

    flow_id = uuid.uuid4().hex
    try:
        client, qr_login = await account_ops.start_qr_login()
    except Exception as exc:
        await message.answer(f"生成登录二维码失败：{exc}", reply_markup=main_menu())
        return

    await state.set_state(LoginFlow.qr_wait)
    await state.update_data(client=client, login_method="qr", flow_id=flow_id)
    wait_task = asyncio.create_task(qr_login.wait(), name=f"qr-login-{flow_id}")
    qr_message: Message | None = None
    try:
        qr_message = await message.answer_photo(
            BufferedInputFile(login_qr_png(qr_login.url), filename="telegram-login.png"),
            caption=(
                "请使用已经登录目标账号的 Telegram 客户端扫码：\n"
                "设置 → 设备 → 连接桌面设备。\n\n"
                "二维码为一次性登录凭证，请勿转发；过期后可重新生成。"
            ),
            reply_markup=cancel_inline(),
        )
        await wait_task
        if not await qr_flow_is_active(state, flow_id):
            return
        session_str, me = await account_ops.finish_authorized_login(client)
        phone = account_ops.phone_from_user(me)
    except asyncio.TimeoutError:
        if await qr_flow_is_active(state, flow_id):
            await state.clear()
            await message.answer("登录二维码已过期，请重新点击“扫码登录”生成。", reply_markup=main_menu())
        return
    except SessionPasswordNeededError:
        if not await qr_flow_is_active(state, flow_id):
            return
        hint = "-"
        try:
            twofa_info = await account_ops.get_2fa_info(client)
            hint = str(twofa_info.get("hint") or "-")
        except Exception:
            pass
        await state.set_state(LoginFlow.password)
        await state.update_data(client=client, login_method="qr", flow_id=flow_id)
        await ask_with_cancel(message, f"扫码确认成功，该账号需要 2FA 密码，请输入。\n密码提示：{hint}", "2FA 密码")
        return
    except Exception as exc:
        if await qr_flow_is_active(state, flow_id):
            await state.clear()
            await message.answer(f"二维码登录失败：{exc}", reply_markup=main_menu())
        return
    finally:
        if not wait_task.done():
            wait_task.cancel()
        if qr_message is not None:
            try:
                await qr_message.delete()
            except TelegramAPIError:
                pass
        if await state.get_state() != LoginFlow.password.state and client.is_connected():
            await client.disconnect()

    async with sessionmaker() as session:
        account = await account_ops.save_logged_in_account(session, phone, session_str, me)
    await state.clear()
    await message.answer(f"二维码登录完成：账号ID #{account.id} {account.phone_masked}", reply_markup=main_menu())
    await message.answer("账号操作", reply_markup=account_actions_panel(account.id))
    await prompt_post_login_security(message, account, client_pool)


@router.callback_query(F.data == "login:qr")
async def qr_login_callback(
    callback: CallbackQuery,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    await callback.answer()
    if callback.message:
        await qr_login_start(callback.message, state, sessionmaker, client_pool)


@router.message(LoginFlow.phone)
async def login_phone(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    phone = (message.text or "").strip()
    phone_masked = account_ops.mask_phone(phone)
    async with sessionmaker() as session:
        candidates = list(
            (
                await session.scalars(select(TgAccount).where(TgAccount.phone_masked == phone_masked))
            ).all()
        )
        existing = next(
            (
                account
                for account in candidates
                if decrypt_text(account.phone_encrypted) == phone
            ),
            None,
        )
        if existing is not None:
            active_session = await session.scalar(
                select(TgSession.id)
                .where(TgSession.account_id == existing.id, TgSession.is_active.is_(True))
                .limit(1)
            )
            if active_session is not None:
                await state.clear()
                await message.answer(
                    f"该手机号已登录：账号 #{existing.id} {existing.phone_masked}",
                    reply_markup=main_menu(),
                )
                await message.answer("账号操作", reply_markup=account_actions_panel(existing.id))
                return
    try:
        client, phone_code_hash, delivery = await account_ops.start_login(phone)
    except PhoneNumberInvalidError:
        await state.set_state(LoginFlow.phone)
        await ask_with_cancel(message, "手机号格式无效，请重新输入。", "+8613800000000")
        return
    except PhoneNumberBannedError:
        await state.clear()
        await message.answer("这个手机号被 Telegram 标记为不可登录/封禁，请换一个手机号。", reply_markup=main_menu())
        return
    except FloodWaitError as exc:
        await state.set_state(LoginFlow.phone)
        await ask_with_cancel(
            message,
            f"请求过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再试。",
            "+8613800000000",
        )
        return
    except Exception as exc:
        await state.set_state(LoginFlow.phone)
        await ask_with_cancel(message, f"发送验证码失败：{exc}\n请检查手机号后重试。", "+8613800000000")
        return
    await state.update_data(phone=phone, phone_code_hash=phone_code_hash, client=client, delivery=delivery)
    await state.set_state(LoginFlow.code)
    detail = delivery["label"]
    if delivery.get("length"):
        detail = f"{detail}，{delivery['length']} 位"
    if delivery.get("pattern"):
        detail = f"{detail}，匹配：{delivery['pattern']}"
    await ask_with_cancel(message, f"验证码已发送，请输入验证码。\n送达方式：{detail}", "Telegram 验证码")


@router.message(LoginFlow.code)
async def login_code(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    data = await state.get_data()
    code = (message.text or "").strip()
    await delete_sensitive_input(message)
    try:
        session_str, me = await account_ops.complete_login(
            phone=data["phone"],
            code=code,
            phone_code_hash=data["phone_code_hash"],
            password=None,
            transient_client=data["client"],
        )
    except SessionPasswordNeededError:
        hint = "-"
        try:
            twofa_info = await account_ops.get_2fa_info(data["client"])
            hint = str(twofa_info.get("hint") or "-")
        except Exception:
            pass
        await state.set_state(LoginFlow.password)
        await ask_with_cancel(message, f"该账号需要 2FA 密码，请输入。\n密码提示：{hint}", "2FA 密码")
        return
    except (PhoneCodeInvalidError, PhoneCodeEmptyError):
        await state.set_state(LoginFlow.code)
        await ask_with_cancel(message, "验证码错误，请重新输入。", "Telegram 验证码")
        return
    except PhoneCodeExpiredError:
        client = data.get("client")
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        await state.clear()
        await message.answer("验证码已过期，请重新开始登录。", reply_markup=main_menu())
        return
    except FloodWaitError as exc:
        await state.set_state(LoginFlow.code)
        await ask_with_cancel(
            message,
            f"尝试过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再输入验证码。",
            "Telegram 验证码",
        )
        return
    except Exception as exc:
        await state.set_state(LoginFlow.code)
        await ask_with_cancel(message, f"登录校验失败：{exc}\n请重新输入验证码，或取消后重新登录。", "Telegram 验证码")
        return
    async with sessionmaker() as session:
        account = await account_ops.save_logged_in_account(session, data["phone"], session_str, me)
    await state.clear()
    await message.answer(f"登录完成：账号ID #{account.id} {account.phone_masked}", reply_markup=main_menu())
    await message.answer("账号操作", reply_markup=account_actions_panel(account.id))
    await prompt_post_login_security(message, account, client_pool)


@router.message(LoginFlow.password)
async def login_password(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    data = await state.get_data()
    password = (message.text or "").strip()
    await delete_sensitive_input(message)
    try:
        session_str, me = await account_ops.complete_password_login(data["client"], password)
    except PasswordHashInvalidError:
        hint = "-"
        try:
            twofa_info = await account_ops.get_2fa_info(data["client"])
            hint = str(twofa_info.get("hint") or "-")
        except Exception:
            pass
        await state.set_state(LoginFlow.password)
        await ask_with_cancel(message, f"2FA 密码错误，请重新输入。\n密码提示：{hint}", "2FA 密码")
        return
    except FloodWaitError as exc:
        await state.set_state(LoginFlow.password)
        await ask_with_cancel(
            message,
            f"尝试过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再输入 2FA 密码。",
            "2FA 密码",
        )
        return
    except Exception as exc:
        await state.set_state(LoginFlow.password)
        await ask_with_cancel(message, f"2FA 校验失败：{exc}\n请重新输入，或取消后重新登录。", "2FA 密码")
        return
    try:
        phone = data.get("phone") or account_ops.phone_from_user(me)
    except ValueError as exc:
        await state.clear()
        await message.answer(str(exc), reply_markup=main_menu())
        return
    async with sessionmaker() as session:
        account = await account_ops.save_logged_in_account(session, phone, session_str, me, password)
    await state.clear()
    await message.answer(f"登录完成：账号ID #{account.id} {account.phone_masked}", reply_markup=main_menu())
    await message.answer("账号操作", reply_markup=account_actions_panel(account.id))
    await prompt_post_login_security(message, account, client_pool)


@router.message(Command("import_session"))
async def import_session_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ImportSessionFlow.phone)
    await ask_with_cancel(message, "请输入该 session 对应手机号。", "+8613800000000")


@router.message(ImportSessionFlow.phone)
async def import_session_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=(message.text or "").strip())
    await state.set_state(ImportSessionFlow.session)
    await ask_with_cancel(message, "请输入 Telethon StringSession。", "StringSession")


@router.message(ImportSessionFlow.session)
async def import_session_value(message: Message, state: FSMContext, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    data = await state.get_data()
    session_value = (message.text or "").strip()
    await delete_sensitive_input(message)
    async with sessionmaker() as session:
        account = await account_ops.import_session(session, data["phone"], session_value)
    await state.clear()
    await message.answer(f"导入完成：账号ID #{account.id} {account.phone_masked}", reply_markup=main_menu())
    await message.answer("账号操作", reply_markup=account_actions_panel(account.id))


@router.message(Command("import_sessions"))
async def import_sessions_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ImportSessionsFlow.payload)
    await ask_with_cancel(message, "请上传 /export_sessions 导出的 txt 文件，或直接粘贴文件内容。", "上传 txt 文件或粘贴内容")


@router.message(ImportSessionsFlow.payload)
async def import_sessions_payload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    try:
        content = await read_session_import_content(message, bot)
        await delete_sensitive_input(message)
        _, _, report = await import_session_payload(sessionmaker, content)
    except Exception as exc:
        await state.set_state(ImportSessionsFlow.payload)
        await ask_with_cancel(message, f"批量导入失败：{exc}\n请重新上传文件，或取消当前操作。", "上传 txt 文件或粘贴内容")
        return
    await state.clear()
    await send_import_report(message, report)
    await message.answer("批量导入流程已结束。", reply_markup=main_menu())


@router.message(Command("export_session"))
async def export_session(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    if not a:
        await message.answer("用法：/export_session <账号ID或Telegram ID>")
        return
    try:
        async with sessionmaker() as session:
            account_id = await resolve_account_id(session, a[0])
        await send_session_export(message, sessionmaker, [account_id])
    except Exception as exc:
        await message.answer(f"导出失败：{exc}")


@router.message(Command("export_sessions"))
async def export_sessions(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    if not a:
        await message.answer("用法：/export_sessions <账号ID列表> 或 /export_sessions <start_id> <count>\n例：/export_sessions 1,3,5-8")
        return
    try:
        async with sessionmaker() as session:
            if len(a) >= 2 and a[0].isdigit() and a[1].isdigit():
                account_ids = await account_ids_from_range(session, int(a[0]), int(a[1]))
            else:
                account_ids = parse_account_selection(" ".join(a))
                account_ids = [await resolve_account_id(session, account_id) for account_id in account_ids]
        await send_session_export(message, sessionmaker, account_ids)
    except Exception as exc:
        await message.answer(f"导出失败：{exc}")


@router.message(Command("export_session_select"))
async def export_session_select(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ExportSessionFlow.selection)
    await ask_with_cancel(message, "请输入要导出的账号ID，支持 1,3,5-8。", "1,3,5-8")


@router.message(ExportSessionFlow.selection)
async def export_session_selection(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    try:
        selected = parse_account_selection((message.text or "").strip())
        async with sessionmaker() as session:
            account_ids = [await resolve_account_id(session, account_id) for account_id in selected]
        await send_session_export(message, sessionmaker, account_ids)
        await state.clear()
        await message.answer("导出流程已结束。", reply_markup=main_menu())
    except Exception as exc:
        await state.set_state(ExportSessionFlow.selection)
        await ask_with_cancel(message, f"账号ID格式不正确或账号不存在：{exc}\n请重新输入。", "1,3,5-8")


@router.message(Command("accounts"))
async def accounts(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    text, accounts_list, page, pages = await accounts_text_and_rows(sessionmaker)
    await message.answer(
        text,
        reply_markup=accounts_panel(accounts_list, page, pages),
    )


@router.message(Command("account", "profile"))
async def account_detail(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    if not a:
        await message.answer("用法：/account <id>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        text = await account_detail_text(session, account_id)
    await message.answer(text, reply_markup=account_actions_panel(account_id))


@router.message(Command("account_info", "account_detail"))
async def account_info(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    if not a:
        await message.answer("用法：/account_info <id>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        text = await account_full_detail_text(session, account_id)
    await message.answer(text[:4096], reply_markup=account_actions_panel(account_id))


@router.message(Command("reconnect"))
async def reconnect(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if not a:
        await message.answer("用法：/reconnect <id>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        account = await get_account(session, account_id)
        client = await client_pool.get_client(account.id)
        await account_ops.sync_me(session, account, client)
    await message.answer(f"重连成功：#{account_id}")


@router.message(Command("reconnect_all", "service_monitor_on"))
async def reconnect_all(message: Message, client_pool: ClientPool) -> None:
    await client_pool.start_service_monitor()
    await message.answer(
        f"实时监听已开启，已连接可用 session 账号：{len(client_pool.connected_account_ids)}",
        reply_markup=main_menu(),
    )


@router.message(Command("service_monitor_off"))
async def monitor_off(message: Message, client_pool: ClientPool) -> None:
    await client_pool.stop_service_monitor()
    await message.answer("实时监听已关闭。普通账号操作仍可按需重连。", reply_markup=main_menu())


@router.message(Command("set_name"))
async def set_name(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) < 2:
        await message.answer("用法：/set_name <id> <first> [last]")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        account = await get_account(session, account_id)
        client = await client_pool.get_client(account_id)
        await account_ops.set_name(client, a[1], a[2] if len(a) > 2 else None)
        await account_ops.sync_me(session, account, client)
    await message.answer("姓名已更新。")


@router.message(Command("set_bio"))
async def set_bio(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) < 2:
        await message.answer("用法：/set_bio <id> <bio>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
    client = await client_pool.get_client(account_id)
    await account_ops.set_bio(client, " ".join(a[1:]))
    await message.answer("简介已更新。")


@router.message(Command("set_username"))
async def set_username(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) != 2:
        await message.answer("用法：/set_username <id> <username>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        account = await get_account(session, account_id)
        client = await client_pool.get_client(account_id)
        await account_ops.set_username(client, a[1].lstrip("@"))
        await account_ops.sync_me(session, account, client)
    await message.answer("用户名已更新。")


@router.message(Command("set_avatar"))
async def set_avatar(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) != 2:
        await message.answer("用法：/set_avatar <id> <服务器文件路径>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
    client = await client_pool.get_client(account_id)
    await account_ops.set_avatar(client, a[1])
    await message.answer("头像已更新。")


@router.message(Command("privacy"))
async def privacy(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    if len(a) != 1:
        await message.answer("用法：/privacy <id>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        row = await session.get(PrivacySettings, account_id)
    await message.answer(f"隐私快照：{row.rules_json if row else '{}'}")


@router.message(Command("set_privacy"))
async def set_privacy(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) != 3:
        await message.answer("用法：/set_privacy <id> <phone|last_seen|profile_photo|forwards|calls|groups> <everybody|contacts|nobody>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        client = await client_pool.get_client(account_id)
        values = await account_ops.set_privacy(client, a[1], a[2])
        await account_ops.save_privacy_snapshot(session, account_id, values)
    await message.answer("隐私设置已更新。")


@router.message(Command("check_2fa"))
async def check_2fa(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) != 1:
        await message.answer("用法：/check_2fa <id>")
        return
    async with sessionmaker() as session:
        account_id = await resolve_account_id(session, a[0])
        client = await client_pool.get_client(account_id)
        has_2fa = await account_ops.check_2fa(client)
        await account_ops.update_security_snapshot(session, account_id, has_2fa)
    await message.answer(f"2FA: {'已启用' if has_2fa else '未启用'}")


@router.message(Command("set_2fa", "change_2fa", "disable_2fa"))
async def twofa(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    command = (message.text or "").split()[0].lstrip("/")
    a = args(message)
    await delete_sensitive_input(message)
    try:
        account_id = int(a[0])
    except Exception:
        await message.answer("用法：/set_2fa <id> <new> [hint] | /change_2fa <id> <old> <new> [hint] | /disable_2fa <id> <old>")
        return
    client = await client_pool.get_client(account_id)
    if command == "set_2fa":
        if len(a) < 2:
            await message.answer("用法：/set_2fa <id> <new_password> [hint]")
            return
        await account_ops.edit_2fa(client, None, a[1], a[2] if len(a) > 2 else None)
        async with sessionmaker() as session:
            await account_ops.update_security_snapshot(session, account_id, True, a[1], a[2] if len(a) > 2 else None)
    elif command == "change_2fa":
        if len(a) < 3:
            await message.answer("用法：/change_2fa <id> <old_password> <new_password> [hint]")
            return
        await account_ops.edit_2fa(client, a[1], a[2], a[3] if len(a) > 3 else None)
        async with sessionmaker() as session:
            await account_ops.update_security_snapshot(session, account_id, True, a[2], a[3] if len(a) > 3 else None)
    else:
        if len(a) != 2:
            await message.answer("用法：/disable_2fa <id> <old_password>")
            return
        await account_ops.edit_2fa(client, a[1], None)
        async with sessionmaker() as session:
            await account_ops.update_security_snapshot(session, account_id, False)
    await message.answer("2FA 操作完成。")


@router.message(Command("spam"))
async def spam(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) != 1:
        await message.answer("用法：/spam <id>")
        return
    try:
        async with sessionmaker() as session:
            account_id = await resolve_account_id(session, a[0])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    client = await client_pool.get_client(account_id)
    async with sessionmaker() as session:
        record = await account_ops.spam_check(session, account_id, client)
    await message.answer(f"SpamBot 状态：{account_status_label(record.status_detected)}\n{(record.response_text or '')[:3500]}")


@router.message(Command("spam_all"))
async def spam_all(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    async with sessionmaker() as session:
        ids = await account_ids_from_range(session, 1, 200)
    ok = 0
    failed = 0
    for account_id in ids:
        try:
            client = await client_pool.get_client(account_id)
            async with sessionmaker() as session:
                await account_ops.spam_check(session, account_id, client)
            ok += 1
        except Exception:
            failed += 1
    await message.answer(f"SpamBot 批量完成：成功 {ok}，失败 {failed}")


@router.message(Command("service_check"))
async def service_check(message: Message, sessionmaker: async_sessionmaker[AsyncSession], client_pool: ClientPool) -> None:
    a = args(message)
    if len(a) != 1:
        await message.answer("用法：/service_check <id>")
        return
    try:
        async with sessionmaker() as session:
            account_id = await resolve_account_id(session, a[0])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    client = await client_pool.get_client(account_id)
    async with sessionmaker() as session:
        service_inserted = await account_ops.service_check(session, account_id, client)
    await client_pool.catch_up_recent_login_alerts(account_id, client)
    await message.answer(f"Telegram 777000 服务消息检查完成：新增 {service_inserted} 条。")


@router.message(Command("target_allowlist"))
async def target_allowlist(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    async with sessionmaker() as session:
        if not a or a[0] == "list":
            rows = await session.scalars(select(AllowedTarget).order_by(AllowedTarget.id))
            lines = [f"#{r.id} {r.target_type} {r.target_ref} {r.title or ''}" for r in rows.all()]
            await message.answer("授权目标\n" + ("\n".join(lines) if lines else "暂无"))
            return
        if a[0] == "add" and len(a) >= 3:
            target_ref = canonicalize_target_ref(a[2])
            exists = await session.scalar(
                select(AllowedTarget.id).where(
                    AllowedTarget.target_ref == target_ref,
                )
            )
            if exists is not None:
                await message.answer("该授权目标已经存在。")
                return
            session.add(AllowedTarget(target_type=a[1], target_ref=target_ref, title=" ".join(a[3:]) or None))
            await session.commit()
            await message.answer("已添加授权目标。")
            return
        if a[0] == "remove" and len(a) == 2:
            target_ref = canonicalize_target_ref(a[1])
            await session.execute(delete(AllowedTarget).where(AllowedTarget.target_ref == target_ref))
            await session.commit()
            await message.answer("已删除授权目标。")
            return
    await message.answer("用法：/target_allowlist add <type> <target> [title] | remove <target> | list")


@router.message(Command("rate"))
async def rate(message: Message, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a = args(message)
    async with sessionmaker() as session:
        if not a or a[0] == "show":
            rows = await session.scalars(select(RateLimit).order_by(RateLimit.scope))
            lines = [f"{r.scope}: {r.max_actions}/{r.per_seconds}s jitter {r.jitter_min}-{r.jitter_max}s" for r in rows.all()]
            await message.answer("速率配置\n" + ("\n".join(lines) if lines else "暂无"))
            return
        if a[0] == "set" and len(a) == 6:
            try:
                max_actions, per_seconds, jitter_min, jitter_max = validate_rate_values(
                    int(a[2]), int(a[3]), int(a[4]), int(a[5])
                )
            except ValueError as exc:
                await message.answer(str(exc))
                return
            row = await session.scalar(select(RateLimit).where(RateLimit.scope == a[1]))
            if not row:
                row = RateLimit(scope=a[1])
                session.add(row)
            row.max_actions = max_actions
            row.per_seconds = per_seconds
            row.jitter_min = jitter_min
            row.jitter_max = jitter_max
            await session.commit()
            await message.answer("速率配置已更新。")
            return
    await message.answer("用法：/rate show | /rate set <scope> <max_actions> <per_seconds> <jitter_min> <jitter_max>")


async def run_one_or_many(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin: Admin | None,
    client_pool: ClientPool,
    job_type: str,
    account_ids: list[int],
    target_ref: str,
    func,
    *op_args,
) -> None:
    async with sessionmaker() as session:
        try:
            await require_allowed_target(session, target_ref)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=settings_panel())
            return
        rate = await get_rate(session, "batch")
        job = await create_job(session, job_type, {"target": target_ref, "accounts": account_ids})
        await audit(session, admin, job_type, "target", target_ref, {"accounts": account_ids})
        await session.commit()
        job_id = job.id
    gate = RateGate(rate)
    ok = failed = 0
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None:
            await message.answer("任务创建失败。")
            return
        try:
            for account_id in account_ids:
                await gate.wait()
                try:
                    result = await func(client_pool, account_id, target_ref, *op_args)
                    await add_job_item(session, job, account_id, target_ref, "ok", result=result)
                    ok += 1
                except Exception as exc:
                    await add_job_item(session, job, account_id, target_ref, "failed", error=str(exc))
                    failed += 1
                await session.commit()
            await finish_job(session, job, "finished_with_errors" if failed else "finished")
        except Exception as exc:
            await session.rollback()
            job = await session.get(Job, job_id)
            if job is not None:
                await finish_job(session, job, "failed", str(exc))
            await message.answer(f"{job_type} 任务失败：{exc}")
            await session.commit()
            return
        await session.commit()
    await message.answer(f"{job_type} 完成：成功 {ok}，失败 {failed}")


async def forward_to_target(
    client_pool: ClientPool,
    account_id: int,
    target: str,
    source: str,
    message_id: int,
) -> dict[str, int]:
    return await batch_ops.forward(client_pool, account_id, source, message_id, target)


@router.message(Command("send", "subscribe", "react", "unreact", "view_post", "forward"))
async def single_batch_command(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin: Admin | None,
    client_pool: ClientPool,
) -> None:
    command = (message.text or "").split()[0].lstrip("/")
    a = args(message)
    usage = "用法：/send <id> <target> <text> | /subscribe <id> <target> | /react <id> <target> <msg_id> <emoji> | /unreact <id> <target> <msg_id> | /view_post <id> <target> <msg_id> | /forward <id> <source> <msg_id> <target>"
    minimum_args = {"send": 3, "subscribe": 2, "react": 4, "unreact": 3, "view_post": 3, "forward": 4}
    if len(a) < minimum_args.get(command, 99):
        await message.answer(usage)
        return
    async with sessionmaker() as session:
        try:
            account_id = await resolve_account_id(session, a[0])
        except ValueError as exc:
            await message.answer(str(exc))
            return
    try:
        message_id = int(a[2]) if command in {"react", "unreact", "view_post", "forward"} else None
        if message_id is not None and message_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("消息 ID 必须为正整数。")
        return
    if command == "send":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "send", [account_id], a[1], batch_ops.send_message, " ".join(a[2:]))
    elif command == "subscribe":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "subscribe", [account_id], a[1], batch_ops.subscribe)
    elif command == "react":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "react", [account_id], a[1], batch_ops.react, message_id, a[3])
    elif command == "unreact":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "unreact", [account_id], a[1], batch_ops.unreact, message_id)
    elif command == "view_post":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "view_post", [account_id], a[1], batch_ops.view_post, message_id)
    elif command == "forward":
        source, msg_id, target = a[1], message_id, a[3]
        async with sessionmaker() as session:
            try:
                await require_allowed_target(session, source)
            except ValueError as exc:
                await message.answer(str(exc), reply_markup=settings_panel())
                return
        await run_one_or_many(message, sessionmaker, admin, client_pool, "forward", [account_id], target, forward_to_target, source, msg_id)


@router.message(Command("send_all", "subscribe_all", "react_all", "view_post_all", "forward_all"))
async def many_batch_command(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin: Admin | None,
    client_pool: ClientPool,
) -> None:
    command = (message.text or "").split()[0].lstrip("/")
    a = args(message)
    minimum_args = {"send_all": 4, "subscribe_all": 3, "react_all": 5, "view_post_all": 4, "forward_all": 5}
    if len(a) < minimum_args.get(command, 99):
        await message.answer("用法：<cmd> <start_id> <count> <target/source> ...")
        return
    try:
        start_id, count = int(a[0]), int(a[1])
        message_id = int(a[3]) if command in {"react_all", "view_post_all", "forward_all"} else None
        if start_id <= 0 or not 1 <= count <= 200 or (message_id is not None and message_id <= 0):
            raise ValueError
    except ValueError:
        await message.answer("起始账号 ID、数量和消息 ID 必须为正整数，数量最多为 200。")
        return
    async with sessionmaker() as session:
        account_ids = await account_ids_from_range(session, start_id, count)
    if command == "send_all":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "send_all", account_ids, a[2], batch_ops.send_message, " ".join(a[3:]))
    elif command == "subscribe_all":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "subscribe_all", account_ids, a[2], batch_ops.subscribe)
    elif command == "react_all":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "react_all", account_ids, a[2], batch_ops.react, message_id, a[4])
    elif command == "view_post_all":
        await run_one_or_many(message, sessionmaker, admin, client_pool, "view_post_all", account_ids, a[2], batch_ops.view_post, message_id)
    elif command == "forward_all":
        source, msg_id, target = a[2], message_id, a[4]
        async with sessionmaker() as session:
            try:
                await require_allowed_target(session, source)
            except ValueError as exc:
                await message.answer(str(exc), reply_markup=settings_panel())
                return
        await run_one_or_many(message, sessionmaker, admin, client_pool, "forward_all", account_ids, target, forward_to_target, source, msg_id)


@router.message(F.text.in_(MENU_TEXTS))
async def menu_text_command(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    action = MENU_TEXTS[message.text or ""]
    if action == "status":
        await message.answer(await status_text(sessionmaker, client_pool), reply_markup=main_menu())
    elif action == "accounts":
        text, accounts_list, page, pages = await accounts_text_and_rows(sessionmaker)
        await message.answer(
            text,
            reply_markup=accounts_panel(accounts_list, page, pages),
        )
    elif action == "login":
        await login_start(message, state)
    elif action == "qr_login":
        await qr_login_start(message, state, sessionmaker, client_pool)
    elif action == "import_session":
        await import_session_start(message, state)
    elif action == "import_sessions":
        await import_sessions_start(message, state)
    elif action == "export_session":
        await export_session_select(message, state)
    elif action == "batch":
        await message.answer("批量任务入口", reply_markup=batch_panel())
    elif action == "settings":
        await message.answer("目标白名单与速率配置", reply_markup=settings_panel())
    elif action == "security":
        async with sessionmaker() as session:
            text, panel = await login_email_guard_view(session, client_pool)
        await message.answer(text, reply_markup=panel)
    elif action == "monitor":
        await message.answer("监控中心", reply_markup=monitor_panel())
    elif action == "mini_app":
        panel = mini_app_panel()
        if panel is None:
            await message.answer(
                "内置应用未配置公开 HTTPS 地址。请设置 MINI_APP_PUBLIC_URL，例如 https://your-domain.example/mini-app。"
            )
        else:
            await message.answer("打开 Telegram 内置应用。", reply_markup=panel)
    elif action == "help":
        await message.answer(COMMANDS[:4096], reply_markup=main_menu())


@router.callback_query(F.data.startswith("nav:"))
async def nav_callback(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    parts = (callback.data or "").split(":")
    target = parts[1]
    if target == "home":
        await dismiss_panel(callback)
    elif target == "status":
        await answer_panel(callback, await status_text(sessionmaker, client_pool), main_menu())
    elif target == "accounts":
        try:
            requested_page = int(parts[2]) if len(parts) > 2 else 1
        except ValueError:
            requested_page = 1
        text, accounts_list, page, pages = await accounts_text_and_rows(
            sessionmaker, max(requested_page, 1)
        )
        await answer_panel(
            callback,
            text,
            accounts_panel(accounts_list, page, pages),
        )
    elif target == "batch":
        await answer_panel(callback, "批量任务入口", batch_panel())
    elif target == "settings":
        await answer_panel(callback, "目标白名单与速率配置", settings_panel())
    elif target == "monitor":
        await answer_panel(callback, "监控中心", monitor_panel())
    elif target == "security":
        async with sessionmaker() as session:
            text, panel = await login_email_guard_view(session, client_pool)
        await answer_panel(callback, text, panel)
    elif target == "help":
        await answer_panel(callback, COMMANDS[:4096], main_menu())
    elif target == "mini_app":
        panel = mini_app_panel()
        if panel is None:
            await answer_panel(
                callback,
                "内置应用未配置公开 HTTPS 地址。请设置 MINI_APP_PUBLIC_URL，例如 https://your-domain.example/mini-app。",
                main_menu(),
            )
        else:
            await answer_panel(callback, "打开 Telegram 内置应用。", panel)


@router.callback_query(F.data.startswith("flow:"))
async def flow_callback(callback: CallbackQuery, state: FSMContext) -> None:
    flow = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    if not callback.message:
        return
    if flow == "login":
        await state.clear()
        await state.set_state(LoginFlow.phone)
        await ask_with_cancel(callback.message, "请输入手机号，格式如 +8613800000000", "+8613800000000")
    elif flow == "import_session":
        await state.clear()
        await state.set_state(ImportSessionFlow.phone)
        await ask_with_cancel(callback.message, "请输入该 session 对应手机号。", "+8613800000000")
    elif flow == "import_sessions":
        await state.clear()
        await state.set_state(ImportSessionsFlow.payload)
        await ask_with_cancel(callback.message, "请上传 /export_sessions 导出的 txt 文件，或直接粘贴文件内容。", "上传 txt 文件或粘贴内容")
    elif flow == "export_session":
        await state.clear()
        await state.set_state(ExportSessionFlow.selection)
        await ask_with_cancel(callback.message, "请输入要导出的账号ID，支持 1,3,5-8。", "1,3,5-8")


@router.callback_query(F.data.startswith("acct:"))
async def account_callback(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parts = (callback.data or "").split(":")
    account_id = int(parts[1])
    accounts_page = int(parts[2]) if len(parts) > 2 else 1
    async with sessionmaker() as session:
        text = await account_detail_text(session, account_id)
    await answer_panel(
        callback,
        text,
        account_actions_panel(account_id, accounts_page),
    )


@router.callback_query(F.data.startswith("acct_action:"))
async def account_action_callback(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    _, action, account_id_raw = (callback.data or "").split(":", 2)
    account_id = int(account_id_raw)
    await callback.answer("处理中...")
    if not callback.message:
        return
    try:
        if action == "reconnect":
            async with sessionmaker() as session:
                account = await get_account(session, account_id)
                client = await client_pool.get_client(account.id)
                await account_ops.sync_me(session, account, client)
            text = f"重连成功：#{account_id}"
        elif action == "spam":
            client = await client_pool.get_client(account_id)
            async with sessionmaker() as session:
                record = await account_ops.spam_check(session, account_id, client)
            text = f"SpamBot 状态：{account_status_label(record.status_detected)}\n{(record.response_text or '')[:3500]}"
        elif action == "detail":
            client = await client_pool.get_client(account_id)
            live_2fa = await account_ops.get_2fa_info(client)
            async with sessionmaker() as session:
                text = await account_full_detail_text(session, account_id, live_2fa)
        elif action == "check_detail":
            client = await client_pool.get_client(account_id)
            live_2fa = await account_ops.get_2fa_info(client)
            async with sessionmaker() as session:
                await account_ops.spam_check(session, account_id, client)
                text = await account_full_detail_text(session, account_id, live_2fa)
        elif action == "avatar_random":
            client = await client_pool.get_client(account_id)
            text = "随机头像设置失败。"
            last_error = None
            for url in RANDOM_AVATAR_URLS:
                temp_path = Path(tempfile.gettempdir()) / f"tg_random_avatar_{account_id}_{int(datetime.now().timestamp())}.jpg"
                try:
                    await asyncio.to_thread(download_url_to_file, url, temp_path)
                    await account_ops.set_avatar(client, str(temp_path))
                    text = f"随机头像已更新。\n来源：{url}"
                    break
                except Exception as exc:
                    last_error = f"{url}: {type(exc).__name__}: {exc}"
                finally:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except OSError:
                            pass
            else:
                text = f"随机头像设置失败：{last_error}"
        elif action == "service":
            client = await client_pool.get_client(account_id)
            async with sessionmaker() as session:
                service_inserted = await account_ops.service_check(session, account_id, client)
            await client_pool.catch_up_recent_login_alerts(account_id, client)
            text = f"Telegram 777000 服务消息检查完成：新增 {service_inserted} 条。"
        elif action == "twofa":
            client = await client_pool.get_client(account_id)
            has_2fa = await account_ops.check_2fa(client)
            async with sessionmaker() as session:
                await account_ops.update_security_snapshot(session, account_id, has_2fa)
            text = f"2FA: {'已启用' if has_2fa else '未启用'}"
        elif action == "privacy":
            async with sessionmaker() as session:
                row = await session.get(PrivacySettings, account_id)
            text = f"隐私快照：{row.rules_json if row else '{}'}"
        elif action == "export_session":
            await send_session_export(callback.message, sessionmaker, [account_id])
            text = "Session 导出完成。"
        else:
            text = "未知账号操作。"
    except Exception as exc:
        text = f"操作失败：{exc}"
    await callback.message.answer(text, reply_markup=account_actions_panel(account_id))


@router.callback_query(F.data.startswith("acct_panel:"))
async def account_panel_callback(callback: CallbackQuery) -> None:
    _, panel, account_id_raw = (callback.data or "").split(":", 2)
    account_id = int(account_id_raw)
    if panel == "profile":
        await answer_panel(callback, "资料设置", profile_edit_panel(account_id))
    elif panel == "avatar":
        await answer_panel(callback, "头像设置", avatar_panel(account_id))
    elif panel == "privacy":
        await answer_panel(callback, "选择要设置的隐私项", privacy_keys_panel(account_id))
    elif panel == "twofa":
        await answer_panel(callback, "2FA 设置", twofa_panel(account_id))
    else:
        await answer_panel(callback, "未知账号面板。", account_actions_panel(account_id))


@router.callback_query(F.data.startswith("acct_edit:"))
async def account_edit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, action, account_id_raw = (callback.data or "").split(":", 2)
    account_id = int(account_id_raw)
    prompts = {
        "name": "请输入新姓名，格式：first [last]",
        "bio": "请输入新简介",
        "username": "请输入新用户名，不用带 @",
        "avatar_path": "请输入服务器上的头像图片路径",
        "avatar_upload": "请直接发送一张图片，或以文件形式发送图片。",
    }
    placeholders = {
        "name": "张 三",
        "bio": "账号简介",
        "username": "new_username",
        "avatar_path": "/root/avatar.jpg",
        "avatar_upload": "发送图片",
    }
    await state.clear()
    await state.set_state(ProfileEditFlow.value)
    await state.update_data(account_id=account_id, action=action)
    await ask_callback_with_cancel(callback, prompts.get(action, "请输入新值"), placeholders.get(action, "新值"))


@router.message(ProfileEditFlow.value)
async def profile_edit_value(
    message: Message,
    state: FSMContext,
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    data = await state.get_data()
    account_id = int(data["account_id"])
    action = data["action"]
    value = (message.text or "").strip()
    client = await client_pool.get_client(account_id)
    temp_path: Path | None = None
    try:
        if action == "name":
            parts = shlex.split(value)
            if not parts:
                await ask_with_cancel(message, "姓名不能为空，请重新输入。", "first [last]")
                return
            async with sessionmaker() as session:
                account = await get_account(session, account_id)
                await account_ops.set_name(client, parts[0], parts[1] if len(parts) > 1 else None)
                await account_ops.sync_me(session, account, client)
            text = "姓名已更新。"
        elif action == "bio":
            await account_ops.set_bio(client, value)
            text = "简介已更新。"
        elif action == "username":
            async with sessionmaker() as session:
                account = await get_account(session, account_id)
                await account_ops.set_username(client, value.lstrip("@"))
                await account_ops.sync_me(session, account, client)
            text = "用户名已更新。"
        elif action == "avatar_path":
            await account_ops.set_avatar(client, value)
            text = "头像已更新。"
        elif action == "avatar_upload":
            source = None
            suffix = ".jpg"
            if message.photo:
                source = message.photo[-1]
            elif message.document and (message.document.mime_type or "").startswith("image/"):
                source = message.document
                if message.document.file_name and "." in message.document.file_name:
                    suffix = "." + message.document.file_name.rsplit(".", 1)[1]
            if source is None:
                await ask_with_cancel(message, "请发送图片，或以文件形式发送图片。", "发送图片")
                return
            if getattr(source, "file_size", 0) and source.file_size > MAX_IMAGE_SIZE:
                await ask_with_cancel(message, "图片不能超过 5MB，请重新发送。", "发送图片")
                return
            with tempfile.NamedTemporaryFile(prefix="tg_avatar_", suffix=suffix, delete=False) as tmp:
                temp_path = Path(tmp.name)
            await bot.download(source, destination=temp_path)
            await account_ops.set_avatar(client, str(temp_path))
            text = "头像已更新。"
        else:
            text = "未知资料操作。"
    except Exception as exc:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        await message.answer(f"操作失败：{exc}", reply_markup=profile_edit_panel(account_id))
        return
    await state.clear()
    await message.answer(text, reply_markup=account_actions_panel(account_id))
    if temp_path is not None:
        try:
            temp_path.unlink()
        except OSError:
            pass


@router.callback_query(F.data.startswith("privacy_key:"))
async def privacy_key_callback(callback: CallbackQuery) -> None:
    _, key_name, account_id_raw = (callback.data or "").split(":", 2)
    await answer_panel(callback, f"选择 {key_name} 的可见范围", privacy_rules_panel(int(account_id_raw), key_name))


@router.callback_query(F.data.startswith("privacy_set:"))
async def privacy_set_callback(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    _, key_name, rule_name, account_id_raw = (callback.data or "").split(":", 3)
    account_id = int(account_id_raw)
    await callback.answer("处理中...")
    if not callback.message:
        return
    try:
        client = await client_pool.get_client(account_id)
        values = await account_ops.set_privacy(client, key_name, rule_name)
        async with sessionmaker() as session:
            await account_ops.save_privacy_snapshot(session, account_id, values)
        text = f"隐私设置已更新：{key_name} = {rule_name}"
    except Exception as exc:
        text = f"隐私设置失败：{exc}"
    await callback.message.answer(text, reply_markup=privacy_keys_panel(account_id))


@router.callback_query(F.data.startswith("twofa_edit:"))
async def twofa_edit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, action, account_id_raw = (callback.data or "").split(":", 2)
    account_id = int(account_id_raw)
    prompts = {
        "set": "请输入新 2FA 密码，可追加提示和邮箱：new_password [hint] [email]",
        "change": "请输入旧密码和新密码，可追加提示和邮箱：old_password new_password [hint] [email]",
        "email": "配置邮箱需要重新提交当前 2FA 密码。格式：current_password email [hint]",
        "disable": "请输入当前 2FA 密码",
    }
    placeholders = {
        "set": "new_password hint email@example.com",
        "change": "old_password new_password hint email@example.com",
        "email": "current_password email@example.com hint",
        "disable": "current_password",
    }
    await state.clear()
    await state.set_state(TwoFAEditFlow.value)
    await state.update_data(account_id=account_id, action=action)
    await ask_callback_with_cancel(callback, prompts.get(action, "请输入 2FA 参数"), placeholders.get(action, "2FA 参数"))


@router.message(TwoFAEditFlow.value)
async def twofa_edit_value(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    data = await state.get_data()
    account_id = int(data["account_id"])
    action = data["action"]
    try:
        parts = shlex.split(message.text or "")
    except ValueError:
        parts = (message.text or "").split()
    await delete_sensitive_input(message)
    client = await client_pool.get_client(account_id)
    try:
        if action == "set":
            if not parts:
                await ask_with_cancel(message, "请输入新 2FA 密码。", "new_password [hint] [email]")
                return
            hint, email = split_hint_email(parts, 1)
            try:
                await account_ops.edit_2fa(client, None, parts[0], hint, email, require_email_code if email else None)
            except EmailCodeRequired as exc:
                await state.update_data(password=parts[0], hint=hint, email=email)
                await state.set_state(TwoFAEditFlow.email_code)
                await ask_with_cancel(message, f"验证码已发送到邮箱，请输入 {exc.code_length} 位邮箱验证码。", "邮箱验证码")
                return
            async with sessionmaker() as session:
                await account_ops.update_security_snapshot(session, account_id, True, parts[0], hint, email)
            text = "2FA 已设置。"
        elif action == "change":
            if len(parts) < 2:
                await ask_with_cancel(message, "请输入旧密码和新密码。", "old_password new_password [hint] [email]")
                return
            hint, email = split_hint_email(parts, 2)
            try:
                await account_ops.edit_2fa(client, parts[0], parts[1], hint, email, require_email_code if email else None)
            except EmailCodeRequired as exc:
                await state.update_data(password=parts[1], hint=hint, email=email)
                await state.set_state(TwoFAEditFlow.email_code)
                await ask_with_cancel(message, f"验证码已发送到邮箱，请输入 {exc.code_length} 位邮箱验证码。", "邮箱验证码")
                return
            async with sessionmaker() as session:
                await account_ops.update_security_snapshot(session, account_id, True, parts[1], hint, email)
            text = "2FA 已修改。"
        elif action == "email":
            if len(parts) < 2 or "@" not in parts[1]:
                await ask_with_cancel(message, "请输入当前 2FA 密码和邮箱。", "current_password email@example.com [hint]")
                return
            current_password = parts[0]
            email = parts[1]
            hint = " ".join(parts[2:]) or None
            try:
                await account_ops.edit_2fa(
                    client,
                    current_password,
                    current_password,
                    hint,
                    email,
                    require_email_code,
                )
            except EmailCodeRequired as exc:
                await state.update_data(password=current_password, hint=hint, email=email)
                await state.set_state(TwoFAEditFlow.email_code)
                await ask_with_cancel(message, f"验证码已发送到邮箱，请输入 {exc.code_length} 位邮箱验证码。", "邮箱验证码")
                return
            async with sessionmaker() as session:
                await account_ops.update_security_snapshot(session, account_id, True, current_password, hint, email)
            text = "2FA 邮箱已配置。"
        elif action == "disable":
            if len(parts) != 1:
                await ask_with_cancel(message, "请输入当前 2FA 密码。", "current_password")
                return
            await account_ops.edit_2fa(client, parts[0], None)
            async with sessionmaker() as session:
                await account_ops.update_security_snapshot(session, account_id, False)
            text = "2FA 已关闭。"
        else:
            text = "未知 2FA 操作。"
    except PasswordHashInvalidError:
        await message.answer("2FA 密码错误，请重新输入。", reply_markup=twofa_panel(account_id))
        return
    except Exception as exc:
        await message.answer(f"2FA 操作失败：{exc}", reply_markup=twofa_panel(account_id))
        return
    await state.clear()
    await message.answer(text, reply_markup=twofa_panel(account_id))


@router.message(TwoFAEditFlow.email_code)
async def twofa_email_code_value(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> None:
    data = await state.get_data()
    account_id = int(data["account_id"])
    code = (message.text or "").strip()
    await delete_sensitive_input(message)
    client = await client_pool.get_client(account_id)
    try:
        await client(functions.account.ConfirmPasswordEmailRequest(code))
        async with sessionmaker() as session:
            await account_ops.update_security_snapshot(
                session,
                account_id,
                True,
                data.get("password"),
                data.get("hint"),
                data.get("email"),
            )
    except Exception as exc:
        await message.answer(f"邮箱验证码确认失败：{exc}", reply_markup=twofa_panel(account_id))
        return
    await state.clear()
    await message.answer("2FA 邮箱已确认并保存。", reply_markup=twofa_panel(account_id))


@router.callback_query(F.data.startswith("settings:"))
async def settings_callback(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    action = (callback.data or "").split(":", 1)[1]
    async with sessionmaker() as session:
        if action == "targets":
            rows = await session.scalars(select(AllowedTarget).order_by(AllowedTarget.id))
            lines = [f"#{r.id} {r.target_type} {r.target_ref} {r.title or ''}" for r in rows.all()]
            text = "授权目标\n" + ("\n".join(lines) if lines else "暂无")
        elif action == "rate":
            rows = await session.scalars(select(RateLimit).order_by(RateLimit.scope))
            lines = [
                f"{r.scope}: {r.max_actions}/{r.per_seconds}s jitter {r.jitter_min}-{r.jitter_max}s"
                for r in rows.all()
            ]
            text = "速率配置\n" + ("\n".join(lines) if lines else "暂无")
        else:
            text = "未知设置项。"
    await answer_panel(callback, text, settings_panel())


@router.callback_query(F.data.startswith("monitor:"))
async def monitor_callback(callback: CallbackQuery, client_pool: ClientPool) -> None:
    action = (callback.data or "").split(":", 1)[1]
    await callback.answer("处理中...")
    if not callback.message:
        return
    if action == "on":
        await client_pool.start_service_monitor()
        text = f"已连接 active 账号：{len(client_pool.connected_account_ids)}"
    elif action == "off":
        await client_pool.stop_service_monitor()
        text = "已断开所有实时监听。"
    elif action == "notify":
        text = "通知测试 OK。"
    else:
        text = "未知监控操作。"
    await callback.message.answer(text, reply_markup=monitor_panel())


async def login_email_guard_view(
    session: AsyncSession,
    client_pool: ClientPool,
) -> tuple[str, InlineKeyboardMarkup]:
    domains = await get_available_domains(session)
    selected_domain = await get_selected_domain(session)
    whitelisted_ids = await get_whitelist_ids(session)
    active_account_ids = set(
        (await session.scalars(select(TgSession.account_id).where(TgSession.is_active.is_(True)))).all()
    )
    connected_active_ids = active_account_ids.intersection(client_pool.connected_account_ids)
    protected_connected_ids = connected_active_ids.difference(whitelisted_ids)
    event_count = await session.scalar(
        select(func.count()).select_from(LoginEmailProtectionEvent)
    )
    configuration_ready = bool(
        domains
        and selected_domain
        and settings.login_email_gmail_username
        and settings.login_email_gmail_app_password
    )
    credentials_ready = bool(
        settings.login_email_gmail_username and settings.login_email_gmail_app_password
    )
    protection_status = login_email_runtime_status(
        configuration_ready=configuration_ready,
        monitor_enabled=client_pool.monitor_enabled,
        monitor_running=client_pool.service_monitor_running,
        active_count=len(active_account_ids),
        connected_count=len(connected_active_ids),
        protected_connected_count=len(protected_connected_ids),
        health_checked=client_pool.login_email_health_checked_at is not None,
        health_error=client_pool.login_email_health_error,
    )
    gmail_status = (
        "未检查"
        if client_pool.login_email_health_checked_at is None
        else "失败"
        if client_pool.login_email_health_error
        else "正常"
    )
    lines = [
        "安全防护中心",
        f"自动换绑开关：{'开启' if settings.login_email_protection_enabled else '关闭'}",
        f"运行状态：{protection_status}",
        f"Gmail 凭据：{'已填写' if credentials_ready else '未填写'}",
        f"Gmail IMAP：{gmail_status}",
        f"当前域名：@{selected_domain}" if selected_domain else "当前域名：未配置",
        f"实时监听：{'运行中' if client_pool.service_monitor_running else '未运行'}",
        f"自动保护账号：{len(protected_connected_ids)}/{len(active_account_ids)}",
        f"候选域名：{len(domains)} 个",
        f"白名单账号：{len(whitelisted_ids)} 个",
        f"保护事件：{event_count or 0} 条",
        "",
        "非白名单账号收到 777000 登录提醒后才会触发保护；白名单账号只转发通知。",
    ]
    if not settings.login_email_protection_enabled:
        lines.extend(["", "提示：环境变量中的自动保护开关当前为关闭状态。"])
    elif not credentials_ready:
        lines.extend(["", "提示：缺少可用的 Gmail 应用专用密码。"])
    elif not domains or not selected_domain:
        lines.extend(["", "提示：至少需要配置并选中一个登录邮箱域名。"])
    elif not client_pool.monitor_enabled:
        lines.extend(["", "提示：实时监听已关闭，自动换绑不会触发。请在监控中心重新开启。"])
    elif not client_pool.service_monitor_running:
        lines.extend(["", "提示：监听开关虽已开启，但后台任务未运行；请重启服务并检查日志。"])
    elif not active_account_ids:
        lines.extend(["", "提示：没有 active Session，当前没有可监听账号。"])
    elif not connected_active_ids:
        lines.extend(["", "提示：active 账号均未连接，请检查 Session 状态和服务日志。"])
    elif not protected_connected_ids:
        lines.extend(["", "提示：所有已连接账号都在白名单中，只会转发提醒，不会自动换绑。"])
    elif client_pool.login_email_health_error:
        lines.extend(["", "提示：Gmail IMAP 检查失败，请点击“检查 Gmail”查看具体原因。"])
    return "\n".join(lines), login_email_guard_panel()


async def login_email_domains_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    domains = await get_available_domains(session)
    selected = await get_selected_domain(session)
    lines = ["邮箱域名管理", "点击域名可设为默认；删除操作需要二次确认。", ""]
    lines.extend(
        f"{'当前 · ' if domain == selected else ''}@{domain}" for domain in domains
    )
    return "\n".join(lines), login_email_domains_panel(domains, selected)


async def login_email_whitelist_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    accounts = list(
        (await session.scalars(select(TgAccount).order_by(TgAccount.id).limit(30))).all()
    )
    whitelisted_ids = await get_whitelist_ids(session)
    text = (
        "登录保护白名单\n"
        "白名单账号收到 777000 登录提醒时，只把原通知转发给管理员，不自动换绑登录邮箱。\n\n"
        f"当前白名单：{len(whitelisted_ids)} 个"
    )
    return text, login_email_whitelist_panel(accounts, whitelisted_ids)


async def login_email_events_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    events = list(
        (
            await session.scalars(
                select(LoginEmailProtectionEvent)
                .where(LoginEmailProtectionEvent.parent_event_id.is_(None))
                .order_by(desc(LoginEmailProtectionEvent.id))
                .limit(20)
            )
        ).all()
    )
    text = "最近登录邮箱保护事件\n点击事件查看详情。" if events else "暂时没有保护事件。"
    return text, login_email_events_panel(events)


async def login_email_account_view(
    session: AsyncSession,
    account_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    account = await get_account(session, account_id)
    whitelisted = await session.get(LoginEmailWhitelist, account_id) is not None
    text = (
        f"账号 #{account.id} 登录邮箱保护\n"
        f"手机号：{account.phone_masked}\n"
        f"通知后等待：{'即时换绑' if account.login_email_window_hours == 0 else f'{account.login_email_window_hours} 小时'}\n"
        f"白名单：{'是，仅转发通知' if whitelisted else '否，允许自动换绑'}"
    )
    return text, login_email_account_panel(account_id, whitelisted)


@router.callback_query(F.data.startswith("emailguard:"))
async def login_email_guard_callback(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
    state: FSMContext,
) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "open"
    if action == "checkall":
        await callback.answer("正在执行全链路检测，请稍候...")
        async with sessionmaker() as session:
            report = await run_security_health_check(session, client_pool)
        if callback.message:
            await callback.message.answer(report.render()[:4096], reply_markup=login_email_guard_panel())
        return
    if action == "testimap":
        if not settings.login_email_gmail_username or not settings.login_email_gmail_app_password:
            await callback.answer("Gmail 凭据未填写", show_alert=True)
            return
        await callback.answer("正在检查 Gmail 连接...")
        try:
            await client_pool.check_login_email_health()
            if client_pool.login_email_health_error:
                raise RuntimeError(client_pool.login_email_health_error)
            result_text = "Gmail IMAP 检查成功，邮箱目录可只读访问。"
        except Exception as exc:
            result_text = f"Gmail IMAP 检查失败：{str(exc)[:800]}"
        if callback.message:
            await callback.message.answer(result_text, reply_markup=login_email_guard_panel())
        return
    async with sessionmaker() as session:
        if action == "open":
            text, panel = await login_email_guard_view(session, client_pool)
        elif action == "domains":
            text, panel = await login_email_domains_view(session)
        elif action == "whitelist":
            text, panel = await login_email_whitelist_view(session)
        elif action == "events":
            text, panel = await login_email_events_view(session)
        elif action == "account" and len(parts) == 3:
            text, panel = await login_email_account_view(session, int(parts[2]))
        elif action == "window" and len(parts) == 3:
            account_id = int(parts[2])
            account = await session.get(TgAccount, account_id)
            if account is None:
                await callback.answer("账号不存在", show_alert=True)
                return
            await state.set_state(LoginEmailWindowFlow.value)
            await state.update_data(account_id=account_id)
            await ask_callback_with_cancel(
                callback,
                (
                    f"账号 #{account_id} 当前为 "
                    f"{'0 小时（收到通知后立即换绑）' if account.login_email_window_hours == 0 else f'{account.login_email_window_hours} 小时'}。\n"
                    "请输入新的等待小时数，允许 0–720 的整数。"
                ),
                "例如 0、8 或 24",
            )
            return
        elif action == "domain" and len(parts) == 3:
            domains = await get_available_domains(session)
            try:
                domain = domains[int(parts[2])]
            except (IndexError, ValueError):
                await callback.answer("域名配置已变化，请重新打开", show_alert=True)
                return
            await set_selected_domain(session, domain)
            text, panel = await login_email_domains_view(session)
        elif action == "add":
            await state.set_state(LoginEmailDomainFlow.value)
            await ask_callback_with_cancel(
                callback,
                "请输入要添加的 catch-all 邮箱域名，例如 mail.example.com",
                "仅输入域名，不包含 @",
            )
            return
        elif action == "deleteask" and len(parts) == 3:
            domains = await get_available_domains(session)
            try:
                domain = domains[int(parts[2])]
            except (IndexError, ValueError):
                await callback.answer("域名配置已变化，请重新打开", show_alert=True)
                return
            await answer_panel(
                callback,
                f"确认删除邮箱域名 @{domain}？\n删除后不能用于自动换绑或快捷重试。",
                login_email_delete_confirm_panel(int(parts[2])),
            )
            return
        elif action == "delete" and len(parts) == 3:
            domains = await get_available_domains(session)
            try:
                domain = domains[int(parts[2])]
            except (IndexError, ValueError):
                await callback.answer("域名配置已变化，请重新打开", show_alert=True)
                return
            try:
                await delete_available_domain(session, domain)
            except ValueError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            text, panel = await login_email_domains_view(session)
        elif action == "white" and len(parts) == 3:
            account_id = int(parts[2])
            enabled = await session.get(LoginEmailWhitelist, account_id) is None
            await set_whitelisted(session, account_id, enabled)
            text, panel = await login_email_whitelist_view(session)
        elif action == "accounttoggle" and len(parts) == 3:
            account_id = int(parts[2])
            enabled = await session.get(LoginEmailWhitelist, account_id) is None
            await set_whitelisted(session, account_id, enabled)
            text, panel = await login_email_account_view(session, account_id)
        elif action == "event" and len(parts) == 3:
            event = await session.get(LoginEmailProtectionEvent, int(parts[2]))
            if event is None:
                await callback.answer("保护事件不存在", show_alert=True)
                return
            text = (
                f"保护事件 #{event.id}\n"
                f"账号：#{event.account_id}\n"
                f"状态：{event.status}\n"
                f"域名：@{event.selected_domain or '-'}\n"
                f"窗口提醒数：{event.alert_count}\n"
                f"窗口结束：{event.window_ends_at.isoformat() if event.window_ends_at else '-'}\n"
                f"最后提醒：{event.last_detected_at.isoformat() if event.last_detected_at else '-'}\n"
                f"尝试次数：{event.attempt_count}\n"
                f"检测时间：{event.detected_at.isoformat()}\n"
                f"确认时间：{event.confirmed_at.isoformat() if event.confirmed_at else '-'}\n"
                f"错误：{event.error or '-'}"
            )
            if event.status in {"failed", "interrupted"}:
                panel = login_email_retry_panel(
                    event.id,
                    await get_available_domains(session),
                    event.selected_domain,
                )
            else:
                panel = login_email_events_panel([])
        elif action == "retrymenu" and len(parts) == 3:
            event = await session.get(LoginEmailProtectionEvent, int(parts[2]))
            if event is None:
                await callback.answer("保护事件不存在", show_alert=True)
                return
            if event.status not in {"failed", "interrupted"}:
                await callback.answer(f"当前状态为 {event.status}，无需重试", show_alert=True)
                return
            text = (
                f"快捷换绑登录邮箱\n账号：#{event.account_id}\n"
                f"失败域名：@{event.selected_domain or '-'}\n"
                "请选择一个环境变量中配置的域名重新执行。"
            )
            panel = login_email_retry_panel(
                event.id,
                await get_available_domains(session),
                event.selected_domain,
            )
            await answer_panel(callback, text, panel)
            return
        elif action == "retry" and len(parts) == 4:
            event_id = int(parts[2])
            domains = await get_available_domains(session)
            try:
                domain = domains[int(parts[3])]
            except (IndexError, ValueError):
                await callback.answer("域名配置已变化，请重新打开", show_alert=True)
                return
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None:
                await callback.answer("保护事件不存在", show_alert=True)
                return
            if event.status not in {"failed", "interrupted"}:
                if event.status in {"requesting", "waiting_email"}:
                    remaining = login_email_wait_remaining(event.email_requested_at)
                    await callback.answer(
                        f"验证码已在处理，请等待至 {format_wait_deadline(remaining)}；本次未重复发码",
                        show_alert=True,
                    )
                else:
                    await callback.answer(
                        f"当前状态为 {event.status}，无法重试", show_alert=True
                    )
                return
            await set_selected_domain(session, domain)
            await client_pool.retry_login_email_protection(event_id, domain)
            await answer_panel(
                callback,
                f"已提交后台换绑任务\n账号：#{event.account_id}\n目标域名：@{domain}",
                login_email_guard_panel(),
            )
            return
        else:
            text, panel = await login_email_guard_view(session, client_pool)
    await answer_panel(callback, text, panel)


@router.message(LoginEmailDomainFlow.value)
async def login_email_domain_add(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    domain = (message.text or "").strip()
    await delete_sensitive_input(message)
    try:
        async with sessionmaker() as session:
            added = await add_available_domain(session, domain)
            text, panel = await login_email_domains_view(session)
    except ValueError as exc:
        await ask_with_cancel(
            message,
            f"添加失败：{exc}\n请重新输入域名。",
            "例如 mail.example.com",
        )
        return
    await state.clear()
    await message.answer(f"已添加 @{added}\n\n{text}", reply_markup=panel)


@router.message(LoginEmailWindowFlow.value)
async def login_email_window_update(
    message: Message,
    state: FSMContext,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    try:
        hours = parse_login_email_window_hours((message.text or "").strip())
    except ValueError as exc:
        await ask_with_cancel(
            message,
            f"设置失败：{exc}\n请重新输入。",
            "例如 0、8 或 24",
        )
        return
    data = await state.get_data()
    account_id = int(data.get("account_id") or 0)
    async with sessionmaker() as session:
        account = await session.get(TgAccount, account_id)
        if account is None:
            await state.clear()
            await message.answer("账号不存在，设置已取消。")
            return
        account.login_email_window_hours = hours
        admin = None
        if message.from_user is not None:
            admin = await session.scalar(
                select(Admin).where(Admin.telegram_user_id == message.from_user.id)
            )
        await audit(
            session,
            admin,
            "bot_login_email_window_update",
            "account",
            str(account_id),
            {"hours": hours},
        )
        await session.commit()
        text, panel = await login_email_account_view(session, account_id)
    await state.clear()
    behavior = "收到登录通知后立即换绑" if hours == 0 else f"收到登录通知 {hours} 小时后换绑"
    await message.answer(
        f"已保存：{behavior}。已开始的窗口不受影响。\n\n{text}",
        reply_markup=panel,
    )


@router.callback_query(F.data.startswith("template:"))
async def template_callback(callback: CallbackQuery) -> None:
    key = (callback.data or "").split(":", 1)[1]
    template = TEMPLATES.get(key)
    if not template:
        await answer_panel(callback, "未知模板。", main_menu())
        return
    await answer_panel(
        callback,
        f"请按需补全并发送：\n{template}",
        force_reply("补全命令后发送"),
    )


@router.message(Command("notify_test"))
async def notify_test(message: Message) -> None:
    await message.answer("通知测试 OK。")


@router.message(Command("backup"))
async def backup(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    admin: Admin | None,
) -> None:
    await message.answer("正在创建加密字段数据库备份，请稍候……")
    try:
        path = await create_database_backup_async("bot")
        async with sessionmaker() as session:
            await audit(
                session,
                admin,
                "database_backup",
                "backup",
                path.name,
                {"size": path.stat().st_size},
            )
            await session.commit()
    except Exception as exc:
        await message.answer(f"备份失败：{exc}", reply_markup=main_menu())
        return
    await message.answer(
        f"备份完成：{path.name}\n大小：{path.stat().st_size} 字节\n"
        "文件权限已设为 600；FERNET_KEY 仍需单独安全保存。",
        reply_markup=main_menu(),
    )


@router.message(F.text)
async def unknown(message: Message) -> None:
    await message.answer("未知指令，发送 /cmd 查看用法。")
