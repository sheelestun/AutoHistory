import asyncio
import json
import time
from datetime import date
from pathlib import Path

from app.reports import render
from tests.test_service import record


async def test_500_records_and_100_operations(env,tmp_path):
    s,ctx,car,_=env
    for _ in range(500):
        await record(s,ctx,car)
    snapshot=await s.report_snapshot(ctx,car['id'])
    assert len(snapshot['records'])==500
    started=time.perf_counter()
    path=tmp_path/'500.pdf'
    await asyncio.to_thread(render,snapshot,path)
    duration=time.perf_counter()-started
    assert duration<15 and path.stat().st_size<10*1024**2
    semaphore=asyncio.Semaphore(5)
    times=[]
    async def operation():
        async with semaphore:
            start=time.perf_counter()
            await s.car_get(ctx,car['id'])
            await s.record_list(ctx,car['id'])
            times.append(time.perf_counter()-start)
    await asyncio.gather(*(operation() for _ in range(100)))
    metrics=dict(pdf_500_seconds=round(duration,3),pdf_bytes=path.stat().st_size,local_service_p95_seconds=round(sorted(times)[94],4),operations=100,concurrency=5)
    (tmp_path/'metrics.json').write_text(json.dumps(metrics),encoding='utf-8')
    print('\nCAPACITY',json.dumps(metrics))
