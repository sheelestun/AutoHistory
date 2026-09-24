import asyncio
import hashlib

import asyncpg

from app.config import Config, ROOT


async def pool(config):
    return await asyncpg.create_pool(config.database_url, min_size=1, max_size=5, command_timeout=20)


async def migrate(connection):
    await connection.execute('SELECT pg_advisory_lock(731100)')
    try:
        await connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
        for path in sorted((ROOT / 'migrations').glob('*.sql')):
            body = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(body.encode()).hexdigest()
            old = await connection.fetchval('SELECT checksum FROM schema_migrations WHERE name=$1', path.name)
            if old:
                if checksum != old:
                    raise RuntimeError('Applied migration was modified: ' + path.name)
                continue
            async with connection.transaction():
                await connection.execute(body)
                await connection.execute('INSERT INTO schema_migrations(name,checksum) VALUES($1,$2)', path.name, checksum)
    finally:
        await connection.execute('SELECT pg_advisory_unlock(731100)')


async def main():
    connection = await asyncpg.connect(Config.load().database_url)
    try:
        await migrate(connection)
    finally:
        await connection.close()


if __name__ == '__main__':
    asyncio.run(main())
