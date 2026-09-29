import struct

from telethon.tl.types import InputUser

from tganalytics.infra.managed_bot import (
    CheckManagedBotUsernameRequest,
    CreateManagedBotRequest,
)


def test_check_managed_bot_username_request_encoding():
    request = CheckManagedBotUsernameRequest("example_bot")

    assert bytes(request).startswith(struct.pack("<I", 0x87F2219B))
    assert request.to_dict()["username"] == "example_bot"


def test_create_managed_bot_request_encoding_and_guard_classification():
    request = CreateManagedBotRequest(
        name="Mira",
        username="example_bot",
        manager_id=InputUser(user_id=42, access_hash=84),
        via_deeplink=True,
    )

    payload = bytes(request)
    assert payload.startswith(struct.pack("<II", 0xE5B17F2B, 1))
    assert request.__class__.__module__ == "telethon.tl.functions.bots"
    assert request.to_dict()["via_deeplink"] is True


def test_managed_bot_direct_creation_does_not_claim_a_deeplink():
    request = CreateManagedBotRequest(
        name="example",
        username="example_bot",
        manager_id=InputUser(user_id=42, access_hash=84),
    )
    assert bytes(request).startswith(struct.pack("<II", 0xE5B17F2B, 0))
