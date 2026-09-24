import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from app.service import Service
from app.jobs.replay_deletions import replay
from tests.test_service import record,plan


async def command(*args):
    process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    _,error=await process.communicate()
    assert process.returncode==0,error.decode(errors='replace')


async def test_real_dump_restore_and_deletion_replay(env,tmp_path):
    binary=os.getenv('TEST_PG_BIN')
    if not binary:
        pytest.skip('Set TEST_PG_BIN for real pg_dump/pg_restore test')
    s,ctx,car,_=env
    await record(s,ctx,car)
    await plan(s,ctx,car)
    async with s.pool.acquire() as c:
        schema=await c.fetchval('SELECT current_schema()')
    suffix='.exe' if os.name=='nt' else ''
    dump=tmp_path/'test.dump'
    await command(str(Path(binary)/('pg_dump'+suffix)),s.config.database_url,'-Fc','-n',schema,'-f',str(dump))
    await s.user_delete(ctx,True)  # Independently preserved journal survives an older dump.
    dbname='autohistory_restore_test_'+uuid4().hex
    url=urlsplit(s.config.database_url)
    restored_dsn=urlunsplit((url.scheme,url.netloc,'/'+dbname,url.query,''))
    admin=await asyncpg.connect(s.config.database_url)
    await admin.execute(f'CREATE DATABASE {dbname}')
    restored=None
    try:
        await command(str(Path(binary)/('pg_restore'+suffix)),'-d',restored_dsn,'--no-owner','--exit-on-error',str(dump))
        restored=await asyncpg.create_pool(restored_dsn,min_size=1,max_size=2,server_settings={'search_path':schema})
        service=Service(restored,s.config,s.clock)
        assert len((await service.record_list(ctx,car['id']))['items'])==1
        await replay(service,s.config.deletion_journal_dir)
        assert await service.user_get(111111) is None
    finally:
        if restored:
            await restored.close()
        await admin.execute(f'DROP DATABASE {dbname}')
        await admin.close()
