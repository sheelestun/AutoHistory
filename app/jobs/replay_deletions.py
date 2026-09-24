"""Apply a separately preserved deletion journal BEFORE using a restored database."""
import asyncio
import json
from pathlib import Path

from app.config import Config
from app.db import pool
from app.domain import uid
from app.service import Service


async def replay(service, directory):
    events=[]
    for path in sorted(Path(directory).glob('*.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            events.append(json.loads(line))
    async with service.pool.acquire() as c, c.transaction():
        affected=set()
        forced={}
        for event in events:
            identifier=uid(event['id'])
            kind=event['kind']
            if kind=='record':
                cid=await c.fetchval('SELECT car_id FROM maintenance_records WHERE id=$1',identifier)
                if cid:
                    affected.add(cid)
                    forced.setdefault(cid,set()).update(r['id'] for r in await c.fetch('SELECT id FROM maintenance_plans WHERE last_record_id=$1',identifier))
                await c.execute('DELETE FROM maintenance_records WHERE id=$1',identifier)
            elif kind=='car':
                await c.execute('DELETE FROM cars WHERE id=$1',identifier)
            elif kind=='user':
                await c.execute('DELETE FROM users WHERE id=$1',identifier)
            else:
                raise ValueError('Unknown journal event')
        for cid in affected:
            await service.rebase(c,cid,forced.get(cid,()))
            await service.sync(c,cid)
    return len(events)


async def main():
    config=Config.load()
    db=await pool(config)
    try:
        count=await replay(Service(db,config),config.deletion_journal_dir)
        print(f'Applied {count} deletion journal events')
    finally:
        await db.close()


if __name__=='__main__':
    asyncio.run(main())
