import asyncio
import shutil
from datetime import datetime, timezone

from app.config import Config
from app.db import pool


async def main():
    config=Config.load()
    db=await pool(config)
    try:
        async with db.acquire() as c:
            last=await c.fetchval("SELECT last_success_at FROM job_health WHERE name='tick'")
            failed=await c.fetchval("SELECT count(*) FROM notification_outbox WHERE status='failed'")
        disk=shutil.disk_usage(config.pdf_tmp_dir)
        healthy=last is not None and (datetime.now(timezone.utc)-last).total_seconds()<=300 and not failed and disk.free/disk.total>=0.2
        print(f'healthy={healthy} last_tick={last} failed={failed} disk_free_percent={disk.free/disk.total*100:.1f}')
        return 0 if healthy else 1
    finally:
        await db.close()


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
