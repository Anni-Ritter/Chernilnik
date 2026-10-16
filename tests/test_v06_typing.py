"""Typing indicator must refresh while waiting, then stop on both success and error."""
import asyncio
import pytest
from typing_status import typing_during


class DummyBot:
    def __init__(self):
        self.actions=[]

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id,action))


def test_typing_pulses_and_stops():
    async def go():
        b=DummyBot()
        async with typing_during(b,123,every=0.012):
            await asyncio.sleep(0.055)
        assert len(b.actions)>=2
        assert all(action=='typing' and chat==123 for chat,action in b.actions)
        before=len(b.actions)
        await asyncio.sleep(0.045)
        assert len(b.actions)==before
    asyncio.run(go())


def test_typing_stops_after_error():
    async def go():
        b=DummyBot()
        with pytest.raises(RuntimeError):
            async with typing_during(b,456,every=0.01):
                await asyncio.sleep(0.022)
                raise RuntimeError('mock generation error')
        before=len(b.actions)
        await asyncio.sleep(0.030)
        assert len(b.actions)==before
    asyncio.run(go())
