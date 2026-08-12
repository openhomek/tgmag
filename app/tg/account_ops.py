from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, functions, types
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.custom.qrlogin import QRLogin

from app.config import settings
from app.db.models import (
    AccountSecurity,
    PrivacySettings,
    ServiceMessage,
    SpamCheck,
    TgAccount,
    TgSession,
)
from app.services.crypto import decrypt_text, encrypt_text, mask_phone

logger = logging.getLogger(__name__)

STATUS_NORMAL = "normal"
STATUS_LIMITED = "limited"
STATUS_BANNED = "banned"
STATUS_UNKNOWN = "unknown"
def sent_code_delivery_info(sent: types.auth.SentCode) -> dict[str, str | int | None]:
    code_type = sent.type
    type_name = type(code_type).__name__
    labels = {
        "SentCodeTypeApp": "Telegram App 内验证码",
        "SentCodeTypeSms": "短信验证码",
        "SentCodeTypeCall": "电话语音验证码",
        "SentCodeTypeFlashCall": "闪电来电验证码",
        "SentCodeTypeMissedCall": "未接来电验证码",
        "SentCodeTypeFragmentSms": "Fragment 短信验证码",
        "SentCodeTypeEmailCode": "邮箱验证码",
    }
    return {
        "type": type_name,
        "label": labels.get(type_name, type_name),
        "length": getattr(code_type, "length", None),
        "pattern": getattr(code_type, "pattern", None)
        or getattr(code_type, "phone_code_pattern", None)
        or getattr(code_type, "email_pattern", None),
        "timeout": getattr(sent, "timeout", None),
        "next_type": type(getattr(sent, "next_type", None)).__name__ if getattr(sent, "next_type", None) else None,
    }


async def start_login(phone: str) -> tuple[TelegramClient, str, dict[str, str | int | None]]:
    client = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception:
        await client.disconnect()
        raise
    delivery = sent_code_delivery_info(sent)
    logger.info(
        "Telegram accepted login code request for %s: type=%s next_type=%s timeout=%s",
        mask_phone(phone),
        delivery["type"],
        delivery["next_type"],
        delivery["timeout"],
    )
    return client, sent.phone_code_hash, delivery


async def start_qr_login() -> tuple[TelegramClient, QRLogin]:
    client = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        qr_login = await client.qr_login()
    except Exception:
        await client.disconnect()
        raise
    return client, qr_login


async def finish_authorized_login(client: TelegramClient) -> tuple[str, types.User]:
    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()
    return session_str, me


def phone_from_user(user: types.User) -> str:
    phone = str(getattr(user, "phone", "") or "").strip()
    if not phone:
        raise ValueError("Telegram 未返回账号手机号，无法保存二维码登录结果")
    return phone if phone.startswith("+") else f"+{phone}"


async def complete_login(
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None,
    transient_client: TelegramClient | None = None,
) -> tuple[str, types.User]:
    client = transient_client or TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    if not client.is_connected():
        await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            raise
        await client.sign_in(password=password)
    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()
    return session_str, me


async def complete_password_login(
    client: TelegramClient,
    password: str,
) -> tuple[str, types.User]:
    if not client.is_connected():
        await client.connect()
    await client.sign_in(password=password)
    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()
    return session_str, me


async def save_logged_in_account(
    session: AsyncSession,
    phone: str,
    session_str: str,
    me: types.User,
    twofa_password: str | None = None,
) -> TgAccount:
    account = await session.scalar(select(TgAccount).where(TgAccount.user_id == me.id))
    if account is None:
        phone_masked = mask_phone(phone)
        candidates = list(
            (await session.scalars(select(TgAccount).where(TgAccount.phone_masked == phone_masked))).all()
        )
        account = next(
            (row for row in candidates if decrypt_text(row.phone_encrypted) == phone),
            None,
        )
        if account is None:
            account = TgAccount(phone_encrypted=encrypt_text(phone), phone_masked=phone_masked)
            session.add(account)
            await session.flush()
    else:
        account.phone_encrypted = encrypt_text(phone)
        account.phone_masked = mask_phone(phone)
    account.user_id = me.id
    account.username = me.username
    account.first_name = me.first_name
    account.last_name = me.last_name
    if account.status in {"new", "active", "session_invalid"}:
        account.status = STATUS_UNKNOWN
    account.last_login_at = datetime.now(timezone.utc)
    account.last_error = None
    await session.execute(
        update(TgSession).where(TgSession.account_id == account.id).values(is_active=False)
    )
    session.add(
        TgSession(
            account_id=account.id,
            session_encrypted=encrypt_text(session_str),
            session_type="string",
            is_active=True,
        )
    )
    security = await session.get(AccountSecurity, account.id)
    if security is None:
        security = AccountSecurity(account_id=account.id)
        session.add(security)
    if twofa_password:
        security.has_2fa = True
        security.twofa_encrypted = encrypt_text(twofa_password)
    await session.commit()
    return account


async def import_session(session: AsyncSession, phone: str, session_str: str) -> TgAccount:
    client = TelegramClient(StringSession(session_str), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("session 未授权或已失效")
        me = await client.get_me()
    finally:
        await client.disconnect()
    return await save_logged_in_account(session, phone, session_str, me)


async def sync_me(session: AsyncSession, account: TgAccount, client: TelegramClient) -> TgAccount:
    me = await client.get_me()
    account.user_id = me.id
    account.username = me.username
    account.first_name = me.first_name
    account.last_name = me.last_name
    if account.status in {"new", "active"}:
        account.status = STATUS_UNKNOWN
    account.last_login_at = datetime.now(timezone.utc)
    account.last_error = None
    await session.commit()
    return account


async def set_name(client: TelegramClient, first_name: str, last_name: str | None) -> None:
    await client(functions.account.UpdateProfileRequest(first_name=first_name, last_name=last_name or ""))


async def set_bio(client: TelegramClient, bio: str) -> None:
    await client(functions.account.UpdateProfileRequest(about=bio))


async def set_username(client: TelegramClient, username: str) -> None:
    await client(functions.account.UpdateUsernameRequest(username=username))


async def set_avatar(client: TelegramClient, file_path: str) -> None:
    uploaded = await client.upload_file(Path(file_path))
    await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))


async def check_2fa(client: TelegramClient) -> bool:
    password = await client(functions.account.GetPasswordRequest())
    return bool(password.has_password)


async def get_2fa_info(client: TelegramClient) -> dict[str, str | bool | None]:
    password = await client(functions.account.GetPasswordRequest())
    login_email_pattern = getattr(password, "login_email_pattern", None)
    email_unconfirmed_pattern = getattr(password, "email_unconfirmed_pattern", None)
    return {
        "has_2fa": bool(password.has_password),
        "hint": getattr(password, "hint", None),
        "email_pattern": login_email_pattern or email_unconfirmed_pattern,
        "login_email_pattern": login_email_pattern,
        "email_unconfirmed_pattern": email_unconfirmed_pattern,
    }


async def edit_2fa(
    client: TelegramClient,
    current_password: str | None,
    new_password: str | None,
    hint: str | None = None,
    email: str | None = None,
    email_code_callback=None,
) -> None:
    await client.edit_2fa(
        current_password=current_password,
        new_password=new_password,
        hint=hint or "",
        email=email,
        email_code_callback=email_code_callback,
    )


async def send_login_email_code(client: TelegramClient, email: str) -> dict[str, str | int]:
    sent = await client(
        functions.account.SendVerifyEmailCodeRequest(
            purpose=types.EmailVerifyPurposeLoginChange(),
            email=email,
        ),
        flood_sleep_threshold=0,
    )
    return {"email_pattern": sent.email_pattern, "length": sent.length}


async def confirm_login_email(client: TelegramClient, code: str) -> Any:
    return await client(
        functions.account.VerifyEmailRequest(
            purpose=types.EmailVerifyPurposeLoginChange(),
            verification=types.EmailVerificationCode(code),
        ),
        flood_sleep_threshold=0,
    )


PRIVACY_KEYS = {
    "phone": types.InputPrivacyKeyPhoneNumber,
    "last_seen": types.InputPrivacyKeyStatusTimestamp,
    "profile_photo": types.InputPrivacyKeyProfilePhoto,
    "forwards": types.InputPrivacyKeyForwards,
    "calls": types.InputPrivacyKeyPhoneCall,
    "groups": types.InputPrivacyKeyChatInvite,
}

PRIVACY_RULES = {
    "everybody": types.InputPrivacyValueAllowAll,
    "contacts": types.InputPrivacyValueAllowContacts,
    "nobody": types.InputPrivacyValueDisallowAll,
}


async def set_privacy(client: TelegramClient, key_name: str, rule_name: str) -> dict[str, str]:
    key_factory = PRIVACY_KEYS[key_name]
    rule_factory = PRIVACY_RULES[rule_name]
    await client(functions.account.SetPrivacyRequest(key=key_factory(), rules=[rule_factory()]))
    return {key_name: rule_name}


def detect_spam_status(text: str) -> str:
    lowered = text.lower()
    if "no limits" in lowered or "free as a bird" in lowered:
        return STATUS_NORMAL
    if any(word in lowered for word in ["banned", "deleted", "permanently", "forever"]):
        return STATUS_BANNED
    if any(word in lowered for word in ["limited", "restriction", "restricted", "spam", "reported"]):
        return STATUS_LIMITED
    return STATUS_UNKNOWN


async def spam_check(session: AsyncSession, account_id: int, client: TelegramClient) -> SpamCheck:
    await client.send_message("SpamBot", "/start")
    await asyncio.sleep(2)
    messages = await client.get_messages("SpamBot", limit=1)
    text = messages[0].message if messages else ""
    status = detect_spam_status(text)
    record = SpamCheck(account_id=account_id, response_text=text, status_detected=status)
    session.add(record)
    account = await session.get(TgAccount, account_id)
    if account is not None:
        account.status = status
    await session.commit()
    return record


async def service_check(session: AsyncSession, account_id: int, client: TelegramClient, limit: int = 10) -> int:
    messages = await client.get_messages(777000, limit=limit)
    inserted = 0
    for msg in messages:
        text = msg.message or ""
        inserted += int(await save_service_message(session, account_id, 777000, msg.id, text, msg.date or datetime.now(timezone.utc)))
    await session.commit()
    return inserted


async def save_service_message(
    session: AsyncSession,
    account_id: int,
    source_user_id: int,
    message_id: int,
    text: str,
    received_at: datetime,
) -> bool:
    if not text:
        return False
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    exists = await session.scalar(
        select(ServiceMessage).where(
            ServiceMessage.account_id == account_id,
            ServiceMessage.source_user_id == source_user_id,
            ServiceMessage.message_id == message_id,
        )
    )
    if exists:
        if exists.text != text or exists.text_preview != text[:1000] or exists.text_hash != text_hash:
            exists.text = text
            exists.text_preview = text[:1000]
            exists.text_hash = text_hash
        return False
    session.add(
        ServiceMessage(
            account_id=account_id,
            source_user_id=source_user_id,
            message_id=message_id,
            text_hash=text_hash,
            text=text,
            text_preview=text[:1000],
            received_at=received_at,
        )
    )
    return True


async def update_security_snapshot(
    session: AsyncSession,
    account_id: int,
    has_2fa: bool,
    password: str | None = None,
    hint: str | None = None,
    email: str | None = None,
) -> None:
    security = await session.get(AccountSecurity, account_id)
    if security is None:
        security = AccountSecurity(account_id=account_id)
        session.add(security)
    security.has_2fa = has_2fa
    if not has_2fa:
        security.twofa_encrypted = None
        security.hint_encrypted = None
        security.email_encrypted = None
        await session.commit()
        return
    if password is not None:
        security.twofa_encrypted = encrypt_text(password)
    if hint is not None:
        security.hint_encrypted = encrypt_text(hint)
    if email is not None:
        security.email_encrypted = encrypt_text(email)
    await session.commit()


async def save_privacy_snapshot(session: AsyncSession, account_id: int, values: dict[str, str]) -> None:
    row = await session.get(PrivacySettings, account_id)
    if row is None:
        row = PrivacySettings(account_id=account_id, rules_json={})
        session.add(row)
    row.rules_json = {**(row.rules_json or {}), **values}
    await session.commit()


async def get_decrypted_session(session: AsyncSession, account_id: int) -> str:
    row = await session.scalar(
        select(TgSession)
        .where(TgSession.account_id == account_id, TgSession.is_active.is_(True))
        .order_by(TgSession.id.desc())
    )
    if not row:
        raise ValueError("账号没有可用 session")
    decrypted = decrypt_text(row.session_encrypted)
    if not decrypted:
        raise ValueError("session 解密失败")
    return decrypted
