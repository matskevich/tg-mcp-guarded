# Native audio attachments

Telegram distinguishes a track from a generic document by `DocumentAttributeAudio`, not by its MIME string or caption. The sender delegates attribute extraction to Telethon, which requires the optional `hachoir` dependency. Without it, an otherwise valid MP3/M4A can be delivered as a generic file.

`hachoir==3.4.0` is now an explicit runtime dependency. A sender already running when that dependency is installed must be reconnected, or delivery must use a fresh instance of the existing guarded ActionMCP launcher. Never bypass action policy or send directly through Telethon.

Before delivery, a local `telethon.utils.get_attributes` check should contain `DocumentAttributeAudio` with a duration. MP3 with title and artist tags is the interoperability path when M4A tags are not extracted. The original master remains unchanged.

Independent readback exposes `media_is_audio`, `media_is_voice`, `media_audio_duration`, `media_audio_title` and `media_audio_performer`. These derive from the actual Telegram document attributes. An `audio/*` MIME value alone is insufficient evidence; `media_type=MessageMediaDocument` is also normal for a native audio track.

Acceptance checks cover tracks, voice notes, a MIME-only generic file and absent documents. Owner-device display and placement in a particular client tab remain separate observations.
