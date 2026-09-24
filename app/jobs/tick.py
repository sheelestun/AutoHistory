"""One cron pass. The session advisory lock is held across network calls."""
import asyncio
import logging
import time
from datetime import timedelta

import asyncpg
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError, TelegramServerError, TelegramAPIError

from app.config import Config, WORKS
from app.db import pool
from app.domain import delivery_time, plan_state
from app.service import Service

log = logging.getLogger(__name__)
LOCK_ID = 731102
RETRIES = [1, 5, 15, 60, 180]


async def prepare(service, c, event_id):
    """Reconcile immediately before sending. No transaction survives return."""
    async with c.transaction():
        e = await c.fetchrow('SELECT * FROM notification_outbox WHERE id=$1', event_id)
        if not e:
            return None
        car = await c.fetchrow('SELECT c.*,u.timezone,u.telegram_user_id,u.delivery_enabled FROM cars c JOIN users u ON u.id=c.user_id WHERE c.id=$1 FOR UPDATE OF c', e['car_id'])
        if not car:
            return None
        await service.sync(c, car['id'])
        e = await c.fetchrow('SELECT * FROM notification_outbox WHERE id=$1', event_id)
        now = service.clock()
        if not e or e['status'] != 'pending' or not car['delivery_enabled'] or e['next_attempt_at'] > now:
            return None
        allowed = delivery_time(now, car['timezone'], service.config)
        if allowed > now:
            await c.execute('UPDATE notification_outbox SET next_attempt_at=$2 WHERE id=$1', event_id, allowed)
            return None
        text = f'{car["brand"]} {car["model"]}\n'
        if e['event_type'] == 'odometer':
            text += f'Обновите текущий пробег. Последнее показание: {car["odometer_km"]} км от {car["odometer_as_of"]:%d.%m.%Y}. Без новых данных точность напоминаний по километрам ограничена.'
        else:
            plans = await service.plans(c, car['id'])
            p = next((p for p in plans if p['id'] == e['plan_id']), None)
            if not p or p['cycle_no'] != e['cycle_no']:
                return None
            state = plan_state(p, car['odometer_km'], service.today(car['timezone']), service.config)
            text += ('Пора выполнить ТО' if e['stage'] == 'due' else 'Скоро ТО') + ': ' + WORKS[p['work_code']]
            text += '\nПорог: ' + ', '.join(state['reasons'])
            if state['next_date']:
                text += f'\nСрок: {state["next_date"]:%d.%m.%Y}'
            if state['next_odometer_km'] is not None:
                text += f'\nПробег ТО: {state["next_odometer_km"]} км'
            text += f'\nПоследний пробег: {car["odometer_km"]} км от {car["odometer_as_of"]:%d.%m.%Y}'
        return dict(event=dict(e), car=dict(car), text=text)


async def dispatch(service, c, bot, prepared):
    e, car = prepared['event'], prepared['car']
    now = service.clock()
    try:
        msg = await bot.send_message(car['telegram_user_id'], prepared['text'])
    except TelegramRetryAfter as exc:
        retry_at = now+timedelta(seconds=exc.retry_after)
        async with c.transaction():
            await c.execute("UPDATE notification_outbox SET next_attempt_at=$2,last_error='429' WHERE id=$1 AND status='pending'", e['id'], delivery_time(retry_at, car['timezone'], service.config))
            await c.execute('INSERT INTO delivery_backoff(id,blocked_until) VALUES(true,$1) ON CONFLICT(id) DO UPDATE SET blocked_until=greatest(delivery_backoff.blocked_until,EXCLUDED.blocked_until)', retry_at)
    except TelegramForbiddenError:
        async with c.transaction():
            await c.execute('UPDATE users SET delivery_enabled=false WHERE id=$1', car['user_id'])
            await c.execute("UPDATE notification_outbox SET status='cancelled',last_error='403' WHERE status='pending' AND car_id IN (SELECT id FROM cars WHERE user_id=$1)", car['user_id'])
    except (TelegramNetworkError, TelegramServerError, TimeoutError):
        attempts = e['attempts'] + 1
        if attempts > len(RETRIES):
            await c.execute("UPDATE notification_outbox SET status='failed',attempts=$2,last_error='NETWORK_RETRIES_EXHAUSTED' WHERE id=$1 AND status='pending'", e['id'], attempts)
            log.error('notification_failed event_id=%s', e['id'])
        else:
            when = delivery_time(now+timedelta(minutes=RETRIES[attempts-1]), car['timezone'], service.config)
            await c.execute("UPDATE notification_outbox SET attempts=$2,next_attempt_at=$3,last_error='NETWORK' WHERE id=$1 AND status='pending'", e['id'], attempts, when)
    except TelegramAPIError:
        await c.execute("UPDATE notification_outbox SET status='failed',last_error='TELEGRAM_API' WHERE id=$1 AND status='pending'", e['id'])
        log.error('notification_failed event_id=%s', e['id'])
    else:
        # Even if concurrently cancelled, a delivered event must never reopen.
        await c.execute("UPDATE notification_outbox SET status='sent',sent_at=$2,telegram_message_id=$3,last_error=NULL WHERE id=$1", e['id'], service.clock(), msg.message_id)


async def run_tick(service, bot, lock_connection, sleep=asyncio.sleep):
    c = lock_connection
    if not await c.fetchval('SELECT pg_try_advisory_lock($1)', LOCK_ID):
        return False
    try:
        for row in await c.fetch('SELECT id FROM cars ORDER BY id'):
            async with c.transaction():
                await c.fetchrow('SELECT id FROM cars WHERE id=$1 FOR UPDATE', row['id'])
                await service.sync(c, row['id'])
        rows = list(await c.fetch("SELECT o.id,u.telegram_user_id AS chat FROM notification_outbox o JOIN cars c ON c.id=o.car_id JOIN users u ON u.id=c.user_id WHERE o.status='pending' AND o.next_attempt_at<=$1 ORDER BY o.next_attempt_at,o.id", service.clock()))
        started, global_ready, chat_ready = time.monotonic(), 0.0, {}
        # Leave a bounded pass so the next minute can reconcile newly due events.
        while rows and time.monotonic()-started < 45:
            blocked_until = await c.fetchval('SELECT blocked_until FROM delivery_backoff WHERE id=true')
            if blocked_until and blocked_until > service.clock():
                break
            row = min(rows, key=lambda r: chat_ready.get(r['chat'], 0))
            rows.remove(row)
            delay = max(global_ready, chat_ready.get(row['chat'], 0))-time.monotonic()
            if delay > 0:
                await sleep(delay)
            prepared = await prepare(service, c, row['id'])
            if prepared:
                await dispatch(service, c, bot, prepared)
                sent_at = time.monotonic()
                global_ready, chat_ready[row['chat']] = sent_at + .1, sent_at + 1
        await c.execute("DELETE FROM notification_outbox WHERE retired_at < $1::timestamptz - interval '90 days'", service.clock())
        await c.execute("INSERT INTO job_health(name,last_success_at) VALUES('tick',$1) ON CONFLICT(name) DO UPDATE SET last_success_at=EXCLUDED.last_success_at", service.clock())
        return True
    finally:
        await c.execute('SELECT pg_advisory_unlock($1)', LOCK_ID)


async def main():
    config = Config.load()
    logging.basicConfig(level=config.log_level)
    db = await pool(config)
    connection = await asyncpg.connect(config.database_url)
    try:
        async with Bot(config.bot_token, session=AiohttpSession(timeout=20)) as bot:
            await run_tick(Service(db, config), bot, connection)
    finally:
        await connection.close()
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
