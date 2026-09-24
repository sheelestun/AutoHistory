import asyncio
from unittest.mock import AsyncMock

import pytest

from app.reports import Reports
from app.domain import AppError
from tests.test_service import record


async def test_pdf_failure_cleans_file_and_unlocks(env):
    s,ctx,car,_=env
    await record(s,ctx,car)
    reports=Reports(s)
    with pytest.raises(RuntimeError):
        await reports.send(ctx,car['id'],AsyncMock(side_effect=RuntimeError('network')),AsyncMock())
    assert not reports.lock.locked()
    assert not list(reports.directory.glob('*.pdf'))


async def test_pdf_busy_does_not_start_second_report(env):
    s,ctx,car,_=env
    await record(s,ctx,car)
    reports=Reports(s)
    started,finish=asyncio.Event(),asyncio.Event()
    async def send(*args):
        started.set()
        await finish.wait()
    task=asyncio.create_task(reports.send(ctx,car['id'],send,AsyncMock()))
    await started.wait()
    with pytest.raises(AppError) as exc:
        await reports.send(ctx,car['id'],AsyncMock(),AsyncMock())
    assert exc.value.code=='BUSY'
    finish.set()
    await task
