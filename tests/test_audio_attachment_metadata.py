from types import SimpleNamespace

from telethon.tl.types import DocumentAttributeAudio

from tganalytics.domain.groups import GroupManager


def metadata(attributes, mime='audio/mpeg'):
    return GroupManager._media_metadata(SimpleNamespace(
        file=SimpleNamespace(mime_type=mime, name='sample.mp3', size=123),
        document=SimpleNamespace(attributes=attributes),
    ))


def test_mime_alone_is_not_native_audio():
    result = metadata([])
    assert result['media_mime_type'] == 'audio/mpeg'
    assert result['media_is_audio'] is False
    assert result['media_audio_duration'] is None


def test_track_is_distinct_from_voice_note():
    result = metadata([DocumentAttributeAudio(
        duration=799, voice=False, title='story', performer='narrator',
    )])
    assert result['media_is_audio'] is True
    assert result['media_is_voice'] is False
    assert result['media_audio_duration'] == 799
    assert result['media_audio_title'] == 'story'
    assert result['media_audio_performer'] == 'narrator'


def test_voice_is_preserved_as_voice():
    assert metadata([DocumentAttributeAudio(duration=12, voice=True)])['media_is_voice']


def test_plain_file_and_absent_document_are_safe():
    assert metadata([], mime='application/pdf')['media_is_audio'] is False
    result = GroupManager._media_metadata(SimpleNamespace(file=None))
    assert result['media_is_audio'] is False
    assert result['media_audio_duration'] is None
