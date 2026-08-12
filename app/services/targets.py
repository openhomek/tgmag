from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AllowedTarget

TELEGRAM_LINK_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
INVITE_HASH_PATTERN = re.compile(r"[A-Za-z0-9_-]{5,128}")


def telegram_invite_hash(target_ref: str) -> str | None:
    value = target_ref.strip()
    candidate = ""
    if value.startswith("+"):
        candidate = value[1:]
    else:
        parsed = urlparse(value if "://" in value else "")
        if parsed.scheme in {"http", "https"} and parsed.hostname in TELEGRAM_LINK_HOSTS:
            path = parsed.path.strip("/")
            if path.startswith("+"):
                candidate = path[1:]
            elif path.lower().startswith("joinchat/"):
                candidate = path.split("/", 1)[1]
        elif parsed.scheme == "tg" and parsed.netloc.lower() == "join":
            candidate = parse_qs(parsed.query).get("invite", [""])[0]
    if candidate and INVITE_HASH_PATTERN.fullmatch(candidate):
        return candidate
    return None


def canonicalize_target_ref(target_ref: str) -> str:
    value = target_ref.strip()
    if not value:
        raise ValueError("目标不能为空")
    if value.lstrip("-").isdigit():
        return str(int(value))
    if value.startswith("@"):
        return "@" + value[1:].lower()
    invite_hash = telegram_invite_hash(value)
    if invite_hash:
        return f"https://t.me/+{invite_hash}"
    parsed = urlparse(value if "://" in value else "")
    if parsed.hostname in TELEGRAM_LINK_HOSTS:
        path = parsed.path.strip("/")
        if path and "/" not in path and not path.startswith("+"):
            return "@" + path.lower()
    return value


async def is_allowed_target(session: AsyncSession, target_ref: str) -> bool:
    normalized = canonicalize_target_ref(target_ref)
    if normalized.startswith("@"):
        condition = func.lower(AllowedTarget.target_ref) == normalized.lower()
    else:
        condition = AllowedTarget.target_ref == normalized
    target = await session.scalar(select(AllowedTarget).where(condition))
    return target is not None


async def require_allowed_target(session: AsyncSession, target_ref: str) -> None:
    if not await is_allowed_target(session, target_ref):
        raise ValueError("目标不在授权白名单中，请先使用 /target_allowlist add 添加。")
