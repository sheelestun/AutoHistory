import os
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.config import Config
from app.db import migrate
from app.service import Service, AuthContext


@pytest_asyncio.fixture
async def env(tmp_path):
    dsn = os.getenv('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set TEST_DATABASE_URL to run PostgreSQL integration tests')
    schema = 'test_' + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA {schema}')
    db = await asyncpg.create_pool(dsn, min_size=1,max_size=5,server_settings={'search_path':schema})
    async with db.acquire() as c:
        await migrate(c)
        await migrate(c)
    now = [datetime(2026,9,24,5,tzinfo=timezone.utc)]
    config = Config(database_url=dsn,pdf_tmp_dir=str(tmp_path/'pdf'),deletion_journal_dir=str(tmp_path/'deletions'))
    service = Service(db,config,lambda:now[0])
    user = await service.user_start(111111,'Asia/Novosibirsk')
    ctx = AuthContext(user['id'])
    car = await service.car_create(ctx,str(uuid4()),brand='Лада',model='Веста',year=2020,odometer_km=10000)
    yield service,ctx,car,now
    await db.close()
    await admin.execute(f'DROP SCHEMA {schema} CASCADE')
    await admin.close()
