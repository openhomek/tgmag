import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import (
    InviteHashExpiredError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)

from app.bot.handlers import (
    forward_to_target,
    login_email_guard_callback,
    login_email_runtime_status,
    parse_account_selection,
)
from app.config import settings
from app.services import security_health
from app.services.qr_code import login_qr_png
from app.services.rate_limit import validate_rate_values
from app.services.security_health import SecurityHealthCheck, SecurityHealthReport
from app.services.targets import canonicalize_target_ref, telegram_invite_hash
from app.tg import account_ops, batch_ops
from app.tg.account_ops import phone_from_user


def test_account_selection_is_bounded() -> None:
    assert parse_account_selection("1,3,5-7") == [1, 3, 5, 6, 7]
    with pytest.raises(ValueError):
        parse_account_selection("0")
    with pytest.raises(ValueError):
        parse_account_selection("1-201")


def test_login_qr_is_generated_locally_as_png() -> None:
    payload = login_qr_png("tg://login?token=test-token")
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_qr_login_phone_is_normalized_from_telegram_user() -> None:
    assert phone_from_user(SimpleNamespace(phone="447700900123")) == "+447700900123"
    with pytest.raises(ValueError, match="未返回账号手机号"):
        phone_from_user(SimpleNamespace(phone=None))


def test_account_save_rejects_the_fifty_first_account() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(side_effect=[None, 50]),
        scalars=AsyncMock(return_value=SimpleNamespace(all=list)),
    )
    user = SimpleNamespace(
        id=999001,
        username="capacity_test",
        first_name="Capacity",
        last_name="Test",
    )

    with pytest.raises(ValueError, match="50 个"):
        asyncio.run(
            account_ops.save_logged_in_account(
                session,
                "+447700900999",
                "test-session",
                user,
            )
        )

    session.execute.assert_awaited_once()
    assert session.scalar.await_count == 2


def test_target_canonicalization() -> None:
    assert canonicalize_target_ref(" @Example ") == "@example"
    assert canonicalize_target_ref("https://t.me/Example/") == "@example"
    assert canonicalize_target_ref("-1000123") == "-1000123"
    assert canonicalize_target_ref("https://telegram.me/joinchat/AbCd_12345") == (
        "https://t.me/+AbCd_12345"
    )
    assert canonicalize_target_ref("tg://join?invite=AbCd_12345") == (
        "https://t.me/+AbCd_12345"
    )
    assert telegram_invite_hash("https://t.me/+AbCd_12345") == "AbCd_12345"


def test_private_invite_subscribe_uses_import_request() -> None:
    client = AsyncMock(return_value=SimpleNamespace(chats=[SimpleNamespace(id=2406607000)]))
    pool = SimpleNamespace(get_client=AsyncMock(return_value=client))

    result = asyncio.run(batch_ops.subscribe(pool, 7, "https://t.me/+AbCd_12345"))

    assert result == {
        "joined": "https://t.me/+AbCd_12345",
        "status": "joined",
        "chat_id": 2406607000,
    }
    request = client.await_args.args[0]
    assert request.hash == "AbCd_12345"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UserAlreadyParticipantError(request=None), "already_member"),
        (InviteRequestSentError(request=None), "request_sent"),
    ],
)
def test_private_invite_subscribe_handles_non_failure_results(error, expected: str) -> None:
    client = AsyncMock(side_effect=error)
    pool = SimpleNamespace(get_client=AsyncMock(return_value=client))

    result = asyncio.run(batch_ops.subscribe(pool, 7, "+AbCd_12345"))

    assert result["status"] == expected


def test_private_invite_subscribe_explains_expired_links() -> None:
    client = AsyncMock(side_effect=InviteHashExpiredError(request=None))
    pool = SimpleNamespace(get_client=AsyncMock(return_value=client))

    with pytest.raises(ValueError, match="邀请链接已过期"):
        asyncio.run(batch_ops.subscribe(pool, 7, "https://t.me/+AbCd_12345"))


def test_private_group_id_subscribe_requests_an_invite_link() -> None:
    pool = SimpleNamespace(
        get_client=AsyncMock(return_value=SimpleNamespace(get_input_entity=AsyncMock())),
        sessionmaker=AsyncMock(),
    )
    pool.get_client.return_value.get_input_entity.side_effect = ValueError("unknown entity")

    with pytest.raises(ValueError, match="无法仅凭 ID 加入"):
        asyncio.run(batch_ops.subscribe(pool, 7, "-1002406607000"))


def test_rate_validation() -> None:
    assert validate_rate_values(8, 60, 2, 6) == (8, 60, 2, 6)
    with pytest.raises(ValueError):
        validate_rate_values(0, 60, 2, 6)
    with pytest.raises(ValueError):
        validate_rate_values(8, 60, 7, 6)


def test_forward_adapter_uses_source_message_target_order(monkeypatch: pytest.MonkeyPatch) -> None:
    call = AsyncMock(return_value={"message_id": 9})
    monkeypatch.setattr(batch_ops, "forward", call)
    pool = object()
    result = asyncio.run(forward_to_target(pool, 7, "@target", "@source", 42))
    assert result == {"message_id": 9}
    call.assert_awaited_once_with(pool, 7, "@source", 42, "@target")


def test_domain_add_prompt_has_clickable_cancel_button() -> None:
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="emailguard:add",
        answer=AsyncMock(),
        message=message,
    )
    state = SimpleNamespace(set_state=AsyncMock())

    asyncio.run(
        login_email_guard_callback(
            callback,
            lambda: Session(),
            object(),
            state,
        )
    )

    markup = message.answer.await_args.kwargs["reply_markup"]
    button = markup.inline_keyboard[0][0]
    assert button.text == "取消当前操作"
    assert button.callback_data == "flow:cancel"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"configuration_ready": False}, "配置不完整"),
        ({"monitor_enabled": False}, "实时监听已关闭"),
        (
            {"active_count": 0, "connected_count": 0, "protected_connected_count": 0},
            "没有 active 账号",
        ),
        ({"connected_count": 0, "protected_connected_count": 0}, "账号均未连接"),
        ({"protected_connected_count": 0}, "已连接账号均在白名单"),
        ({"health_checked": False}, "Gmail IMAP 尚未检查"),
        ({"health_error": "bad credentials"}, "Gmail IMAP 不可用"),
        ({"connected_count": 1}, "基础链路部分就绪（已连接 1/2）"),
        ({}, "基础链路就绪（待全链路检测）"),
    ],
)
def test_login_email_runtime_status_explains_non_operational_states(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected: str,
) -> None:
    monkeypatch.setattr(settings, "login_email_protection_enabled", True)
    values = {
        "configuration_ready": True,
        "monitor_enabled": True,
        "monitor_running": True,
        "active_count": 2,
        "connected_count": 2,
        "protected_connected_count": 2,
        "health_checked": True,
        "health_error": None,
    }
    values.update(overrides)
    assert login_email_runtime_status(**values) == expected


def test_login_email_runtime_status_reports_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "login_email_protection_enabled", False)
    assert (
        login_email_runtime_status(
            configuration_ready=True,
            monitor_enabled=True,
            monitor_running=True,
            active_count=1,
            connected_count=1,
            protected_connected_count=1,
            health_checked=True,
            health_error=None,
        )
        == "已停用"
    )


def test_login_email_runtime_status_detects_dead_monitor_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "login_email_protection_enabled", True)
    assert (
        login_email_runtime_status(
            configuration_ready=True,
            monitor_enabled=True,
            monitor_running=False,
            active_count=1,
            connected_count=1,
            protected_connected_count=1,
            health_checked=True,
            health_error=None,
        )
        == "实时监听任务未运行"
    )


def test_security_health_report_never_calls_warning_state_available() -> None:
    report = SecurityHealthReport(
        checks=(
            SecurityHealthCheck("数据库", "pass", "正常"),
            SecurityHealthCheck("端到端", "warn", "尚未实测", "完成一次测试"),
        ),
        checked_at=datetime.now(UTC),
    )
    assert report.available is False
    assert "⚠️ 未证实可用" in report.render()


def test_security_health_report_only_calls_all_pass_state_available() -> None:
    report = SecurityHealthReport(
        checks=(SecurityHealthCheck("端到端", "pass", "已验证"),),
        checked_at=datetime.now(UTC),
    )
    assert report.available is True
    assert "✅ 可用" in report.render()


def test_full_security_check_uses_current_runtime_state_without_historical_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "login_email_protection_enabled", True)
    monkeypatch.setattr(settings, "login_email_gmail_username", "admin@example.com")
    monkeypatch.setattr(settings, "login_email_gmail_app_password", "app-password")
    monkeypatch.setattr(
        security_health,
        "get_available_domains",
        AsyncMock(return_value=("mail.example.com",)),
    )
    monkeypatch.setattr(
        security_health,
        "get_selected_domain",
        AsyncMock(return_value="mail.example.com"),
    )
    monkeypatch.setattr(security_health, "get_whitelist_ids", AsyncMock(return_value=set()))

    session = SimpleNamespace(
        execute=AsyncMock(),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [1])),
        scalar=AsyncMock(return_value=None),
    )
    pool = SimpleNamespace(
        check_login_email_health=AsyncMock(return_value=True),
        login_email_health_error=None,
        monitor_enabled=True,
        service_monitor_running=True,
        connected_account_ids={1},
    )

    report = asyncio.run(security_health.run_security_health_check(session, pool))

    assert report.available is True
    assert "✅ 可用" in report.render()
    assert "最近端到端真实保护" not in report.render()
