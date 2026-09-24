"""Windows/local launcher: private PostgreSQL cluster, polling and minute ticks.

The server/VPS entry points remain app.bot and app.jobs.tick. This launcher is
only for a personal computer; it does not install a service or scheduled task.
"""
import argparse
import asyncio
import ctypes
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import dotenv_values

from app.config import Config, ROOT
from app.db import migrate

LOCAL = ROOT / '.local'
PGDATA = LOCAL / 'postgres'
STOP = LOCAL / 'stop.request'
RUNNER_LOCK = 731103
PORT = 55432
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class LocalError(Exception):
    pass


def pg_binary(name):
    suffix = '.exe' if os.name == 'nt' else ''
    configured = os.getenv('AUTOHISTORY_PG_BIN')
    candidates = [Path(configured)] if configured else []
    if os.name == 'nt':
        base = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'PostgreSQL'
        candidates += sorted(base.glob('*/bin'), reverse=True)
    for directory in candidates:
        binary = directory / (name + suffix)
        if binary.is_file():
            return str(binary)
    import shutil
    found = shutil.which(name)
    if found:
        return found
    raise LocalError('PostgreSQL не найден. Установите PostgreSQL или задайте AUTOHISTORY_PG_BIN')


def pg_command(name, *arguments, check=True):
    # pg_ctl descendants can inherit pipe handles on Windows; communicate() then
    # waits forever for EOF even though pg_ctl exited. Use a file, not PIPE.
    with (LOCAL / 'setup.log').open('ab', buffering=0) as output:
        result = subprocess.run([pg_binary(name), *map(str, arguments)], stdout=output, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, timeout=120)
    if check and result.returncode:
        # Neither password nor complete DSN is ever written to the console/log.
        raise LocalError(f'{name}: ошибка. Проверьте .local/postgres.log и установку PostgreSQL')
    return result


def ensure_database_files():
    LOCAL.mkdir(exist_ok=True)
    values = dotenv_values(ROOT / '.env')
    url = urlsplit(values.get('DATABASE_URL') or '')
    if url.hostname != '127.0.0.1' or url.port != PORT or url.path != '/autohistory' or url.username != 'autohistory':
        raise LocalError('Локальный запуск ожидает DATABASE_URL для autohistory на 127.0.0.1:55432. Другие базы не изменяются')
    password = values.get('POSTGRES_PASSWORD')
    if not password or url.password != password:
        raise LocalError('Проверьте совпадение пароля POSTGRES_PASSWORD и пароля в DATABASE_URL')
    if not (PGDATA / 'PG_VERSION').exists():
        if PGDATA.exists() and any(PGDATA.iterdir()):
            raise LocalError('Каталог .local/postgres не пуст, но кластер не готов. Автоматическая перезапись запрещена')
        secret = LOCAL / 'init-password.tmp'
        try:
            secret.write_text(password, encoding='utf-8')
            pg_command('initdb', '-D', PGDATA, '-U', 'autohistory', '--auth-host=scram-sha-256', '--auth-local=scram-sha-256', '--encoding=UTF8', '--locale=C', '--pwfile', secret)
            with (PGDATA / 'postgresql.conf').open('a', encoding='utf-8') as f:
                f.write(f"\n# AutoHistory local development only\nlisten_addresses = '127.0.0.1'\nport = {PORT}\nmax_connections = 30\n")
        finally:
            secret.unlink(missing_ok=True)
    if pg_command('pg_ctl', '-D', PGDATA, 'status', check=False).returncode != 0:
        pg_command('pg_ctl', '-D', PGDATA, '-l', LOCAL / 'postgres.log', '-w', '-t', '30', 'start')


async def prepare():
    await asyncio.to_thread(ensure_database_files)
    config = Config.load()
    # Connect to the private cluster's maintenance database to create our DB once.
    url = urlsplit(config.database_url)
    maintenance_dsn = url._replace(path='/postgres').geturl()
    c = await asyncpg.connect(maintenance_dsn, timeout=10)
    try:
        await c.execute('SELECT pg_advisory_lock(731104)')
        if not await c.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname='autohistory')"):
            await c.execute('CREATE DATABASE autohistory')
    finally:
        await c.close()
    c = await asyncpg.connect(config.database_url, timeout=10)
    try:
        await migrate(c)
    finally:
        await c.close()
    print('База AutoHistory готова: 127.0.0.1:55432. Существующие базы не изменены', flush=True)
    return config


async def check_token(config):
    if not config.bot_token:
        raise LocalError('Вставьте токен BotFather после BOT_TOKEN= в .env и сохраните файл')
    try:
        async with Bot(config.bot_token, session=AiohttpSession(timeout=20)) as bot:
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise LocalError('У бота включён webhook. Его нужно отключить перед локальным polling')
            return me.username
    except LocalError:
        raise
    except Exception as exc:
        raise LocalError(f'Не удалось проверить токен/соединение с Telegram ({type(exc).__name__}). Токен в журнал не выводится') from None


class ChildJob:
    """Windows closes child processes even when the launcher window is closed."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p,w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None,None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close()
            raise LocalError('Не удалось настроить завершение дочерних процессов Windows')

    def attach(self, process):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):
            process.terminate()
            process.wait()
            raise LocalError('Windows не разрешила связать процесс бота с окном запуска')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


async def launch(config):
    lock = await asyncpg.connect(config.database_url)
    job, bot, tick = None, None, None
    streams = []
    try:
        if not await lock.fetchval('SELECT pg_try_advisory_lock($1)',RUNNER_LOCK):
            raise LocalError('Бот уже запущен на этом компьютере. Второй экземпляр не нужен')
        username = await check_token(config)
        log_dir = LOCAL / 'logs'
        log_dir.mkdir(exist_ok=True)
        for path in log_dir.glob('*.log'):
            if path.stat().st_mtime < time.time()-14*86400:
                path.unlink(missing_ok=True)
        day = datetime.now().strftime('%Y-%m-%d')
        bot_log = (log_dir / f'bot-{day}.log').open('ab',buffering=0)
        tick_log = (log_dir / f'reminders-{day}.log').open('ab',buffering=0)
        streams = [bot_log,tick_log]
        job = ChildJob()
        STOP.unlink(missing_ok=True)
        child_env = os.environ.copy()
        child_env['PYTHONUTF8'] = '1'
        child_env['PYTHONUNBUFFERED'] = '1'
        def spawn(module, stream):
            proc = subprocess.Popen([sys.executable,'-m',module],cwd=ROOT,env=child_env,stdout=stream,stderr=subprocess.STDOUT,creationflags=NO_WINDOW)
            job.attach(proc)
            return proc
        bot = spawn('app.bot',bot_log)
        print(f'Бот @{username} запущен. Откройте https://t.me/{username} и отправьте /start',flush=True)
        print('Напоминания проверяются каждую минуту. Компьютер должен быть включён и не спать',flush=True)
        print('Остановка: Ctrl+C или файл «Остановить бота.cmd». Журналы: .local/logs',flush=True)
        next_tick = 0.0
        last_health = time.monotonic()
        while not STOP.exists():
            if bot.poll() is not None:
                raise LocalError('Процесс бота завершился. Подробности в .local/logs/bot-*.log')
            if tick is not None and tick.poll() is not None:
                if tick.returncode:
                    print('Не удалось проверить напоминания. Повтор через минуту; подробности в .local/logs',flush=True)
                tick = None
            if tick is None and time.monotonic() >= next_tick:
                tick = spawn('app.jobs.tick',tick_log)
                next_tick = time.monotonic()+60
            if time.monotonic()-last_health >= 30:
                # A lost database session also loses the single-runner advisory lock.
                await lock.fetchval('SELECT 1')
                last_health = time.monotonic()
            await asyncio.sleep(1)
    finally:
        for process in (tick,bot):
            if process is not None and process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait)
        if job:
            job.close()
        for stream in streams:
            stream.close()
        await lock.close()


async def main(args):
    os.chdir(ROOT)
    config = await prepare()
    if args.prepare:
        return
    if args.check:
        username = await check_token(config)
        print(f'Telegram доступен. Токен принадлежит @{username}',flush=True)
        return
    await launch(config)


def entry():
    parser=argparse.ArgumentParser(description='AutoHistory local launcher')
    parser.add_argument('--prepare',action='store_true',help='Prepare database without connecting to Telegram')
    parser.add_argument('--check',action='store_true',help='Check token without starting polling')
    args=parser.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print('\nБот остановлен. Данные сохранены')
    except LocalError as exc:
        print(str(exc),file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Ошибка локального запуска: {type(exc).__name__}. Проверьте PostgreSQL и .env',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(entry())
