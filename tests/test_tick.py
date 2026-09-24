from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from aiogram.methods import SendMessage
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError

from app.jobs.tick import run_tick, dispatch, prepare, LOCK_ID
from tests.test_service import plan


class FakeBot:
    def __init__(self,error=None):
        self.sent=[]
        self.error=error

    async def send_message(self,chat,text):
        if self.error:
            raise self.error
        self.sent.append((chat,text))
        return SimpleNamespace(message_id=42)


async def no_sleep(_):
    pass


async def test_tick_dedup_and_lock(env):
    s,ctx,car,_=env
    await plan(s,ctx,car,interval_months=None)
    bot=FakeBot()
    async with s.pool.acquire() as c, s.pool.acquire() as other:
        await other.execute('SELECT pg_advisory_lock($1)',LOCK_ID)
        assert await run_tick(s,bot,c,no_sleep) is False
        await other.execute('SELECT pg_advisory_unlock($1)',LOCK_ID)
        await run_tick(s,bot,c,no_sleep)
        await run_tick(s,bot,c,no_sleep)
    assert len(bot.sent)==1


async def test_night_and_local_boundary(env):
    s,ctx,car,now=env
    now[0]=now[0].replace(hour=13)  # 20:00 local
    await plan(s,ctx,car,interval_months=None)
    bot=FakeBot()
    async with s.pool.acquire() as c:
        await run_tick(s,bot,c,no_sleep)
        assert not bot.sent
        now[0]=(now[0]+timedelta(days=1)).replace(hour=2)
        await run_tick(s,bot,c,no_sleep)
        assert len(bot.sent)==1


async def test_retry_429_and_403_restart(env):
    s,ctx,car,now=env
    await plan(s,ctx,car,interval_months=None)
    method=SendMessage(chat_id=1,text='test')
    async with s.pool.acquire() as c:
        eid=await c.fetchval("SELECT id FROM notification_outbox WHERE status='pending'")
        ready=await prepare(s,c,eid)
        await dispatch(s,c,FakeBot(TelegramRetryAfter(method=method,message='limit',retry_after=30)),ready)
        assert await c.fetchval('SELECT next_attempt_at FROM notification_outbox WHERE id=$1',eid)==now[0]+timedelta(seconds=30)
        now[0]+=timedelta(seconds=31)
        ready=await prepare(s,c,eid)
        await dispatch(s,c,FakeBot(TelegramForbiddenError(method=method,message='blocked')),ready)
        assert not await c.fetchval('SELECT delivery_enabled FROM users WHERE id=$1',ctx.user_id)
        assert await c.fetchval('SELECT status FROM notification_outbox WHERE id=$1',eid)=='cancelled'
    await s.user_start(111111)
    async with s.pool.acquire() as c:
        assert await c.fetchval('SELECT status FROM notification_outbox WHERE id=$1',eid)=='pending'


async def test_retry_exhaustion(env):
    s,ctx,car,now=env
    await plan(s,ctx,car,interval_months=None)
    bot=FakeBot(TelegramNetworkError(method=SendMessage(chat_id=1,text='test'),message='timeout'))
    async with s.pool.acquire() as c:
        eid=await c.fetchval("SELECT id FROM notification_outbox WHERE status='pending'")
        for _ in range(6):
            ready=await prepare(s,c,eid)
            assert ready
            await dispatch(s,c,bot,ready)
            now[0]=await c.fetchval('SELECT next_attempt_at FROM notification_outbox WHERE id=$1',eid)
        assert await c.fetchval('SELECT status FROM notification_outbox WHERE id=$1',eid)=='failed'
