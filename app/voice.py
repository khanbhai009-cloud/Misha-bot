"""Voice-note replies — PLANNED differentiator, currently OFF.

None of the three researched bots (Baka, Nezuko, Riya) ever sends voice
notes, so a bot that sometimes *speaks* its reply stands out. The wiring
already exists: handlers/chat.py calls maybe_send_voice_reply() before
sending text, behind the VOICE_REPLIES env flag (default "off").

TODO when enabling:
  1. Send the reply text to a TTS provider (none wired yet — pick one
     when enabling), get audio.
  2. Convert to OGG/Opus (Telegram voice-note format).
  3. bot.send_voice(chat_id, file, reply_parameters=...) and return True.
"""
from __future__ import annotations

import logging

from .config import get_settings

log = logging.getLogger("bot.voice")


async def maybe_send_voice_reply(bot, chat_id: int, text: str, reply_to_message_id=None) -> bool:
    """Try to deliver `text` as a voice note. Returns True if sent.

    v1: always returns False (feature off / not implemented yet), so the
    caller falls through to a normal text reply.
    """
    if get_settings().voice_replies != "on":
        return False
    # PLANNED: TTS -> ogg/opus -> bot.send_voice(...)
    log.info("VOICE_REPLIES=on but voice pipeline is still a stub — sending text instead")
    return False
