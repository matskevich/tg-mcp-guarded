"""Preserve forum consumers across the channels -> messages TL migration."""
from telethon.tl.functions import channels, messages


def request(name, channel, **fields):
    modern = getattr(messages, name, None)
    if modern is not None:
        return modern(peer=channel, **fields)
    return getattr(channels, name)(channel=channel, **fields)


def CreateForumTopicRequest(channel, **fields):
    return request("CreateForumTopicRequest", channel, **fields)


def EditForumTopicRequest(channel, **fields):
    return request("EditForumTopicRequest", channel, **fields)


def GetForumTopicsRequest(channel, **fields):
    return request("GetForumTopicsRequest", channel, **fields)


def GetForumTopicsByIDRequest(channel, **fields):
    return request("GetForumTopicsByIDRequest", channel, **fields)


def UpdatePinnedForumTopicRequest(channel, **fields):
    return request("UpdatePinnedForumTopicRequest", channel, **fields)
