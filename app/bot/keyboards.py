from __future__ import annotations

from aiogram.types import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.config import settings
from app.db.models import LoginEmailProtectionEvent, TgAccount


def main_menu() -> ReplyKeyboardMarkup:
    """Return the single client-retained, user-collapsible reply keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="系统状态"), KeyboardButton(text="账号管理")],
            [
                KeyboardButton(text="登录账号"),
                KeyboardButton(text="扫码登录"),
            ],
            [KeyboardButton(text="安全防护")],
        ],
        resize_keyboard=True,
        is_persistent=False,
        one_time_keyboard=False,
        input_field_placeholder="选择常用操作",
    )


def cancel_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="取消当前操作", callback_data="flow:cancel")]]
    )


def login_phone_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="改用二维码登录", callback_data="login:qr")],
            [InlineKeyboardButton(text="取消当前操作", callback_data="flow:cancel")],
        ]
    )


def force_reply(placeholder: str) -> ForceReply:
    return ForceReply(
        force_reply=True,
        input_field_placeholder=placeholder,
        selective=True,
    )


def mini_app_panel() -> InlineKeyboardMarkup | None:
    mini_app_url = settings.mini_app_public_url.strip()
    if not mini_app_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="打开", web_app=WebAppInfo(url=mini_app_url))],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def bot_menu_button() -> MenuButtonCommands | MenuButtonWebApp:
    """Use Telegram's native Web App launcher whenever the Mini App is configured."""
    mini_app_url = settings.mini_app_public_url.strip()
    if not settings.mini_app_enabled or not mini_app_url:
        return MenuButtonCommands()
    return MenuButtonWebApp(
        text="打开",
        web_app=WebAppInfo(url=mini_app_url),
    )


def account_button_label(account: TgAccount) -> str:
    if account.user_id:
        identity = str(account.user_id)
    elif account.username:
        identity = f"@{account.username}"
    else:
        identity = account.phone_masked
    return f"#{account.id} · {identity}"


def accounts_panel(accounts: list[TgAccount]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for account in accounts[:20]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=account_button_label(account), callback_data=f"acct:{account.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="刷新", callback_data="nav:accounts"),
            InlineKeyboardButton(text="返回", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_panel(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="重连", callback_data=f"acct_action:reconnect:{account_id}"
                ),
                InlineKeyboardButton(
                    text="SpamBot", callback_data=f"acct_action:spam:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="详细信息", callback_data=f"acct_action:detail:{account_id}"
                ),
                InlineKeyboardButton(
                    text="刷新检测", callback_data=f"acct_action:check_detail:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="拉取777000", callback_data=f"acct_action:service:{account_id}"
                ),
                InlineKeyboardButton(text="2FA", callback_data=f"acct_action:twofa:{account_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="隐私快照", callback_data=f"acct_action:privacy:{account_id}"
                ),
                InlineKeyboardButton(
                    text="资料设置", callback_data=f"acct_panel:profile:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="隐私设置", callback_data=f"acct_panel:privacy:{account_id}"
                ),
                InlineKeyboardButton(
                    text="2FA 设置", callback_data=f"acct_panel:twofa:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="登录邮箱保护",
                    callback_data=f"emailguard:account:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="导出 Session", callback_data=f"acct_action:export_session:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="返回列表", callback_data="nav:accounts"),
            ],
        ]
    )


def post_login_security_panel(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="打开 2FA 设置",
                    callback_data=f"acct_panel:twofa:{account_id}",
                ),
                InlineKeyboardButton(
                    text="返回账号",
                    callback_data=f"acct:{account_id}",
                ),
            ]
        ]
    )


def profile_edit_panel(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="改姓名", callback_data=f"acct_edit:name:{account_id}"),
                InlineKeyboardButton(text="改简介", callback_data=f"acct_edit:bio:{account_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="改用户名", callback_data=f"acct_edit:username:{account_id}"
                ),
                InlineKeyboardButton(
                    text="头像设置", callback_data=f"acct_panel:avatar:{account_id}"
                ),
            ],
            [InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}")],
        ]
    )


def avatar_panel(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="上传图片", callback_data=f"acct_edit:avatar_upload:{account_id}"
                ),
                InlineKeyboardButton(
                    text="随机头像", callback_data=f"acct_action:avatar_random:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="返回资料", callback_data=f"acct_panel:profile:{account_id}"
                )
            ],
        ]
    )


def privacy_keys_panel(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="手机号", callback_data=f"privacy_key:phone:{account_id}"
                ),
                InlineKeyboardButton(
                    text="在线时间", callback_data=f"privacy_key:last_seen:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="头像", callback_data=f"privacy_key:profile_photo:{account_id}"
                ),
                InlineKeyboardButton(
                    text="转发来源", callback_data=f"privacy_key:forwards:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="通话", callback_data=f"privacy_key:calls:{account_id}"),
                InlineKeyboardButton(text="拉群", callback_data=f"privacy_key:groups:{account_id}"),
            ],
            [InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}")],
        ]
    )


def privacy_rules_panel(account_id: int, key_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="所有人", callback_data=f"privacy_set:{key_name}:everybody:{account_id}"
                ),
                InlineKeyboardButton(
                    text="联系人", callback_data=f"privacy_set:{key_name}:contacts:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="没有人", callback_data=f"privacy_set:{key_name}:nobody:{account_id}"
                ),
                InlineKeyboardButton(text="返回", callback_data=f"acct_panel:privacy:{account_id}"),
            ],
        ]
    )


def twofa_panel(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="查询状态", callback_data=f"acct_action:twofa:{account_id}"
                ),
                InlineKeyboardButton(text="设置 2FA", callback_data=f"twofa_edit:set:{account_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="修改 2FA", callback_data=f"twofa_edit:change:{account_id}"
                ),
                InlineKeyboardButton(
                    text="配置邮箱", callback_data=f"twofa_edit:email:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="关闭 2FA", callback_data=f"twofa_edit:disable:{account_id}"
                ),
                InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}"),
            ],
        ]
    )


def batch_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="发送模板", callback_data="template:send"),
                InlineKeyboardButton(text="关注模板", callback_data="template:subscribe"),
            ],
            [
                InlineKeyboardButton(text="反应模板", callback_data="template:react"),
                InlineKeyboardButton(text="浏览模板", callback_data="template:view_post"),
            ],
            [
                InlineKeyboardButton(text="转发模板", callback_data="template:forward"),
                InlineKeyboardButton(text="导出 Session", callback_data="flow:export_session"),
            ],
            [InlineKeyboardButton(text="批量导入 Session", callback_data="flow:import_sessions")],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def settings_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="查看白名单", callback_data="settings:targets"),
                InlineKeyboardButton(text="查看速率", callback_data="settings:rate"),
            ],
            [
                InlineKeyboardButton(text="添加白名单模板", callback_data="template:target_add"),
                InlineKeyboardButton(text="设置速率模板", callback_data="template:rate_set"),
            ],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def monitor_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="开启监听", callback_data="monitor:on"),
                InlineKeyboardButton(text="关闭监听", callback_data="monitor:off"),
            ],
            [
                InlineKeyboardButton(text="通知测试", callback_data="monitor:notify"),
                InlineKeyboardButton(text="系统状态", callback_data="nav:status"),
            ],
            [InlineKeyboardButton(text="安全防护", callback_data="nav:security")],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def login_email_guard_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="邮箱域名管理", callback_data="emailguard:domains"),
                InlineKeyboardButton(text="账号白名单", callback_data="emailguard:whitelist"),
            ],
            [
                InlineKeyboardButton(text="最近保护事件", callback_data="emailguard:events"),
                InlineKeyboardButton(text="检查 Gmail", callback_data="emailguard:testimap"),
            ],
            [
                InlineKeyboardButton(
                    text="检测整个安全防护链路", callback_data="emailguard:checkall"
                )
            ],
            [InlineKeyboardButton(text="刷新", callback_data="emailguard:open")],
            [InlineKeyboardButton(text="返回主菜单", callback_data="nav:home")],
        ]
    )


def login_email_domains_panel(
    domains: tuple[str, ...],
    selected_domain: str | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, domain in enumerate(domains):
        marker = "✅" if domain == selected_domain else "○"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} @{domain}",
                    callback_data=f"emailguard:domain:{index}",
                ),
                InlineKeyboardButton(
                    text="删除",
                    callback_data=f"emailguard:deleteask:{index}",
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="添加邮箱域名", callback_data="emailguard:add")])
    rows.append([InlineKeyboardButton(text="返回安全中心", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def login_email_whitelist_panel(
    accounts: list[TgAccount],
    whitelisted_ids: set[int],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    account_buttons: list[InlineKeyboardButton] = []
    for account in accounts[:30]:
        marker = "✅ 移出" if account.id in whitelisted_ids else "＋ 加入"
        account_buttons.append(
            InlineKeyboardButton(
                text=f"{marker} · #{account.id} {account.phone_masked}",
                callback_data=f"emailguard:white:{account.id}",
            )
        )
    rows.extend([[button] for button in account_buttons])
    rows.append([InlineKeyboardButton(text="返回安全中心", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def login_email_delete_confirm_panel(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="确认删除",
                    callback_data=f"emailguard:delete:{index}",
                ),
                InlineKeyboardButton(text="取消", callback_data="emailguard:domains"),
            ]
        ]
    )


def login_email_events_panel(
    events: list[LoginEmailProtectionEvent],
) -> InlineKeyboardMarkup:
    labels = {
        "succeeded": "✅ 成功",
        "failed": "❌ 失败",
        "whitelisted": "🟦 白名单",
        "disabled": "⚪ 未启用",
        "cooldown": "⏳ 冷却",
        "waiting_window": "🕗 聚合中",
        "requesting": "🔄 请求中",
        "waiting_email": "📨 等待邮件",
        "interrupted": "⚠️ 已中断",
    }
    rows = [
        [
            InlineKeyboardButton(
                text=f"{labels.get(event.status, event.status)} · #{event.account_id} · 事件{event.id}",
                callback_data=f"emailguard:event:{event.id}",
            )
        ]
        for event in events
    ]
    rows.append([InlineKeyboardButton(text="返回安全中心", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def login_email_account_panel(account_id: int, whitelisted: bool) -> InlineKeyboardMarkup:
    label = "移出白名单" if whitelisted else "加入白名单"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="设置等待时间",
                    callback_data=f"emailguard:window:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"emailguard:accounttoggle:{account_id}",
                )
            ],
            [InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}")],
        ]
    )


def login_email_retry_panel(
    event_id: int,
    domains: tuple[str, ...],
    failed_domain: str | None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'↻' if domain == failed_domain else '→'} 使用 @{domain}",
                callback_data=f"emailguard:retry:{event_id}:{index}",
            )
        ]
        for index, domain in enumerate(domains)
    ]
    rows.append([InlineKeyboardButton(text="返回保护设置", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
