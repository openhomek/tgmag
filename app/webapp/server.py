from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from sqlalchemy import String, cast, delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import functions
from telethon.errors import (
    BadRequestError,
    FloodError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from app.config import settings
from app.db.models import (
    AccountSecurity,
    Admin,
    AllowedTarget,
    Job,
    JobItem,
    LoginEmailProtectionEvent,
    PrivacySettings,
    RateLimit,
    ServiceMessage,
    SpamCheck,
    TgAccount,
    TgSession,
)
from app.services.account_deletion import delete_account_records
from app.services.audit import audit
from app.services.crypto import decrypt_text, encrypt_text
from app.services.jobs import add_job_item, create_job, finish_job
from app.services.login_email_protection import (
    format_wait_deadline,
    login_email_wait_remaining,
    parse_login_email_window_hours,
)
from app.services.pagination import ACCOUNT_PAGE_SIZE, account_page_window
from app.services.qr_code import login_qr_png
from app.services.rate_limit import RateGate, get_rate, validate_rate_values
from app.services.security_health import SecurityHealthReport, run_security_health_check
from app.services.targets import canonicalize_target_ref, require_allowed_target
from app.tg import account_ops, batch_ops
from app.tg.client_pool import ClientPool
from app.webapp.auth import MiniAppUser, require_admin

STATIC_DIR = Path(__file__).with_name("static")
MAX_REQUEST_SIZE = 5 * 1024 * 1024
logger = logging.getLogger(__name__)
RANDOM_AVATAR_URLS = [
    "https://api.btstu.cn/sjbz/api.php?lx=dongman&format=images",
    "https://api.btstu.cn/sjbz/api.php?lx=meizi&format=images",
    "https://img.xjh.me/random_img.php?return=302&type=bg&ctype=acg",
    "https://picsum.photos/1200/1200.jpg",
    "https://picsum.photos/1024/1024.jpg",
]


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (KeyError, ValueError) as exc:
        logger.info("Invalid Mini App request %s %s: %s", request.method, request.path, exc)
        raise web.HTTPBadRequest(text=str(exc)[:500] or "请求参数无效") from exc
    except Exception:
        logger.exception("Mini App request failed: %s %s", request.method, request.path)
        raise web.HTTPInternalServerError(text="服务器处理失败，请稍后重试")


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    if request.path.startswith(("/mini-app/api/", "/mini-app/static/")):
        response.headers["Cache-Control"] = "no-store"
    return response


class EmailCodeRequired(Exception):
    def __init__(self, code_length: int):
        self.code_length = code_length
        super().__init__(f"email confirmation code required: {code_length}")


def require_email_code(code_length: int) -> str:
    raise EmailCodeRequired(code_length)


def download_url_to_file(url: str, path: Path) -> None:
    if url not in RANDOM_AVATAR_URLS or not url.startswith("https://"):
        raise ValueError("不允许的随机头像来源")
    request = urllib.request.Request(url, headers={"User-Agent": "tg-account-bot/0.1"})
    # URL is selected from the immutable HTTPS allowlist above, never user input.
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
        payload = response.read(MAX_REQUEST_SIZE + 1)
    if len(payload) > MAX_REQUEST_SIZE:
        raise ValueError("图片超过 5MB 限制")
    header = payload[:16]
    if not header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")):
        raise ValueError("接口返回的不是可识别图片")
    path.write_bytes(payload)


def account_payload(account: TgAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "phone_masked": account.phone_masked,
        "user_id": account.user_id,
        "username": account.username,
        "first_name": account.first_name,
        "last_name": account.last_name,
        "name": " ".join(part for part in [account.first_name, account.last_name] if part) or "",
        "status": account.status,
        "login_email_window_hours": account.login_email_window_hours,
        "last_login_at": account.last_login_at.isoformat() if account.last_login_at else None,
        "last_error": account.last_error,
    }


def login_payload_key(user_id: int, login_id: str) -> str:
    return f"{user_id}:{login_id}"


def account_flow_key(user_id: int, account_id: int) -> str:
    return f"{user_id}:{account_id}"


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


async def build_session_export(
    session: AsyncSession, account_ids: list[int]
) -> tuple[str, int, list[str]]:
    lines = [
        "# Telethon StringSession export",
        f"# generated_at={datetime.now(UTC).isoformat()}",
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
        except Exception as exc:  # noqa: BLE001 - report per-account export failures
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


async def close_pending_login(pending: dict[str, Any] | None) -> None:
    if not pending:
        return
    client = pending.get("client")
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("Pending Telegram login disconnect failed", exc_info=True)


async def close_user_pending_logins(app: web.Application, user_id: int) -> None:
    prefix = f"{user_id}:"
    keys = [key for key in app["pending_logins"] if key.startswith(prefix)]
    for key in keys:
        await close_pending_login(app["pending_logins"].pop(key, None))


def qr_login_payload(qr_login: Any) -> dict[str, Any]:
    image = base64.b64encode(login_qr_png(qr_login.url)).decode("ascii")
    return {
        "qr_image": f"data:image/png;base64,{image}",
        "expires_at": qr_login.expires.isoformat(),
    }


def reusable_phone_login(
    app: web.Application,
    user_id: int,
    phone: str,
) -> tuple[str, dict[str, Any]] | None:
    prefix = f"{user_id}:"
    for key, pending in app["pending_logins"].items():
        if not key.startswith(prefix) or pending.get("method") != "phone":
            continue
        if pending.get("phone") != phone or pending.get("needs_password"):
            continue
        delivery = pending.get("delivery") or {}
        reuse_seconds = max(30, int(delivery.get("timeout") or 60))
        if time.time() - float(pending.get("created_at") or 0) < reuse_seconds:
            return key.split(":", 1)[1], pending
    return None


async def prune_pending_logins(app: web.Application, max_age_seconds: int = 600) -> None:
    now = time.time()
    stale_keys = [
        key
        for key, pending in app["pending_logins"].items()
        if now - float(pending.get("created_at") or 0)
        > (180 if pending.get("method") == "qr" else max_age_seconds)
    ]
    for key in stale_keys:
        await close_pending_login(app["pending_logins"].pop(key, None))


async def cleanup_pending_logins(app: web.Application) -> None:
    for pending in list(app["pending_logins"].values()):
        await close_pending_login(pending)
    app["pending_logins"].clear()


async def login_twofa_hint(client: Any) -> str:
    try:
        twofa_info = await account_ops.get_2fa_info(client)
        return str(twofa_info.get("hint") or "-")
    except Exception:  # noqa: BLE001 - a missing hint must not block a login flow
        return "-"


async def authenticated(request: web.Request) -> MiniAppUser:
    sessionmaker = request.app["sessionmaker"]
    return await require_admin(request, sessionmaker)


async def index(request: web.Request) -> web.FileResponse:
    response = web.FileResponse(STATIC_DIR / "index.html")
    # Telegram's embedded browsers can keep an entry document alive for a long
    # time. Always revalidate it so fixes to the versioned assets take effect.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


async def api_bootstrap(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    async with sessionmaker() as session:
        total = await session.scalar(select(func.count()).select_from(TgAccount))
        usable = await session.scalar(
            select(func.count(func.distinct(TgSession.account_id))).where(
                TgSession.is_active.is_(True)
            )
        )
        recent_accounts = list(
            (
                await session.scalars(
                    select(TgAccount).order_by(TgAccount.id.desc()).limit(6)
                )
            ).all()
        )
        targets = list(
            (await session.scalars(select(AllowedTarget).order_by(AllowedTarget.id))).all()
        )
        rates = list((await session.scalars(select(RateLimit).order_by(RateLimit.scope))).all())
        running_jobs = await session.scalar(
            select(func.count()).select_from(Job).where(Job.status == "running")
        )
    return web.json_response(
        {
            "user": {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
            "status": {
                "accounts": total or 0,
                "usable": usable or 0,
                "connected": len(client_pool.connected_account_ids),
                "monitor_enabled": client_pool.monitor_enabled,
                "monitor_running": client_pool.service_monitor_running,
                "running_jobs": running_jobs or 0,
                "server_time": datetime.now(UTC).isoformat(),
            },
            "recent_accounts": [account_payload(account) for account in recent_accounts],
            "targets": [
                {
                    "id": target.id,
                    "target_type": target.target_type,
                    "target_ref": target.target_ref,
                    "title": target.title,
                    "notes": target.notes,
                }
                for target in targets
            ],
            "rates": [
                {
                    "scope": rate.scope,
                    "max_actions": rate.max_actions,
                    "per_seconds": rate.per_seconds,
                    "jitter_min": rate.jitter_min,
                    "jitter_max": rate.jitter_max,
                }
                for rate in rates
            ],
        }
    )


def security_health_payload(report: SecurityHealthReport) -> dict[str, Any]:
    failed = sum(check.status == "fail" for check in report.checks)
    warnings = sum(check.status == "warn" for check in report.checks)
    return {
        "available": report.available,
        "summary": (
            "安全防护链路可用"
            if report.available
            else f"安全防护链路不可用：{failed} 项失败，{warnings} 项待验证"
        ),
        "checked_at": report.checked_at.isoformat(),
        "checks": [
            {
                "name": check.name,
                "status": check.status,
                "detail": check.detail,
                "fix": check.fix,
            }
            for check in report.checks
        ],
    }


async def api_security_health(request: web.Request) -> web.Response:
    """Run the real, non-destructive protection dependency checks on demand."""
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    async with sessionmaker() as session:
        report = await run_security_health_check(session, client_pool)
    return web.json_response(security_health_payload(report))


async def api_monitor_update(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    data = await request.json()
    action = str(data.get("action") or "").strip().lower()
    if action == "on":
        await client_pool.start_service_monitor()
        message = "实时监听已开启"
    elif action == "off":
        await client_pool.stop_service_monitor()
        message = "实时监听已关闭，所有账号连接已断开"
    else:
        raise web.HTTPBadRequest(text="unsupported monitor action")
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await audit(session, admin, f"webapp_monitor_{action}", "monitor", action)
        await session.commit()
    return web.json_response(
        {
            "ok": True,
            "message": message,
            "monitor_enabled": client_pool.monitor_enabled,
            "monitor_running": client_pool.service_monitor_running,
            "connected": len(client_pool.connected_account_ids),
        }
    )


async def api_jobs(request: web.Request) -> web.Response:
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    try:
        limit = min(max(int(request.query.get("limit", "8")), 1), 30)
    except ValueError as exc:
        raise web.HTTPBadRequest(text="limit must be an integer") from exc
    async with sessionmaker() as session:
        jobs = list((await session.scalars(select(Job).order_by(Job.id.desc()).limit(limit))).all())
        payload = []
        for job in jobs:
            counts = dict(
                (
                    await session.execute(
                        select(JobItem.status, func.count())
                        .where(JobItem.job_id == job.id)
                        .group_by(JobItem.status)
                    )
                ).all()
            )
            payload.append(
                {
                    "id": job.id,
                    "type": job.type,
                    "status": job.status,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                    "error": job.error,
                    "items": counts,
                }
            )
    return web.json_response({"jobs": payload})


def account_pagination(total: int, requested_page: int) -> tuple[int, int, int]:
    return account_page_window(total, requested_page)


async def api_accounts(request: web.Request) -> web.Response:
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    query = (request.query.get("q") or "").strip()
    try:
        requested_page = int(request.query.get("page", "1"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="page must be an integer") from exc
    if requested_page < 1:
        raise web.HTTPBadRequest(text="page must be at least 1")
    conditions = []
    if query:
        pattern = f"%{query}%"
        conditions.append(
            or_(
                cast(TgAccount.id, String).ilike(pattern),
                TgAccount.phone_masked.ilike(pattern),
                cast(TgAccount.user_id, String).ilike(pattern),
                TgAccount.username.ilike(pattern),
            )
        )
    async with sessionmaker() as session:
        total_statement = select(func.count()).select_from(TgAccount)
        statement = select(TgAccount)
        if conditions:
            total_statement = total_statement.where(*conditions)
            statement = statement.where(*conditions)
        total = int(await session.scalar(total_statement) or 0)
        page, pages, offset = account_pagination(total, requested_page)
        statement = (
            statement.order_by(TgAccount.id)
            .offset(offset)
            .limit(ACCOUNT_PAGE_SIZE)
        )
        rows = list((await session.scalars(statement)).all())
    return web.json_response(
        {
            "accounts": [account_payload(row) for row in rows],
            "pagination": {
                "page": page,
                "page_size": ACCOUNT_PAGE_SIZE,
                "pages": pages,
                "total": total,
            },
        }
    )


async def api_login_start(request: web.Request) -> web.Response:
    user = await authenticated(request)
    await prune_pending_logins(request.app)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    if not phone:
        raise web.HTTPBadRequest(text="phone required")
    reusable = reusable_phone_login(request.app, user.id, phone)
    if reusable is not None:
        login_id, pending = reusable
        delivery = pending.get("delivery") or {}
        detail = str(delivery.get("label") or "Telegram 指定方式")
        return web.json_response(
            {
                "ok": True,
                "login_id": login_id,
                "delivery": delivery,
                "reused": True,
                "message": f"上一条验证码请求仍有效（{detail}），本次未重复发码",
            }
        )
    phone_masked = account_ops.mask_phone(phone)
    async with sessionmaker() as session:
        candidates = list(
            (
                await session.scalars(
                    select(TgAccount).where(TgAccount.phone_masked == phone_masked)
                )
            ).all()
        )
        existing = next(
            (account for account in candidates if decrypt_text(account.phone_encrypted) == phone),
            None,
        )
        if existing is not None:
            active_session = await session.scalar(
                select(TgSession.id)
                .where(TgSession.account_id == existing.id, TgSession.is_active.is_(True))
                .limit(1)
            )
            if active_session is not None:
                return web.json_response(
                    {
                        "ok": True,
                        "already_exists": True,
                        "account": account_payload(existing),
                        "message": f"该手机号已在系统中：账号 #{existing.id}",
                    }
                )
    await close_user_pending_logins(request.app, user.id)
    try:
        client, phone_code_hash, delivery = await account_ops.start_login(phone)
    except PhoneNumberInvalidError as exc:
        raise web.HTTPBadRequest(text="手机号格式无效") from exc
    except PhoneNumberBannedError as exc:
        raise web.HTTPBadRequest(text="这个手机号被 Telegram 标记为不可登录/封禁") from exc
    except FloodWaitError as exc:
        raise web.HTTPTooManyRequests(
            text=f"请求过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再试"
        ) from exc
    login_id = uuid.uuid4().hex
    request.app["pending_logins"][login_payload_key(user.id, login_id)] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "delivery": delivery,
        "method": "phone",
        "created_at": time.time(),
    }
    detail = delivery["label"]
    if delivery.get("length"):
        detail = f"{detail}，{delivery['length']} 位"
    if delivery.get("pattern"):
        detail = f"{detail}，匹配：{delivery['pattern']}"
    return web.json_response(
        {
            "ok": True,
            "login_id": login_id,
            "delivery": delivery,
            "message": f"验证码已发送。送达方式：{detail}",
        }
    )


async def api_login_qr_start(request: web.Request) -> web.Response:
    user = await authenticated(request)
    await prune_pending_logins(request.app)
    await close_user_pending_logins(request.app, user.id)
    try:
        client, qr_login = await account_ops.start_qr_login()
    except FloodWaitError as exc:
        raise web.HTTPTooManyRequests(
            text=f"二维码请求过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再试"
        ) from exc
    login_id = uuid.uuid4().hex
    request.app["pending_logins"][login_payload_key(user.id, login_id)] = {
        "client": client,
        "qr_login": qr_login,
        "method": "qr",
        "poll_lock": asyncio.Lock(),
        "created_at": time.time(),
    }
    return web.json_response(
        {
            "ok": True,
            "login_id": login_id,
            "status": "waiting_scan",
            "message": "请使用已登录目标账号的 Telegram 客户端扫描并确认",
            **qr_login_payload(qr_login),
        }
    )


async def api_login_qr_poll(request: web.Request) -> web.Response:
    user = await authenticated(request)
    await prune_pending_logins(request.app)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    login_id = str(data.get("login_id") or "").strip()
    password = str(data.get("password") or "").strip() or None
    key = login_payload_key(user.id, login_id)
    pending = request.app["pending_logins"].get(key)
    if not pending or pending.get("method") != "qr":
        raise web.HTTPBadRequest(text="二维码登录流程不存在或已过期，请重新生成")
    pending["created_at"] = time.time()

    client = pending["client"]
    qr_login = pending["qr_login"]
    try:
        if pending.get("needs_password"):
            if not password:
                return web.json_response(
                    {
                        "ok": True,
                        "status": "needs_password",
                        "needs_password": True,
                        "hint": pending.get("password_hint") or "-",
                        "message": "扫码已确认，请输入目标账号的 2FA 密码",
                    }
                )
            session_str, me = await account_ops.complete_password_login(client, password)
        else:
            async with pending["poll_lock"]:
                remaining = max(
                    0.1,
                    (qr_login.expires - datetime.now(UTC)).total_seconds(),
                )
                try:
                    await qr_login.wait(timeout=min(20, remaining))
                except TimeoutError:
                    refreshed: dict[str, Any] = {}
                    if datetime.now(UTC) >= qr_login.expires:
                        await qr_login.recreate()
                        refreshed = qr_login_payload(qr_login)
                    return web.json_response(
                        {
                            "ok": True,
                            "status": "waiting_scan",
                            "message": "等待扫码确认",
                            **refreshed,
                        }
                    )
                except SessionPasswordNeededError:
                    pending["needs_password"] = True
                    pending["password_hint"] = await login_twofa_hint(client)
                    return web.json_response(
                        {
                            "ok": True,
                            "status": "needs_password",
                            "needs_password": True,
                            "hint": pending["password_hint"],
                            "message": "扫码已确认，请输入目标账号的 2FA 密码",
                        }
                    )
            session_str, me = await account_ops.finish_authorized_login(client)
    except PasswordHashInvalidError as exc:
        raise web.HTTPBadRequest(text="2FA 密码错误") from exc
    except FloodWaitError as exc:
        raise web.HTTPTooManyRequests(
            text=f"尝试过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再试"
        ) from exc
    except BadRequestError as exc:
        await close_pending_login(request.app["pending_logins"].pop(key, None))
        raise web.HTTPBadRequest(text="登录二维码已失效，请重新生成") from exc

    phone = account_ops.phone_from_user(me)
    async with sessionmaker() as session:
        account = await account_ops.save_logged_in_account(
            session, phone, session_str, me, password
        )
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await audit(session, admin, "webapp_qr_login_account", "account", str(account.id))
        await session.commit()
    request.app["pending_logins"].pop(key, None)
    return web.json_response(
        {
            "ok": True,
            "status": "complete",
            "account": account_payload(account),
            "message": f"二维码登录完成：账号 #{account.id} {account.phone_masked}",
        }
    )


async def api_login_qr_cancel(request: web.Request) -> web.Response:
    user = await authenticated(request)
    data = await request.json()
    login_id = str(data.get("login_id") or "").strip()
    key = login_payload_key(user.id, login_id)
    pending = request.app["pending_logins"].get(key)
    if pending is not None and pending.get("method") == "qr":
        await close_pending_login(request.app["pending_logins"].pop(key, None))
    return web.json_response({"ok": True, "message": "二维码登录已取消"})


async def api_login_verify(request: web.Request) -> web.Response:
    user = await authenticated(request)
    await prune_pending_logins(request.app)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    login_id = str(data.get("login_id") or "").strip()
    code = str(data.get("code") or "").strip()
    password = str(data.get("password") or "").strip() or None
    key = login_payload_key(user.id, login_id)
    pending = request.app["pending_logins"].get(key)
    if not pending:
        raise web.HTTPBadRequest(text="登录流程不存在或已过期，请重新发送验证码")
    try:
        if pending.get("needs_password"):
            if not password:
                hint = str(pending.get("password_hint") or "-")
                return web.json_response(
                    {
                        "ok": True,
                        "needs_password": True,
                        "hint": hint,
                        "message": f"该账号需要 2FA 密码，请填写后再次确认。密码提示：{hint}",
                    }
                )
            session_str, me = await account_ops.complete_password_login(pending["client"], password)
        else:
            try:
                session_str, me = await account_ops.complete_login(
                    phone=pending["phone"],
                    code=code,
                    phone_code_hash=pending["phone_code_hash"],
                    password=None,
                    transient_client=pending["client"],
                )
            except SessionPasswordNeededError:
                pending["needs_password"] = True
                hint = await login_twofa_hint(pending["client"])
                pending["password_hint"] = hint
                if not password:
                    return web.json_response(
                        {
                            "ok": True,
                            "needs_password": True,
                            "hint": hint,
                            "message": f"该账号需要 2FA 密码，请填写后再次确认。密码提示：{hint}",
                        }
                    )
                session_str, me = await account_ops.complete_password_login(
                    pending["client"], password
                )
    except SessionPasswordNeededError:
        hint = await login_twofa_hint(pending["client"])
        pending["needs_password"] = True
        pending["password_hint"] = hint
        return web.json_response(
            {
                "ok": True,
                "needs_password": True,
                "hint": hint,
                "message": f"该账号需要 2FA 密码，请填写后再次确认。密码提示：{hint}",
            }
        )
    except PasswordHashInvalidError as exc:
        raise web.HTTPBadRequest(text="2FA 密码错误") from exc
    except (PhoneCodeInvalidError, PhoneCodeEmptyError) as exc:
        raise web.HTTPBadRequest(text="验证码错误") from exc
    except PhoneCodeExpiredError as exc:
        await close_pending_login(request.app["pending_logins"].pop(key, None))
        raise web.HTTPBadRequest(text="验证码已过期，请重新开始登录") from exc
    except FloodWaitError as exc:
        raise web.HTTPTooManyRequests(
            text=f"尝试过于频繁，请在 {format_wait_deadline(exc.seconds)} 后再试"
        ) from exc
    phone = pending.get("phone") or account_ops.phone_from_user(me)
    async with sessionmaker() as session:
        account = await account_ops.save_logged_in_account(
            session, phone, session_str, me, password
        )
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await audit(session, admin, "webapp_login_account", "account", str(account.id))
        await session.commit()
    request.app["pending_logins"].pop(key, None)
    return web.json_response(
        {
            "ok": True,
            "account": account_payload(account),
            "message": f"登录完成：账号 #{account.id} {account.phone_masked}",
        }
    )


async def api_import_session(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    session_str = str(data.get("session") or "").strip()
    if not phone or not session_str:
        raise web.HTTPBadRequest(text="phone and session required")
    async with sessionmaker() as session:
        account = await account_ops.import_session(session, phone, session_str)
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await audit(session, admin, "webapp_import_session", "account", str(account.id))
        await session.commit()
    return web.json_response(
        {
            "ok": True,
            "account": account_payload(account),
            "message": f"导入完成：账号 #{account.id} {account.phone_masked}",
        }
    )


async def api_export_sessions(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    mode = str(data.get("mode") or "selection").strip()
    async with sessionmaker() as session:
        if mode == "range":
            account_ids = await active_ids_from_range(
                session, int(data["start_id"]), int(data["count"])
            )
        elif mode == "single":
            account_ids = [int(data["account_id"])]
        elif mode == "ids":
            account_ids = [int(value) for value in data.get("account_ids", [])]
        else:
            try:
                account_ids = parse_account_selection(str(data.get("selection") or ""))
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
        if not account_ids:
            raise web.HTTPBadRequest(text="没有可导出的账号")
        content, exported, skipped = await build_session_export(session, account_ids)
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await audit(
            session,
            admin,
            "webapp_export_sessions",
            "account",
            ",".join(str(account_id) for account_id in account_ids[:20]),
            {"account_ids": account_ids, "exported": exported, "skipped": skipped},
        )
        await session.commit()
    if exported == 0:
        raise web.HTTPBadRequest(text="没有可导出的 active session")
    filename = (
        f"tg_session_{account_ids[0]}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.txt"
        if len(account_ids) == 1
        else f"tg_sessions_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.txt"
    )
    return web.Response(
        body=content.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(exported),
            "X-Skipped-Count": str(len(skipped)),
        },
    )


async def api_account_detail(request: web.Request) -> web.Response:
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    account_id = int(request.match_info["account_id"])
    async with sessionmaker() as session:
        account = await session.get(TgAccount, account_id)
        if account is None:
            raise web.HTTPNotFound(text="account not found")
        security = await session.get(AccountSecurity, account_id)
        privacy = await session.get(PrivacySettings, account_id)
        latest_spam = await session.scalar(
            select(SpamCheck)
            .where(SpamCheck.account_id == account_id)
            .order_by(desc(SpamCheck.checked_at), desc(SpamCheck.id))
            .limit(1)
        )
        active_session = await session.scalar(
            select(TgSession.id)
            .where(TgSession.account_id == account_id, TgSession.is_active.is_(True))
            .order_by(TgSession.id.desc())
            .limit(1)
        )
    payload = account_payload(account)
    payload.update(
        {
            "has_active_session": bool(active_session),
            "has_2fa": bool(security and security.has_2fa),
            "privacy": privacy.rules_json if privacy else {},
            "latest_spam": {
                "status": latest_spam.status_detected,
                "checked_at": latest_spam.checked_at.isoformat(),
                "response_text": latest_spam.response_text,
            }
            if latest_spam
            else None,
        }
    )
    return web.json_response({"account": payload})


async def api_account_delete(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])

    await client_pool.begin_account_deletion(account_id)
    try:
        async with sessionmaker() as session:
            admin = await session.scalar(
                select(Admin).where(Admin.telegram_user_id == user.id)
            )
            try:
                result = await delete_account_records(session, account_id, admin)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await client_pool.end_account_deletion(account_id)

    account_key_suffix = f":{account_id}"
    for store_name in ("pending_twofa", "pending_login_email"):
        store = request.app.get(store_name, {})
        for key in [value for value in store if str(value).endswith(account_key_suffix)]:
            store.pop(key, None)

    return web.json_response(
        {
            "ok": True,
            "message": f"账号 #{result.account_id} 已从系统永久删除",
            "deleted_rows": result.deleted_rows,
        }
    )


async def api_account_action(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])
    data = await request.json()
    action = data.get("action")
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        account = await session.get(TgAccount, account_id)
        if account is None:
            raise web.HTTPNotFound(text="account not found")
        await audit(session, admin, f"webapp_account_{action}", "account", str(account_id))
        await session.commit()
    if action == "reconnect":
        async with sessionmaker() as session:
            account = await session.get(TgAccount, account_id)
            if account is None:
                raise web.HTTPNotFound(text="account not found")
            client = await client_pool.get_client(account_id)
            await account_ops.sync_me(session, account, client)
        return web.json_response({"ok": True, "message": f"账号 #{account_id} 重连成功"})
    if action == "spam":
        client = await client_pool.get_client(account_id)
        async with sessionmaker() as session:
            record = await account_ops.spam_check(session, account_id, client)
        return web.json_response(
            {
                "ok": True,
                "message": f"SpamBot 状态：{record.status_detected}",
                "status": record.status_detected,
                "response_text": record.response_text,
            }
        )
    if action == "service_check":
        client = await client_pool.get_client(account_id)
        async with sessionmaker() as session:
            service_inserted = await account_ops.service_check(session, account_id, client)
        await client_pool.catch_up_recent_login_alerts(account_id, client)
        return web.json_response(
            {"ok": True, "message": f"Telegram 777000 服务消息检查完成：新增 {service_inserted} 条"}
        )
    if action == "refresh_status":
        client = await client_pool.get_client(account_id)
        async with sessionmaker() as session:
            account = await session.get(TgAccount, account_id)
            if account is None:
                raise web.HTTPNotFound(text="account not found")
            await account_ops.sync_me(session, account, client)
            twofa_info = await account_ops.get_2fa_info(client)
            await account_ops.update_security_snapshot(
                session, account_id, bool(twofa_info.get("has_2fa"))
            )
            spam_record = await account_ops.spam_check(session, account_id, client)
            service_inserted = await account_ops.service_check(session, account_id, client)
        await client_pool.catch_up_recent_login_alerts(account_id, client)
        return web.json_response(
            {
                "ok": True,
                "message": (
                    f"刷新检测完成：SpamBot={spam_record.status_detected}，"
                    f"2FA={'已启用' if twofa_info.get('has_2fa') else '未启用'}，"
                    f"777000 服务消息新增 {service_inserted} 条"
                ),
                "spam_status": spam_record.status_detected,
                "twofa": twofa_info,
                "service_inserted": service_inserted,
            }
        )
    raise web.HTTPBadRequest(text="unsupported action")


async def api_account_profile_update(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])
    data = await request.json()
    client = await client_pool.get_client(account_id)
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        account = await session.get(TgAccount, account_id)
        if account is None:
            raise web.HTTPNotFound(text="account not found")
        if "first_name" in data or "last_name" in data:
            first_name = str(data.get("first_name") or "").strip()
            last_name = str(data.get("last_name") or "").strip() or None
            if not first_name:
                raise web.HTTPBadRequest(text="first_name required")
            await account_ops.set_name(client, first_name, last_name)
            await account_ops.sync_me(session, account, client)
        if "bio" in data:
            await account_ops.set_bio(client, str(data.get("bio") or ""))
        if "username" in data:
            username = str(data.get("username") or "").strip().lstrip("@")
            await account_ops.set_username(client, username)
            await account_ops.sync_me(session, account, client)
        await audit(session, admin, "webapp_profile_update", "account", str(account_id), data)
        await session.commit()
    return web.json_response({"ok": True, "message": "资料已更新"})


async def api_account_avatar_update(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])
    client = await client_pool.get_client(account_id)
    data = await request.post()
    mode = str(data.get("mode") or "").strip()
    temp_path: Path | None = None
    source = ""
    try:
        if mode == "upload":
            file_field = data.get("avatar")
            if not hasattr(file_field, "file"):
                raise web.HTTPBadRequest(text="avatar file required")
            filename = getattr(file_field, "filename", "") or "avatar.jpg"
            suffix = Path(filename).suffix or ".jpg"
            with tempfile.NamedTemporaryFile(
                prefix="tg_web_avatar_", suffix=suffix, delete=False
            ) as tmp:
                temp_path = Path(tmp.name)
                payload = file_field.file.read(MAX_REQUEST_SIZE + 1)
                if len(payload) > MAX_REQUEST_SIZE:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_REQUEST_SIZE,
                        actual_size=len(payload),
                    )
                tmp.write(payload)
            source = "upload"
        elif mode == "random":
            last_error = None
            for url in RANDOM_AVATAR_URLS:
                candidate = (
                    Path(tempfile.gettempdir())
                    / f"tg_web_random_avatar_{account_id}_{int(datetime.now(UTC).timestamp())}.jpg"
                )
                try:
                    await asyncio.to_thread(download_url_to_file, url, candidate)
                    temp_path = candidate
                    source = url
                    break
                except Exception as exc:  # noqa: BLE001 - retry every trusted avatar source
                    last_error = exc
                    if candidate.exists():
                        candidate.unlink(missing_ok=True)
            if temp_path is None:
                raise web.HTTPBadRequest(text=f"random avatar failed: {last_error}")
        else:
            raise web.HTTPBadRequest(text="unsupported avatar mode")
        await account_ops.set_avatar(client, str(temp_path))
        async with sessionmaker() as session:
            admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
            await audit(
                session,
                admin,
                "webapp_avatar_update",
                "account",
                str(account_id),
                {"mode": mode, "source": source},
            )
            await session.commit()
    finally:
        if mode in {"upload", "random"} and temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return web.json_response({"ok": True, "message": "头像已更新"})


async def api_account_privacy_update(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])
    data = await request.json()
    key_name = str(data.get("key") or "").strip()
    rule_name = str(data.get("rule") or "").strip()
    client = await client_pool.get_client(account_id)
    try:
        values = await account_ops.set_privacy(client, key_name, rule_name)
    except KeyError as exc:
        raise web.HTTPBadRequest(text="invalid privacy key or rule") from exc
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await account_ops.save_privacy_snapshot(session, account_id, values)
        await audit(session, admin, "webapp_privacy_set", "account", str(account_id), values)
        await session.commit()
    return web.json_response({"ok": True, "message": "隐私设置已更新", "values": values})


async def api_account_twofa(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])
    data = await request.json()
    action = str(data.get("action") or "").strip()
    client = await client_pool.get_client(account_id)
    if action == "check":
        info = await account_ops.get_2fa_info(client)
        async with sessionmaker() as session:
            await account_ops.update_security_snapshot(
                session, account_id, bool(info.get("has_2fa"))
            )
        return web.json_response({"ok": True, "info": info})
    if action == "confirm":
        code = str(data.get("code") or "").strip()
        pending_key = account_flow_key(user.id, account_id)
        pending = request.app["pending_twofa"].pop(pending_key, None)
        try:
            await client(functions.account.ConfirmPasswordEmailRequest(code))
        except Exception as exc:
            if pending is not None:
                request.app["pending_twofa"][pending_key] = pending
            raise web.HTTPBadRequest(text=str(exc)) from exc
        async with sessionmaker() as session:
            if pending:
                await account_ops.update_security_snapshot(
                    session,
                    account_id,
                    True,
                    pending.get("password"),
                    pending.get("hint"),
                    pending.get("email"),
                )
            admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
            await audit(session, admin, "webapp_twofa_confirm", "account", str(account_id))
            await session.commit()
        return web.json_response({"ok": True, "message": "2FA 邮箱已确认"})
    current_password = str(data.get("current_password") or "") or None
    new_password = str(data.get("new_password") or "") or None
    hint = str(data.get("hint") or "") or None
    email = str(data.get("email") or "") or None
    try:
        if action == "set":
            if not new_password:
                raise web.HTTPBadRequest(text="new_password required")
            await account_ops.edit_2fa(
                client, None, new_password, hint, email, require_email_code if email else None
            )
            snapshot_password = new_password
            snapshot_enabled = True
        elif action == "change":
            if not current_password or not new_password:
                raise web.HTTPBadRequest(text="current_password and new_password required")
            await account_ops.edit_2fa(
                client,
                current_password,
                new_password,
                hint,
                email,
                require_email_code if email else None,
            )
            snapshot_password = new_password
            snapshot_enabled = True
        elif action == "email":
            if not current_password or not email:
                raise web.HTTPBadRequest(text="current_password and email required")
            await account_ops.edit_2fa(
                client, current_password, current_password, hint, email, require_email_code
            )
            snapshot_password = current_password
            snapshot_enabled = True
        elif action == "disable":
            if not current_password:
                raise web.HTTPBadRequest(text="current_password required")
            await account_ops.edit_2fa(client, current_password, None)
            snapshot_password = None
            snapshot_enabled = False
        else:
            raise web.HTTPBadRequest(text="unsupported twofa action")
    except EmailCodeRequired as exc:
        request.app["pending_twofa"][account_flow_key(user.id, account_id)] = {
            "password": new_password or current_password,
            "hint": hint,
            "email": email,
        }
        return web.json_response(
            {
                "ok": True,
                "needs_code": True,
                "length": exc.code_length,
                "message": "验证码已发送到 2FA 邮箱",
            }
        )
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await account_ops.update_security_snapshot(
            session, account_id, snapshot_enabled, snapshot_password, hint, email
        )
        await audit(session, admin, f"webapp_twofa_{action}", "account", str(account_id))
        await session.commit()
    return web.json_response({"ok": True, "message": "2FA 操作完成"})


async def api_account_login_email(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    account_id = int(request.match_info["account_id"])
    data = await request.json()
    action = str(data.get("action") or "").strip()
    client = await client_pool.get_client(account_id)
    if action == "send":
        email = str(data.get("email") or "").strip()
        if "@" not in email:
            raise web.HTTPBadRequest(text="valid email required")
        pending_key = account_flow_key(user.id, account_id)
        pending = request.app["pending_login_email"].get(pending_key)
        if isinstance(pending, dict):
            elapsed = time.time() - float(pending.get("requested_at") or 0)
            remaining = settings.login_email_poll_timeout_seconds - elapsed
            if remaining > 0:
                raise web.HTTPConflict(
                    text=(
                        "该账号已发送换绑验证码，"
                        f"请等待至 {format_wait_deadline(remaining)}；本次未重复发码"
                    )
                )
            request.app["pending_login_email"].pop(pending_key, None)
        async with sessionmaker() as session:
            automatic_flow = await session.scalar(
                select(LoginEmailProtectionEvent)
                .where(
                    LoginEmailProtectionEvent.account_id == account_id,
                    LoginEmailProtectionEvent.status.in_({"requesting", "waiting_email"}),
                )
                .limit(1)
            )
        if automatic_flow is not None or client_pool.login_email_protector.has_change_in_progress(
            account_id
        ):
            remaining = login_email_wait_remaining(
                automatic_flow.email_requested_at if automatic_flow is not None else None
            )
            raise web.HTTPConflict(
                text=(
                    "该账号的换绑验证码正在等待邮件，"
                    f"请等待至 {format_wait_deadline(remaining)}；本次未重复发码"
                )
            )
        try:
            sent = await account_ops.send_login_email_code(client, email)
        except FloodWaitError as exc:
            raise web.HTTPTooManyRequests(
                text=(
                    "Telegram 限制尝试次数，"
                    f"请在 {format_wait_deadline(exc.seconds)} 后再试；本次未发送验证码"
                )
            ) from exc
        except FloodError as exc:
            raise web.HTTPTooManyRequests(
                text="Telegram 限制尝试次数，请稍后再试；请勿连续发码"
            ) from exc
        except Exception as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        request.app["pending_login_email"][pending_key] = {
            "email": email,
            "requested_at": time.time(),
        }
        wait_minutes = max(1, settings.login_email_poll_timeout_seconds // 60)
        return web.json_response(
            {
                "ok": True,
                "needs_code": True,
                **sent,
                "message": f"登录邮箱验证码已发送，{wait_minutes} 分钟内不会重复发码",
            }
        )
    if action == "confirm":
        code = str(data.get("code") or "").strip()
        pending_key = account_flow_key(user.id, account_id)
        pending = request.app["pending_login_email"].pop(pending_key, None)
        email = pending.get("email") if isinstance(pending, dict) else pending
        try:
            await account_ops.confirm_login_email(client, code)
        except Exception as exc:
            if email:
                request.app["pending_login_email"][pending_key] = pending
            if isinstance(exc, FloodWaitError):
                raise web.HTTPTooManyRequests(
                    text=(
                        "Telegram 限制尝试次数，"
                        f"请在 {format_wait_deadline(exc.seconds)} 后再试"
                    )
                ) from exc
            if isinstance(exc, FloodError):
                raise web.HTTPTooManyRequests(text="Telegram 限制尝试次数，请稍后再试") from exc
            raise web.HTTPBadRequest(text=str(exc)) from exc
        async with sessionmaker() as session:
            admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
            security = await session.get(AccountSecurity, account_id)
            if security is None:
                security = AccountSecurity(account_id=account_id, has_2fa=False)
                session.add(security)
            if email:
                security.login_email_encrypted = encrypt_text(email)
            await audit(
                session,
                admin,
                "webapp_login_email_confirm",
                "account",
                str(account_id),
                {"email": email},
            )
            await session.commit()
        return web.json_response({"ok": True, "message": "登录邮箱已确认"})
    raise web.HTTPBadRequest(text="unsupported login email action")


async def api_account_login_email_window(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    account_id = int(request.match_info["account_id"])
    data = await request.json()
    try:
        hours = parse_login_email_window_hours(data.get("hours"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    async with sessionmaker() as session:
        account = await session.get(TgAccount, account_id)
        if account is None:
            raise web.HTTPNotFound(text="account not found")
        account.login_email_window_hours = hours
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        await audit(
            session,
            admin,
            "webapp_login_email_window_update",
            "account",
            str(account_id),
            {"hours": hours},
        )
        await session.commit()
    behavior = "收到登录通知后立即换绑" if hours == 0 else f"收到登录通知 {hours} 小时后换绑"
    return web.json_response(
        {
            "ok": True,
            "hours": hours,
            "message": f"账号 #{account_id} 已设置为：{behavior}；已开始的窗口不受影响",
        }
    )


async def api_account_service_messages(request: web.Request) -> web.Response:
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    account_id = int(request.match_info["account_id"])
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 100))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="limit 必须是整数") from exc
    async with sessionmaker() as session:
        rows = list(
            (
                await session.scalars(
                    select(ServiceMessage)
                    .where(ServiceMessage.account_id == account_id)
                    .order_by(desc(ServiceMessage.received_at), desc(ServiceMessage.id))
                    .limit(limit)
                )
            ).all()
        )
    return web.json_response(
        {
            "messages": [
                {
                    "id": row.id,
                    "source_user_id": row.source_user_id,
                    "message_id": row.message_id,
                    "text": row.text or row.text_preview or "",
                    "text_preview": row.text_preview,
                    "received_at": row.received_at.isoformat(),
                    "notified_at": row.notified_at.isoformat() if row.notified_at else None,
                }
                for row in rows
            ]
        }
    )


async def api_targets(request: web.Request) -> web.Response:
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    async with sessionmaker() as session:
        rows = list((await session.scalars(select(AllowedTarget).order_by(AllowedTarget.id))).all())
    return web.json_response(
        {
            "targets": [
                {
                    "id": row.id,
                    "target_type": row.target_type,
                    "target_ref": row.target_ref,
                    "title": row.title,
                    "notes": row.notes,
                }
                for row in rows
            ]
        }
    )


async def api_targets_update(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    action = data.get("action")
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        if action == "add":
            target_type = str(data.get("target_type") or "channel").strip()
            try:
                target_ref = canonicalize_target_ref(str(data.get("target_ref") or ""))
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
            exists = await session.scalar(
                select(AllowedTarget.id).where(
                    AllowedTarget.target_ref == target_ref,
                )
            )
            if exists is not None:
                raise web.HTTPConflict(text="该授权目标已经存在")
            session.add(
                AllowedTarget(
                    target_type=target_type,
                    target_ref=target_ref,
                    title=str(data.get("title") or "").strip() or None,
                    notes=str(data.get("notes") or "").strip() or None,
                )
            )
            await audit(session, admin, "webapp_target_add", "target", target_ref)
            await session.commit()
            return web.json_response({"ok": True, "message": "授权目标已添加"})
        if action == "remove":
            try:
                target_ref = canonicalize_target_ref(str(data.get("target_ref") or ""))
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
            await session.execute(
                delete(AllowedTarget).where(AllowedTarget.target_ref == target_ref)
            )
            await audit(session, admin, "webapp_target_remove", "target", target_ref)
            await session.commit()
            return web.json_response({"ok": True, "message": "授权目标已删除"})
    raise web.HTTPBadRequest(text="unsupported action")


async def api_rates(request: web.Request) -> web.Response:
    await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    async with sessionmaker() as session:
        rows = list((await session.scalars(select(RateLimit).order_by(RateLimit.scope))).all())
    return web.json_response(
        {
            "rates": [
                {
                    "scope": row.scope,
                    "max_actions": row.max_actions,
                    "per_seconds": row.per_seconds,
                    "jitter_min": row.jitter_min,
                    "jitter_max": row.jitter_max,
                }
                for row in rows
            ]
        }
    )


async def api_rate_update(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    data = await request.json()
    scope = str(data.get("scope") or "batch").strip()
    try:
        max_actions, per_seconds, jitter_min, jitter_max = validate_rate_values(
            int(data["max_actions"]),
            int(data["per_seconds"]),
            int(data.get("jitter_min") or 0),
            int(data.get("jitter_max") or 0),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    async with sessionmaker() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        row = await session.scalar(select(RateLimit).where(RateLimit.scope == scope))
        if row is None:
            row = RateLimit(scope=scope)
            session.add(row)
        row.max_actions = max_actions
        row.per_seconds = per_seconds
        row.jitter_min = jitter_min
        row.jitter_max = jitter_max
        await audit(session, admin, "webapp_rate_set", "rate", scope)
        await session.commit()
    return web.json_response({"ok": True, "message": "速率配置已更新"})


async def active_ids_from_range(session: AsyncSession, start_id: int, count: int) -> list[int]:
    if start_id <= 0 or not 1 <= count <= 200:
        raise web.HTTPBadRequest(text="start_id 必须为正整数，count 必须在 1 到 200 之间")
    rows = await session.scalars(
        select(TgAccount.id)
        .join(TgSession, TgSession.account_id == TgAccount.id)
        .where(TgAccount.id >= start_id, TgSession.is_active.is_(True))
        .order_by(TgAccount.id)
        .limit(count)
    )
    return list(rows.all())


async def batch_accounts(session: AsyncSession, data: dict[str, Any]) -> list[int]:
    mode = data.get("account_mode")
    if mode == "range":
        return await active_ids_from_range(session, int(data["start_id"]), int(data["count"]))
    account_ids = [int(value) for value in data.get("account_ids", [])]
    if not account_ids:
        raise web.HTTPBadRequest(text="no accounts selected")
    if len(account_ids) > 200 or any(account_id <= 0 for account_id in account_ids):
        raise web.HTTPBadRequest(text="账号ID必须为正整数，一次最多选择 200 个账号")
    return account_ids


async def api_batch_run(request: web.Request) -> web.Response:
    user = await authenticated(request)
    sessionmaker = request.app["sessionmaker"]
    client_pool: ClientPool = request.app["client_pool"]
    data = await request.json()
    job_type = str(data.get("type") or "").strip()
    target_ref = str(data.get("target") or "").strip()
    if job_type not in {"send", "subscribe", "react", "unreact", "view_post", "forward"}:
        raise web.HTTPBadRequest(text="unsupported batch type")
    async with sessionmaker() as session:
        account_ids = await batch_accounts(session, data)
        if job_type == "forward":
            source = str(data.get("source") or "").strip()
            if not source:
                raise web.HTTPBadRequest(text="source required")
            try:
                await require_allowed_target(session, source)
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
        if not target_ref:
            raise web.HTTPBadRequest(text="target required")
        try:
            await require_allowed_target(session, target_ref)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        rate = await get_rate(session, "batch")
        admin = await session.scalar(select(Admin).where(Admin.telegram_user_id == user.id))
        job = await create_job(
            session,
            f"webapp_{job_type}",
            {"target": target_ref, "accounts": account_ids, "payload": data},
        )
        await audit(
            session, admin, f"webapp_{job_type}", "target", target_ref, {"accounts": account_ids}
        )
        await session.commit()
        job_id = job.id

    gate = RateGate(rate)
    ok = failed = 0
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise web.HTTPInternalServerError(text="job not found")
        try:
            for account_id in account_ids:
                await gate.wait()
                try:
                    result = await run_batch_call(
                        client_pool, job_type, account_id, target_ref, data
                    )
                    await add_job_item(session, job, account_id, target_ref, "ok", result=result)
                    ok += 1
                except Exception as exc:  # noqa: BLE001 - one account must not abort a batch
                    await add_job_item(
                        session, job, account_id, target_ref, "failed", error=str(exc)
                    )
                    failed += 1
                await session.commit()
            await finish_job(session, job, "finished_with_errors" if failed else "finished")
            await session.commit()
        except Exception as exc:
            await session.rollback()
            job = await session.get(Job, job_id)
            if job is not None:
                await finish_job(session, job, "failed", str(exc))
                await session.commit()
            raise
    return web.json_response(
        {"ok": True, "job_id": job_id, "message": f"任务完成：成功 {ok}，失败 {failed}"}
    )


async def run_batch_call(
    client_pool: ClientPool,
    job_type: str,
    account_id: int,
    target_ref: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    if job_type == "send":
        return await batch_ops.send_message(
            client_pool, account_id, target_ref, str(data.get("text") or "")
        )
    if job_type == "subscribe":
        return await batch_ops.subscribe(client_pool, account_id, target_ref)
    if job_type == "react":
        return await batch_ops.react(
            client_pool,
            account_id,
            target_ref,
            int(data["message_id"]),
            str(data["emoji"]),
        )
    if job_type == "unreact":
        return await batch_ops.unreact(client_pool, account_id, target_ref, int(data["message_id"]))
    if job_type == "view_post":
        return await batch_ops.view_post(
            client_pool, account_id, target_ref, int(data["message_id"])
        )
    if job_type == "forward":
        return await batch_ops.forward(
            client_pool,
            account_id,
            str(data["source"]),
            int(data["message_id"]),
            target_ref,
        )
    raise ValueError("unsupported batch type")


async def create_webapp(
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, security_headers_middleware],
        client_max_size=MAX_REQUEST_SIZE,
    )
    app["sessionmaker"] = sessionmaker
    app["client_pool"] = client_pool
    app["pending_logins"] = {}
    app["pending_twofa"] = {}
    app["pending_login_email"] = {}
    app.on_cleanup.append(cleanup_pending_logins)
    app.router.add_get("/mini-app", index)
    app.router.add_get("/mini-app/", index)
    app.router.add_static("/mini-app/static", STATIC_DIR)
    app.router.add_get("/mini-app/api/bootstrap", api_bootstrap)
    app.router.add_get("/mini-app/api/security-health", api_security_health)
    app.router.add_post("/mini-app/api/monitor", api_monitor_update)
    app.router.add_get("/mini-app/api/jobs", api_jobs)
    app.router.add_get("/mini-app/api/accounts", api_accounts)
    app.router.add_post("/mini-app/api/accounts/login/start", api_login_start)
    app.router.add_post("/mini-app/api/accounts/login/verify", api_login_verify)
    app.router.add_post("/mini-app/api/accounts/login/qr/start", api_login_qr_start)
    app.router.add_post("/mini-app/api/accounts/login/qr/poll", api_login_qr_poll)
    app.router.add_post("/mini-app/api/accounts/login/qr/cancel", api_login_qr_cancel)
    app.router.add_post("/mini-app/api/accounts/import-session", api_import_session)
    app.router.add_post("/mini-app/api/accounts/export-sessions", api_export_sessions)
    app.router.add_get("/mini-app/api/accounts/{account_id:\\d+}", api_account_detail)
    app.router.add_delete("/mini-app/api/accounts/{account_id:\\d+}", api_account_delete)
    app.router.add_post("/mini-app/api/accounts/{account_id:\\d+}/action", api_account_action)
    app.router.add_post(
        "/mini-app/api/accounts/{account_id:\\d+}/profile", api_account_profile_update
    )
    app.router.add_post(
        "/mini-app/api/accounts/{account_id:\\d+}/avatar", api_account_avatar_update
    )
    app.router.add_post(
        "/mini-app/api/accounts/{account_id:\\d+}/privacy", api_account_privacy_update
    )
    app.router.add_post("/mini-app/api/accounts/{account_id:\\d+}/twofa", api_account_twofa)
    app.router.add_post(
        "/mini-app/api/accounts/{account_id:\\d+}/login-email", api_account_login_email
    )
    app.router.add_put(
        "/mini-app/api/accounts/{account_id:\\d+}/login-email-window",
        api_account_login_email_window,
    )
    app.router.add_get(
        "/mini-app/api/accounts/{account_id:\\d+}/service-messages", api_account_service_messages
    )
    app.router.add_get("/mini-app/api/targets", api_targets)
    app.router.add_post("/mini-app/api/targets", api_targets_update)
    app.router.add_get("/mini-app/api/rates", api_rates)
    app.router.add_post("/mini-app/api/rates", api_rate_update)
    app.router.add_post("/mini-app/api/batch/run", api_batch_run)
    return app


async def start_webapp(
    sessionmaker: async_sessionmaker[AsyncSession],
    client_pool: ClientPool,
) -> web.AppRunner | None:
    if not settings.mini_app_enabled:
        return None
    app = await create_webapp(sessionmaker, client_pool)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.mini_app_host, settings.mini_app_port)
    await site.start()
    return runner
