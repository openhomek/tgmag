from __future__ import annotations

from sqlalchemy import select
from telethon import helpers, types
from telethon.errors import (
    ChannelsTooMuchError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    ForwardMessagesRequest,
    GetMessagesViewsRequest,
    ImportChatInviteRequest,
    SendReactionRequest,
)

from app.db.models import TgAccount
from app.services.targets import canonicalize_target_ref, telegram_invite_hash
from app.tg.client_pool import ClientPool


def normalize_target(target: str) -> str | int:
    stripped = target.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


async def get_peer(pool: ClientPool, account_id: int, target: str):
    client = await pool.get_client(account_id)
    return client, await resolve_target_entity(pool, account_id, target)


async def resolve_target_entity(pool: ClientPool, account_id: int, target: str):
    client = await pool.get_client(account_id)
    normalized = normalize_target(target)
    try:
        return await client.get_input_entity(normalized)
    except ValueError:
        if not isinstance(normalized, int) or normalized < 0:
            raise
        async with pool.sessionmaker() as session:
            account = await session.scalar(select(TgAccount).where(TgAccount.user_id == normalized))
        if account and account.username:
            return await client.get_input_entity(f"@{account.username}")
        raise


async def send_message(pool: ClientPool, account_id: int, target: str, text: str) -> dict[str, int]:
    client = await pool.get_client(account_id)
    entity = await resolve_target_entity(pool, account_id, target)
    msg = await client.send_message(entity, text)
    return {"message_id": msg.id}


async def subscribe(pool: ClientPool, account_id: int, target: str) -> dict[str, str | int]:
    invite_hash = telegram_invite_hash(target)
    if invite_hash:
        client = await pool.get_client(account_id)
        normalized = canonicalize_target_ref(target)
        try:
            result = await client(ImportChatInviteRequest(invite_hash))
        except UserAlreadyParticipantError:
            return {"joined": normalized, "status": "already_member"}
        except InviteRequestSentError:
            return {"joined": normalized, "status": "request_sent"}
        except InviteHashExpiredError as exc:
            raise ValueError("群组邀请链接已过期，请更新授权目标中的链接") from exc
        except InviteHashInvalidError as exc:
            raise ValueError("群组邀请链接无效，请检查链接是否完整") from exc
        except ChannelsTooMuchError as exc:
            raise ValueError("该账号加入的频道或群组数量已达 Telegram 上限") from exc
        payload: dict[str, str | int] = {"joined": normalized, "status": "joined"}
        chats = getattr(result, "chats", None) or []
        if chats and getattr(chats[0], "id", None) is not None:
            payload["chat_id"] = int(chats[0].id)
        return payload
    try:
        client, peer = await get_peer(pool, account_id, target)
    except ValueError as exc:
        if target.strip().lstrip("-").isdigit():
            raise ValueError(
                "未加入的私有群无法仅凭 ID 加入，请改用已授权的 t.me/+ 邀请链接"
            ) from exc
        raise
    await client(JoinChannelRequest(peer))
    return {"joined": canonicalize_target_ref(target), "status": "joined"}


async def react(pool: ClientPool, account_id: int, target: str, message_id: int, emoji: str) -> dict[str, str | int]:
    client, peer = await get_peer(pool, account_id, target)
    await client(
        SendReactionRequest(
            peer=peer,
            msg_id=message_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
        )
    )
    return {"message_id": message_id, "reaction": emoji}


async def unreact(pool: ClientPool, account_id: int, target: str, message_id: int) -> dict[str, int]:
    client, peer = await get_peer(pool, account_id, target)
    await client(SendReactionRequest(peer=peer, msg_id=message_id, reaction=[]))
    return {"message_id": message_id}


async def view_post(pool: ClientPool, account_id: int, target: str, message_id: int) -> dict[str, int]:
    client, peer = await get_peer(pool, account_id, target)
    result = await client(GetMessagesViewsRequest(peer=peer, id=[message_id], increment=True))
    views = (result.views[0].views if result.views else 0) or 0
    return {"message_id": message_id, "views": views}


async def forward(
    pool: ClientPool,
    account_id: int,
    source: str,
    message_id: int,
    target: str,
) -> dict[str, int]:
    client = await pool.get_client(account_id)
    from_peer = await resolve_target_entity(pool, account_id, source)
    to_peer = await resolve_target_entity(pool, account_id, target)
    result = await client(
        ForwardMessagesRequest(
            from_peer=from_peer,
            id=[message_id],
            to_peer=to_peer,
            random_id=[helpers.generate_random_long()],
        )
    )
    updates = getattr(result, "updates", [])
    sent_id = next((getattr(u.message, "id", None) for u in updates if hasattr(u, "message")), None)
    return {"message_id": sent_id or 0}
