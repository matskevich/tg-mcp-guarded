"""Temporary Telethon TL bindings for Telegram managed-bot creation.

Telethon 1.40 does not yet ship the bots.checkUsername/bots.createBot
constructors documented by Telegram. Keep the bindings isolated so they can be
removed once upstream includes them.
"""

from __future__ import annotations

import struct
from typing import Any

from telethon.tl.tlobject import TLObject, TLRequest


class CheckManagedBotUsernameRequest(TLRequest):
    CONSTRUCTOR_ID = 0x87F2219B
    SUBCLASS_OF_ID = 0xF5B399AC

    def __init__(self, username: str):
        self.username = username

    def to_dict(self) -> dict[str, Any]:
        return {"_": type(self).__name__, "username": self.username}

    def _bytes(self) -> bytes:
        return b"".join(
            (
                struct.pack("<I", self.CONSTRUCTOR_ID),
                self.serialize_bytes(self.username),
            )
        )


class CreateManagedBotRequest(TLRequest):
    CONSTRUCTOR_ID = 0xE5B17F2B
    SUBCLASS_OF_ID = 0x2DA17977

    def __init__(
        self,
        *,
        name: str,
        username: str,
        manager_id: Any,
        via_deeplink: bool = False,
    ):
        self.name = name
        self.username = username
        self.manager_id = manager_id
        self.via_deeplink = bool(via_deeplink)

    async def resolve(self, client, utils) -> None:
        self.manager_id = utils.get_input_user(
            await client.get_input_entity(self.manager_id)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "_": type(self).__name__,
            "via_deeplink": self.via_deeplink,
            "name": self.name,
            "username": self.username,
            "manager_id": (
                self.manager_id.to_dict()
                if isinstance(self.manager_id, TLObject)
                else self.manager_id
            ),
        }

    def _bytes(self) -> bytes:
        flags = 1 if self.via_deeplink else 0
        return b"".join(
            (
                struct.pack("<I", self.CONSTRUCTOR_ID),
                struct.pack("<I", flags),
                self.serialize_bytes(self.name),
                self.serialize_bytes(self.username),
                self.manager_id._bytes(),
            )
        )


# Preserve the direct-write guard's module/name classification for custom TL
# bindings. CreateManagedBotRequest must remain ActionMCP-only.
CheckManagedBotUsernameRequest.__module__ = "telethon.tl.functions.bots"
CreateManagedBotRequest.__module__ = "telethon.tl.functions.bots"
