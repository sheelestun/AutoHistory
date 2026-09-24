import asyncio
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.domain import AppError
from app.service import Service, AuthContext


async def record(s,ctx,car,**kwargs):
    fields=dict(service_date=date(2026,9,20),odometer_km=10000,work_code='ENGINE_OIL',cost_rub='4500.50')
    fields.update(kwargs)
    return await s.record_create(ctx,car['id'],fields.pop('creation_key',uuid4()),**fields)


async def plan(s,ctx,car,**kwargs):
    fields=dict(work_code='ENGINE_OIL',interval_km=10000,interval_months=6,initial_date=date(2026,1,31),initial_odometer_km=0)
    fields.update(kwargs)
    return await s.plan_upsert(ctx,car['id'],**fields)


async def test_owner_isolation(env):
    s,ctx,car,_=env
    other=await s.user_start(222222,'Europe/Moscow')
    foreign=AuthContext(other['id'])
    r=await record(s,ctx,car)
    for coro in [s.car_get(foreign,car['id']),s.record_get(foreign,r['id']),s.record_list(foreign,car['id']),s.report_snapshot(foreign,car['id']),s.car_set_odometer(foreign,car['id'],12000),plan(s,foreign,car)]:
        with pytest.raises(AppError,match='недоступны'):
            await coro
    await s.record_delete(foreign,r['id'],True)
    assert (await s.record_get(ctx,r['id']))['id']==r['id']
    await s.car_delete(foreign,car['id'],True)
    assert await s.car_get(ctx,car['id'])


async def test_double_save_concurrent_and_conflict(env):
    s,ctx,car,_=env
    key=uuid4()
    a,b=await asyncio.gather(record(s,ctx,car,creation_key=key),record(s,ctx,car,creation_key=key))
    assert a['id']==b['id']
    assert len((await s.record_list(ctx,car['id']))['items'])==1
    with pytest.raises(AppError) as exc:
        await record(s,ctx,car,creation_key=key,cost_rub='1')
    assert exc.value.code=='CONFLICT'
    restart=Service(s.pool,s.config,s.clock)
    assert len((await restart.record_list(ctx,car['id']))['items'])==1


async def test_car_idempotence_and_limit(env):
    s,ctx,car,_=env
    key=uuid4()
    fields=dict(brand='Toyota',model='Camry',year=2021,odometer_km=0)
    a,b=await asyncio.gather(s.car_create(ctx,key,**fields),s.car_create(ctx,key,**fields))
    assert a['id']==b['id']
    await s.car_create(ctx,uuid4(),**fields)
    with pytest.raises(AppError) as exc:
        await s.car_create(ctx,uuid4(),**fields)
    assert exc.value.code=='LIMIT_REACHED'


async def test_chronology_and_correction(env):
    s,ctx,car,_=env
    await record(s,ctx,car,odometer_km=9000)
    with pytest.raises(AppError):
        await record(s,ctx,car,service_date=date(2026,9,19),odometer_km=9500)
    with pytest.raises(AppError):
        await record(s,ctx,car,service_date=date(2026,9,25))
    with pytest.raises(AppError):
        await record(s,ctx,car,odometer_km=11000)
    await record(s,ctx,car,service_date=date(2026,9,24),odometer_km=11000)
    assert (await s.car_get(ctx,car['id']))['odometer_km']==11000
    with pytest.raises(AppError):
        await s.car_set_odometer(ctx,car['id'],10999,True,True)
    await s.car_set_odometer(ctx,car['id'],12000)
    await s.car_set_odometer(ctx,car['id'],11000,True,True)


async def test_rebase_history_delete_and_noop_plan(env):
    s,ctx,car,_=env
    p=await plan(s,ctx,car)
    same=await plan(s,ctx,car)
    assert same['cycle_no']==p['cycle_no']
    r=await record(s,ctx,car)
    p2=(await s.plan_list(ctx,car['id']))[0]
    assert p2['next_date']==date(2027,3,20)
    assert p2['next_odometer_km']==20000
    assert p2['cycle_no']==p['cycle_no']+1
    await record(s,ctx,car,service_date=date(2026,2,1),odometer_km=100)
    assert (await s.plan_list(ctx,car['id']))[0]['cycle_no']==p2['cycle_no']
    await s.record_delete(ctx,r['id'],True)
    p3=(await s.plan_list(ctx,car['id']))[0]
    assert p3['next_date']==date(2026,8,1)
    assert p3['cycle_no']==p2['cycle_no']+1
    assert list(__import__('pathlib').Path(s.config.deletion_journal_dir).glob('*.jsonl'))


async def test_due_supersedes_soon_and_correction_reopens(env):
    s,ctx,car,_=env
    await plan(s,ctx,car,interval_months=None,initial_date=date(2026,9,1),initial_odometer_km=500)
    async with s.pool.acquire() as c:
        assert await c.fetchval("SELECT stage FROM notification_outbox WHERE status='pending'")=='soon'
    await s.car_set_odometer(ctx,car['id'],10500)
    async with s.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='pending' AND stage='due'")==1
        assert await c.fetchval("SELECT status FROM notification_outbox WHERE stage='soon'")=='cancelled'
    await s.car_set_odometer(ctx,car['id'],9000,True,True)
    async with s.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='pending'")==0
    await s.car_set_odometer(ctx,car['id'],10500)
    async with s.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='pending'")==1
        await c.execute("UPDATE notification_outbox SET status='sent' WHERE stage='due'")
    await s.car_set_odometer(ctx,car['id'],9000,True,True)
    await s.car_set_odometer(ctx,car['id'],10500)
    async with s.pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM notification_outbox WHERE stage='due'")=='sent'


async def test_refresh_only_current_period(env):
    s,ctx,car,now=env
    now[0]+=timedelta(days=31)
    async with s.pool.acquire() as c, c.transaction():
        await s.sync(c,car['id'])
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='pending'")==1
    now[0]+=timedelta(days=90)
    async with s.pool.acquire() as c, c.transaction():
        await s.sync(c,car['id'])
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='pending'")==1
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='cancelled'")==1
    await s.car_set_odometer(ctx,car['id'],10000)
    async with s.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='pending'")==0


async def test_empty_report_and_cascade(env):
    s,ctx,car,_=env
    with pytest.raises(AppError) as exc:
        await s.report_snapshot(ctx,car['id'])
    assert exc.value.code=='EMPTY_HISTORY'
    await record(s,ctx,car)
    await plan(s,ctx,car)
    with pytest.raises(AppError):
        await s.user_delete(ctx)
    await s.user_delete(ctx,True)
    async with s.pool.acquire() as c:
        for table in ['users','cars','maintenance_records','maintenance_plans','notification_outbox']:
            assert await c.fetchval(f'SELECT count(*) FROM {table}')==0


async def test_history_filter_pagination(env):
    s,ctx,car,_=env
    for n in range(7):
        await record(s,ctx,car,service_date=date(2023+n//4,1,1),odometer_km=n)
    assert len((await s.record_list(ctx,car['id'],period='all'))['items'])==5
    assert (await s.record_list(ctx,car['id'],page=2))['has_next'] is False
    assert not (await s.record_list(ctx,car['id'],period='2y'))['items']


async def test_error_contract(env):
    s,ctx,car,_=env
    result=await s.call('car.set_odometer',ctx,car_id=car['id'],odometer_km=-1)
    assert not result['ok'] and result['error']['field']=='odometer_km'
    assert result['request_id']==str(ctx.request_id)
