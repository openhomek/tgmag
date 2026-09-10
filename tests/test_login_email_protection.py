from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError

from app.config import settings
from app.services import login_email_protection
from app.services.login_email_protection import (
    GmailCodeReader,
    LoginEmailProtector,
    build_alias,
    format_wait_deadline,
    is_login_code_alert,
    login_email_wait_remaining,
    normalize_domain,
    parse_telegram_login_email,
    recover_incomplete_events,
)
from app.tg import account_ops

SAMPLE_EMAIL = b"""From: Telegram <noreply@telegram.org>
To: testime001@mail.example.com
Date: Wed, 5 Aug 2026 13:46:00 +0800
Subject: Your Code - 853353
Content-Type: text/plain; charset=utf-8

Dear he,

Your code is: 853353. Use it to verify your email for Login.

If you didn't request this, simply ignore this message.

Yours,
The Telegram Team
"""


def test_wait_message_shows_deadline_instead_of_remaining_duration(monkeypatch) -> None:
    monkeypatch.setattr(settings, "login_email_poll_timeout_seconds", 300)
    requested_at = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    remaining = login_email_wait_remaining(
        requested_at,
        now=datetime(2026, 8, 8, 12, 1, 29, 100000, tzinfo=UTC),
    )

    assert (
        format_wait_deadline(remaining, now=datetime(2026, 8, 8, 12, 1, 29, 100000, tzinfo=UTC))
        == "20:05:00"
    )
    assert (
        format_wait_deadline(300, now=datetime(2026, 8, 8, 15, 58, tzinfo=UTC)) == "08-09 00:03:00"
    )


def test_login_email_calls_never_silently_sleep_on_flood_wait() -> None:
    client = AsyncMock(return_value=SimpleNamespace(email_pattern="t***@example.com", length=6))

    result = asyncio.run(account_ops.send_login_email_code(client, "test@example.com"))

    assert result == {"email_pattern": "t***@example.com", "length": 6}
    assert client.await_args.kwargs["flood_sleep_threshold"] == 0

    client.reset_mock()
    asyncio.run(account_ops.confirm_login_email(client, "123456"))
    assert client.await_args.kwargs["flood_sleep_threshold"] == 0


def test_login_code_alert_matches_supported_777000_wording() -> None:
    assert is_login_code_alert(
        "Login code:97588. Do not give this code to anyone, even if they say they are from Telegram!"
    )
    assert is_login_code_alert(
        "Your login code is 97588. Enter it in the Telegram app where you are trying to log in."
    )
    assert is_login_code_alert("登录验证码：97588。请勿将验证码告诉任何人。")
    assert is_login_code_alert("Код для входа: 97588. Никому не сообщайте этот код.")
    assert is_login_code_alert("Código de inicio de sesión: 97588")
    assert not is_login_code_alert("Your code is: 853353. Use it to verify your email for Login.")
    assert not is_login_code_alert("Login code requested, but the numeric code is absent.")
    assert not is_login_code_alert("Two-Step Verification settings changed on 05/08/2026.")


def test_telegram_email_parser_correlates_sender_recipient_purpose_and_code() -> None:
    parsed = parse_telegram_login_email(SAMPLE_EMAIL, "testime001@mail.example.com")
    assert parsed is not None
    assert parsed.code == "853353"
    assert parsed.recipient == "testime001@mail.example.com"
    assert parsed.sent_at is not None


def test_telegram_email_parser_omits_missing_recipient_headers(monkeypatch) -> None:
    original = login_email_protection.getaddresses

    def strict_getaddresses(values):
        if any(not value for value in values):
            return []
        return original(values)

    monkeypatch.setattr(login_email_protection, "getaddresses", strict_getaddresses)
    parsed = parse_telegram_login_email(SAMPLE_EMAIL, "testime001@mail.example.com")
    assert parsed is not None
    assert parsed.code == "853353"


def test_telegram_email_parser_rejects_wrong_alias_or_sender() -> None:
    assert parse_telegram_login_email(SAMPLE_EMAIL, "another@mail.example.com") is None
    forged = SAMPLE_EMAIL.replace(b"noreply@telegram.org", b"attacker@example.org")
    assert parse_telegram_login_email(forged, "testime001@mail.example.com") is None


def test_telegram_email_parser_rejects_mismatched_subject_code() -> None:
    mismatched = SAMPLE_EMAIL.replace(b"Your Code - 853353", b"Your Code - 111111")
    assert parse_telegram_login_email(mismatched, "testime001@mail.example.com") is None


def test_alias_uses_timestamp_and_phone_digits() -> None:
    assert build_alias("+1 202 555 0147", "mail.example.com", 1775550123) == (
        "1775550123_12025550147@mail.example.com"
    )


def test_domain_normalization_and_validation() -> None:
    assert normalize_domain(" @Mail.Example.COM ") == "mail.example.com"
    for invalid in ("", "https://example.com", "user@example.com", "-bad.example"):
        try:
            normalize_domain(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid domain: {invalid}")


def test_failed_event_retry_button_uses_inline_callback() -> None:
    payload = LoginEmailProtector._retry_markup(42).model_dump(exclude_none=True)
    button = payload["inline_keyboard"][0][0]
    assert button["text"] == "快捷换绑登录邮箱"
    assert button["callback_data"] == "emailguard:retrymenu:42"


def test_gmail_reader_finds_exact_forwarded_alias(monkeypatch) -> None:
    class FakeImap:
        def __init__(self, *args, **kwargs):
            self.logged_in = False

        def login(self, username, password):
            assert username == "owner@gmail.com"
            assert password == "app-password"
            self.logged_in = True
            return "OK", []

        def select(self, folder, readonly=False):
            assert self.logged_in and folder == "INBOX" and readonly is True
            return "OK", []

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"101"]
            assert command == "fetch" and args[0] == b"101"
            return "OK", [(b"101 (BODY[] {1})", SAMPLE_EMAIL), b")"]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr("app.services.login_email_protection.imaplib.IMAP4_SSL", FakeImap)
    monkeypatch.setattr(settings, "login_email_gmail_username", "owner@gmail.com")
    monkeypatch.setattr(settings, "login_email_gmail_app_password", "app-password")
    monkeypatch.setattr(settings, "login_email_poll_timeout_seconds", 1)
    monkeypatch.setattr(settings, "login_email_poll_interval_seconds", 0.01)
    code = GmailCodeReader().wait_for_code_sync(
        "testime001@mail.example.com",
        datetime(2026, 8, 5, 5, 45, tzinfo=UTC),
    )
    assert code == "853353"


def test_gmail_reader_cancellation_stops_polling_thread(monkeypatch) -> None:
    reader = GmailCodeReader()
    captured = {}

    async def blocking_to_thread(function, *args):
        captured["cancel_event"] = args[-1]
        await asyncio.Future()

    monkeypatch.setattr(login_email_protection.asyncio, "to_thread", blocking_to_thread)

    async def exercise() -> None:
        task = asyncio.create_task(
            reader.wait_for_code(
                "alias@mail.example.com",
                datetime.now(UTC),
                300,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert captured["cancel_event"].is_set() is True

    asyncio.run(exercise())


def test_incomplete_protection_events_become_retryable() -> None:
    class Result:
        rowcount = 3

    class Session:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            assert "login_email_protection_events" in str(statement)
            assert "waiting_email" not in str(statement)
            return Result()

        async def commit(self):
            self.committed = True

    session = Session()

    class Sessionmaker:
        def __call__(self):
            return session

    assert asyncio.run(recover_incomplete_events(Sessionmaker())) == 3
    assert session.committed is True


@pytest.mark.parametrize("premium,whitelisted", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("window_hours", [0, 24])
def test_first_alert_uses_account_specific_window(monkeypatch, window_hours: int, premium, whitelisted) -> None:
    detected_at = datetime.now(UTC)

    class Session:
        scalar_calls = 0
        added = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def scalar(self, statement):
            self.scalar_calls += 1
            return None

        async def get(self, model, key):
            if model is login_email_protection.ServiceMessage:
                return SimpleNamespace(received_at=detected_at, text="Telegram Premium" if premium else "")
            if model is login_email_protection.LoginEmailWhitelist and whitelisted:
                return SimpleNamespace(account_id=4)
            if model is login_email_protection.TgAccount:
                return SimpleNamespace(
                    phone_encrypted="encrypted-phone",
                    login_email_window_hours=window_hours,
                )
            return None

        def add(self, record):
            record.id = 91
            self.added = record

        async def flush(self):
            return None

        async def commit(self):
            return None

    session = Session()

    class Sessionmaker:
        def __call__(self):
            return session

    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector.sessionmaker = Sessionmaker()
    protector._account_locks = {}
    protector._notify = AsyncMock()
    monkeypatch.setattr(settings, "login_email_protection_enabled", True)
    monkeypatch.setattr(
        login_email_protection,
        "get_selected_domain",
        AsyncMock(return_value="mail.example.com"),
    )

    notice = asyncio.run(protector._record_alert_locked(4, 73))

    if whitelisted:
        assert notice is None
        assert session.added.status == "whitelisted"
        return
    if premium:
        window_hours = 0

    assert notice is not None
    assert notice.event_id == 91
    assert notice.starts_new_window is True
    assert notice.window_hours == window_hours
    assert notice.alert_count == 1
    assert session.scalar_calls == 2
    assert session.added.status == "waiting_window"
    assert session.added.alert_count == 1
    assert session.added.detected_at == detected_at
    assert session.added.window_ends_at == detected_at + login_email_protection.timedelta(
        hours=window_hours
    )
    protector._notify.assert_not_awaited()


@pytest.mark.parametrize("premium,status", [(False, "waiting_window"), (True, "waiting_window"), (True, "waiting_email"), (True, "requesting")])
def test_alerts_inside_window_are_merged_without_extending_it(monkeypatch, premium, status) -> None:
    first_at = datetime.now(UTC) - login_email_protection.timedelta(hours=3)
    second_at = first_at + login_email_protection.timedelta(hours=2)
    window_ends_at = first_at + login_email_protection.timedelta(hours=8)
    active_window = SimpleNamespace(
        status=status,
        id=91,
        alert_count=1,
        detected_at=first_at,
        last_detected_at=first_at,
        window_ends_at=window_ends_at,
    )

    class Session:
        added = None
        scalar_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def scalar(self, statement):
            self.scalar_calls += 1
            return None if self.scalar_calls == 1 else active_window

        async def get(self, model, key):
            if model is login_email_protection.ServiceMessage:
                return SimpleNamespace(received_at=second_at, text="Telegram Premium" if premium else "")
            if model is login_email_protection.TgAccount:
                return SimpleNamespace(phone_encrypted="encrypted-phone")
            return None

        def add(self, record):
            self.added = record

        async def commit(self):
            return None

    session = Session()

    class Sessionmaker:
        def __call__(self):
            return session

    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector.sessionmaker = Sessionmaker()
    protector._notify = AsyncMock()
    monkeypatch.setattr(settings, "login_email_protection_enabled", True)
    monkeypatch.setattr(
        login_email_protection,
        "get_selected_domain",
        AsyncMock(return_value="mail.example.com"),
    )

    waiter = SimpleNamespace(cancel=lambda: setattr(waiter, "cancelled", True), cancelled=False)
    protector._window_waiters = {91: waiter}
    notice = asyncio.run(protector._record_alert_locked(4, 74))
    expedited = premium and status == "waiting_window"
    assert waiter.cancelled == expedited
    if expedited:
        window_ends_at = second_at

    assert notice is not None
    assert notice.event_id == 91
    assert notice.starts_new_window == expedited
    assert notice.alert_count == 2
    assert notice.window_hours == (0 if expedited else 8)
    assert notice.window_ends_at == window_ends_at
    assert session.added.status == "merged"
    assert session.added.parent_event_id == 91
    assert session.added.window_ends_at == window_ends_at
    assert active_window.alert_count == 2
    assert active_window.last_detected_at == second_at
    protector._notify.assert_not_awaited()


def test_expired_window_immediately_runs_one_change() -> None:
    event = SimpleNamespace(
        id=91,
        account_id=4,
        status="waiting_window",
        window_ends_at=datetime.now(UTC),
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, model, key):
            return event

    class Sessionmaker:
        def __call__(self):
            return Session()

    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector.sessionmaker = Sessionmaker()
    protector._window_waiters = {}
    protector._account_locks = {}
    protector._change_locks = {}
    protector._prepare_window_change = AsyncMock(
        return_value=(4, "alias@mail.example.com", "mail.example.com")
    )

    async def execute_change(*args):
        assert protector._account_locks[4].locked() is False
        assert protector._change_locks[4].locked() is True

    protector._execute_change = AsyncMock(side_effect=execute_change)
    client = object()

    asyncio.run(protector.wait_for_window(91, client))

    protector._prepare_window_change.assert_awaited_once_with(91)
    protector._execute_change.assert_awaited_once_with(
        91,
        4,
        "alias@mail.example.com",
        "mail.example.com",
        client,
    )
    assert protector._window_waiters == {}


def test_protection_failure_reasons_explain_mail_delay_and_flood_wait(monkeypatch) -> None:
    monkeypatch.setattr(settings, "login_email_poll_timeout_seconds", 300)
    monkeypatch.setattr(
        login_email_protection,
        "format_wait_deadline",
        lambda seconds: "20:10:00",
    )

    timeout_reason = LoginEmailProtector._friendly_failure_reason(
        TimeoutError("等待 Telegram 登录邮箱验证码超时")
    )
    flood_reason = LoginEmailProtector._friendly_failure_reason(FloodWaitError(None, 600))

    assert "5 分钟内未收到" in timeout_reason
    assert "邮件接收链路" in timeout_reason
    assert "Cloudflare TempMail Webhook" in timeout_reason
    assert "Gmail 转发/IMAP" in timeout_reason
    assert "Telegram 已受理发码" in timeout_reason
    assert "Webhook 投递记录" in timeout_reason
    assert "未自动重发" in timeout_reason
    assert "Telegram 限制尝试次数" in flood_reason
    assert "20:10:00" in flood_reason
    assert "600 秒" not in flood_reason


def test_resumed_email_wait_does_not_request_another_code(monkeypatch) -> None:
    requested_at = datetime.now(UTC)
    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector._gmail_slots = asyncio.Semaphore(1)
    protector.reader = SimpleNamespace(
        wait_for_code=AsyncMock(side_effect=TimeoutError("late mail"))
    )
    protector._window_summary = AsyncMock(return_value="summary")
    protector._set_event_status = AsyncMock()
    protector._notify = AsyncMock()
    send_code = AsyncMock()
    monkeypatch.setattr(login_email_protection.account_ops, "send_login_email_code", send_code)
    monkeypatch.setattr(settings, "login_email_poll_timeout_seconds", 300)

    asyncio.run(
        protector._execute_change(
            91,
            4,
            "alias@mail.example.com",
            "mail.example.com",
            object(),
            requested_at=requested_at,
            wait_timeout_seconds=240,
        )
    )

    send_code.assert_not_awaited()
    protector.reader.wait_for_code.assert_awaited_once_with(
        "alias@mail.example.com", requested_at, 240
    )
    protector._set_event_status.assert_awaited_once()
    assert "5 分钟内未收到" in protector._notify.await_args.args[0]


def test_email_timeout_sends_one_code_request_and_never_auto_retries(monkeypatch) -> None:
    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector._gmail_slots = asyncio.Semaphore(1)
    protector.reader = SimpleNamespace(
        wait_for_code=AsyncMock(side_effect=TimeoutError("late mail"))
    )
    protector._window_summary = AsyncMock(return_value="summary")
    protector._set_event_status = AsyncMock()
    protector._notify = AsyncMock()
    send_code = AsyncMock(return_value={"length": 6})
    monkeypatch.setattr(login_email_protection.account_ops, "send_login_email_code", send_code)
    monkeypatch.setattr(settings, "login_email_poll_timeout_seconds", 300)

    asyncio.run(
        protector._execute_change(
            91,
            4,
            "alias@mail.example.com",
            "mail.example.com",
            object(),
        )
    )

    send_code.assert_awaited_once()
    protector.reader.wait_for_code.assert_awaited_once()
    assert protector._set_event_status.await_count == 2
    assert "5 分钟内未收到" in protector._notify.await_args.args[0]


def test_resume_waiting_email_uses_remaining_time(monkeypatch) -> None:
    requested_at = datetime.now(UTC) - login_email_protection.timedelta(seconds=60)
    event = SimpleNamespace(
        id=91,
        account_id=4,
        status="waiting_email",
        target_email_encrypted="encrypted-target",
        selected_domain="mail.example.com",
        email_requested_at=requested_at,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, model, key):
            return event

    class Sessionmaker:
        def __call__(self):
            return Session()

    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector.sessionmaker = Sessionmaker()
    protector._change_locks = {}
    protector._execute_change = AsyncMock()
    monkeypatch.setattr(settings, "login_email_poll_timeout_seconds", 300)
    monkeypatch.setattr(
        login_email_protection,
        "decrypt_text",
        lambda value: "alias@mail.example.com",
    )

    asyncio.run(protector.resume_waiting_email(91, object()))

    kwargs = protector._execute_change.await_args.kwargs
    assert kwargs["requested_at"] == requested_at
    assert 230 <= kwargs["wait_timeout_seconds"] <= 240


@pytest.mark.parametrize("text", [
    "Thank you for subscribing to 🌟 Telegram Premium! Learn all about the exclusive benefits you've unlocked: @premium.",
    "Gracias por suscribirte a Telegram Premium", "Votre abonnement Premium a été renouvelé",
    "Dein Telegram Premium-Abonnement läuft ab", "Il tuo abbonamento Premium è attivo",
    "感谢订阅 Telegram Premium！", "会员即将到期，详情：@premium",
    "TELEGRAM PREMIUM", "@PREMIUM",
    "Telegram Premium subscription cancelled", "You received a gift: @premium",
    "Ｔｅｌｅｇｒａｍ Ｐｒｅｍｉｕｍ", "Telegram Pre\u200bmium",
])
def test_premium_related_notices(text):
    assert login_email_protection.is_premium_notice(text)
    assert login_email_protection.is_login_email_protection_alert(text)

@pytest.mark.parametrize("text", ["", "Login code: 12345", "Your login email was changed", "Telegram update", "@premium_support", "premiumize", "您的會員訂閱即將到期", "プレミアム"])
def test_unrelated_notices_are_not_premium(text):
    assert not login_email_protection.is_premium_notice(text)
