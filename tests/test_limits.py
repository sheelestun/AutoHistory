from datetime import date
from uuid import uuid4
from dataclasses import replace

import pytest

from app.domain import AppError
from tests.test_service import record


async def test_record_limit_is_atomic(env):
    s,ctx,car,_=env
    s.config=replace(s.config,max_records=2)
    await record(s,ctx,car)
    await record(s,ctx,car)
    with pytest.raises(AppError) as exc:
        await record(s,ctx,car,service_date=date(2026,9,24),odometer_km=15000)
    assert exc.value.code=='LIMIT_REACHED'
    assert (await s.car_get(ctx,car['id']))['odometer_km']==10000
    assert len((await s.record_list(ctx,car['id']))['items'])==2


async def test_pilot_user_limit(env):
    s,ctx,car,_=env
    s.config=replace(s.config,max_users=1)
    with pytest.raises(AppError) as exc:
        await s.user_start(123123,'Europe/Moscow')
    assert exc.value.code=='LIMIT_REACHED'
    assert await s.user_get(123123) is None
    assert await s.user_start(111111)


async def test_wrong_key_from_other_owner_is_not_disclosed(env):
    s,ctx,car,_=env
    key=uuid4()
    await record(s,ctx,car,creation_key=key)
    other=await s.user_start(555555,'Europe/Moscow')
    from app.service import AuthContext
    foreign=AuthContext(other['id'])
    foreign_car=await s.car_create(foreign,uuid4(),brand='A',model='B',year=2020,odometer_km=10000)
    with pytest.raises(AppError) as exc:
        await record(s,foreign,foreign_car,creation_key=key)
    assert exc.value.code=='CONFLICT'
    assert not (await s.record_list(foreign,foreign_car['id']))['items']
