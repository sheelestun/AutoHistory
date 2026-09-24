import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app import domain as v
from app.config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    request_id: UUID = field(default_factory=uuid4)


def payload(data):
    return json.dumps(data, default=str, sort_keys=True, ensure_ascii=False)


class Service:
    def __init__(self, pool, config=None, clock=None):
        self.pool, self.config = pool, config or Config()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def call(self, method, ctx=None, **data):
        request_id = ctx.request_id if ctx else uuid4()
        try:
            result = await getattr(self, method.replace('.', '_'))(ctx=ctx, **data)
            return dict(ok=True, data=result, request_id=str(request_id))
        except v.AppError as exc:
            return dict(ok=False, error=dict(code=exc.code, message=exc.message, field=exc.field, retryable=exc.retryable), request_id=str(request_id))
        except Exception as exc:
            # Exception messages can contain SQL parameters, DSNs or tokens.
            log.error('service_failure request_id=%s class=%s', request_id, type(exc).__name__)
            return dict(ok=False, error=dict(code='TEMPORARY_UNAVAILABLE', message='Не удалось завершить действие. Повторите позже', field=None, retryable=True), request_id=str(request_id))

    async def user_start(self, telegram_id, timezone_name=None, ctx=None):
        telegram_id = v.integer(telegram_id, 'telegram_id', 1, 2**63-1)
        if timezone_name is not None:
            v.zone(timezone_name)
        async with self.pool.acquire() as c, c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(731101)')
            user = await c.fetchrow('SELECT * FROM users WHERE telegram_user_id=$1 FOR UPDATE', telegram_id)
            if user is None:
                if timezone_name is None:
                    return None
                if await c.fetchval('SELECT count(*) FROM users') >= self.config.max_users:
                    raise v.AppError('LIMIT_REACHED', 'Пилот рассчитан на 50 владельцев. Новые места пока недоступны')
                user = await c.fetchrow('INSERT INTO users(id,telegram_user_id,timezone) VALUES($1,$2,$3) RETURNING *', uuid4(), telegram_id, timezone_name)
            else:
                user = await c.fetchrow('UPDATE users SET timezone=COALESCE($2,timezone),delivery_enabled=true WHERE id=$1 RETURNING *', user['id'], timezone_name)
            cars = await c.fetch('SELECT id FROM cars WHERE user_id=$1 ORDER BY id', user['id'])
            for car in cars:
                await c.fetchrow('SELECT id FROM cars WHERE id=$1 FOR UPDATE', car['id'])
                await self.sync(c, car['id'])
            return dict(user)

    async def user_get(self, telegram_id):
        async with self.pool.acquire() as c:
            row = await c.fetchrow('SELECT * FROM users WHERE telegram_user_id=$1', telegram_id)
            return dict(row) if row else None

    async def owned(self, c, ctx, car_id, lock=False):
        row = await c.fetchrow('SELECT c.*,u.timezone,u.delivery_enabled,u.telegram_user_id FROM cars c JOIN users u ON u.id=c.user_id WHERE c.id=$1 AND c.user_id=$2' + (' FOR UPDATE OF c' if lock else ''), v.uid(car_id), ctx.user_id)
        if not row:
            raise v.AppError('NOT_FOUND', 'Автомобиль или запись недоступны')
        return dict(row)

    def today(self, tz):
        return self.clock().astimezone(ZoneInfo(tz)).date()

    async def car_list(self, ctx):
        async with self.pool.acquire() as c:
            return [dict(r) for r in await c.fetch('SELECT * FROM cars WHERE user_id=$1 ORDER BY created_at,id', ctx.user_id)]

    async def car_get(self, ctx, car_id):
        async with self.pool.acquire() as c:
            return await self.owned(c, ctx, car_id)

    async def repeated(self, c, table, key, body, owner_id, car_id=None):
        # table is selected exclusively by service code, never supplied by a user.
        await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', str(key))
        old = await c.fetchrow(f'SELECT * FROM {table} WHERE creation_key=$1', key)
        if old:
            owned = old['user_id'] == owner_id if table == 'cars' else old['car_id'] == car_id
            if not owned or json.loads(old['creation_payload']) != json.loads(body):
                raise v.AppError('CONFLICT', 'Ключ сохранения уже использован. Откройте форму заново')
            return dict(old)

    async def car_create(self, ctx, creation_key, **data):
        key = v.uid(creation_key)
        async with self.pool.acquire() as c, c.transaction():
            user = await c.fetchrow('SELECT * FROM users WHERE id=$1 FOR UPDATE', ctx.user_id)
            if not user:
                raise v.AppError('NOT_FOUND', 'Откройте /start')
            today = self.today(user['timezone'])
            d = dict(brand=v.text(data.get('brand'), 'brand', 60), model=v.text(data.get('model'), 'model', 60), year=v.integer(data.get('year'), 'year', 1900, today.year), odometer_km=v.integer(data.get('odometer_km'), 'odometer_km'))
            body = payload(d)
            old = await self.repeated(c, 'cars', key, body, ctx.user_id)
            if old:
                return old
            if await c.fetchval('SELECT count(*) FROM cars WHERE user_id=$1', ctx.user_id) >= self.config.max_cars:
                raise v.AppError('LIMIT_REACHED', f'Можно добавить не больше {self.config.max_cars} автомобилей')
            return dict(await c.fetchrow('INSERT INTO cars(id,user_id,creation_key,creation_payload,brand,model,year,odometer_km,odometer_as_of) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *', uuid4(), ctx.user_id, key, body, *d.values(), today))

    async def chronology(self, c, car, day, km):
        if day < self.today(car['timezone']) and km > car['odometer_km']:
            v.invalid('odometer_km', 'Сначала обновите текущий пробег автомобиля')
        bad = await c.fetchval('SELECT EXISTS(SELECT 1 FROM maintenance_records WHERE car_id=$1 AND ((service_date<$2 AND odometer_km>$3) OR (service_date>$2 AND odometer_km<$3)))', car['id'], day, km)
        if bad:
            v.invalid('odometer_km', 'Пробег противоречит записям на более ранние или поздние даты')

    async def record_create(self, ctx, car_id, creation_key, **data):
        key = v.uid(creation_key)
        async with self.pool.acquire() as c, c.transaction():
            car = await self.owned(c, ctx, car_id, True)
            code, custom = v.work(data.get('work_code'), data.get('custom_work'))
            d = dict(service_date=v.service_date(data.get('service_date'), car['year'], self.today(car['timezone'])), odometer_km=v.integer(data.get('odometer_km'), 'odometer_km'), work_code=code, custom_work=custom, cost_rub=v.money(data.get('cost_rub')), station=v.text(data.get('station'), 'station', 120, True), note=v.text(data.get('note'), 'note', 500, True), **v.photo(data, self.config))
            body = payload(d)
            old = await self.repeated(c, 'maintenance_records', key, body, ctx.user_id, car['id'])
            if old:
                return old
            if await c.fetchval('SELECT count(*) FROM maintenance_records WHERE car_id=$1', car['id']) >= self.config.max_records:
                raise v.AppError('LIMIT_REACHED', f'Лимит — {self.config.max_records} записей на автомобиль. Черновик сохранён')
            await self.chronology(c, car, d['service_date'], d['odometer_km'])
            record = await c.fetchrow('INSERT INTO maintenance_records(id,car_id,creation_key,creation_payload,service_date,odometer_km,work_code,custom_work,cost_rub,station,note,photo_file_id,photo_unique_id,photo_kind) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING *', uuid4(), car['id'], key, body, *d.values())
            if d['service_date'] == self.today(car['timezone']) and d['odometer_km'] >= car['odometer_km']:
                await c.execute('UPDATE cars SET odometer_km=$2,odometer_as_of=$3,updated_at=now() WHERE id=$1', car['id'], d['odometer_km'], d['service_date'])
            await self.rebase(c, car['id'])
            await self.sync(c, car['id'])
            return dict(record)

    async def car_set_odometer(self, ctx, car_id, odometer_km, correction=False, confirmed=False):
        km = v.integer(odometer_km, 'odometer_km')
        async with self.pool.acquire() as c, c.transaction():
            car = await self.owned(c, ctx, car_id, True)
            if correction and not confirmed:
                raise v.AppError('CONFLICT', 'Подтвердите исправление пробега')
            floor = await c.fetchval('SELECT greatest(COALESCE((SELECT max(odometer_km) FROM maintenance_records WHERE car_id=$1),0),COALESCE((SELECT max(initial_odometer_km) FROM maintenance_plans WHERE car_id=$1),0))', car['id'])
            if km < floor or (km < car['odometer_km'] and not correction):
                v.invalid('odometer_km', f'Пробег не может быть меньше {floor if correction else max(floor,car["odometer_km"])} км. Для исправления используйте настройки')
            result = await c.fetchrow('UPDATE cars SET odometer_km=$2,odometer_as_of=$3,updated_at=now() WHERE id=$1 RETURNING *', car['id'], km, self.today(car['timezone']))
            await self.sync(c, car['id'])
            return dict(result)

    async def plans(self, c, car_id):
        return [dict(r) for r in await c.fetch('SELECT p.*,r.service_date AS base_date,r.odometer_km AS base_km FROM maintenance_plans p LEFT JOIN maintenance_records r ON r.id=p.last_record_id WHERE p.car_id=$1 ORDER BY p.work_code', car_id)]

    async def plan_list(self, ctx, car_id):
        async with self.pool.acquire() as c:
            car = await self.owned(c, ctx, car_id)
            return [dict(p, **v.plan_state(p, car['odometer_km'], self.today(car['timezone']), self.config)) for p in await self.plans(c, car['id'])]

    async def rebase(self, c, car_id, force_ids=()):
        for p in await self.plans(c, car_id):
            base = await c.fetchval('SELECT id FROM maintenance_records WHERE car_id=$1 AND work_code=$2 AND service_date>=$3 AND odometer_km>=$4 ORDER BY service_date DESC,odometer_km DESC,id DESC LIMIT 1', car_id, p['work_code'], p['initial_date'], p['initial_odometer_km'])
            if base != p['last_record_id'] or p['id'] in force_ids:
                await c.execute('UPDATE maintenance_plans SET last_record_id=$2,cycle_no=cycle_no+1,updated_at=now() WHERE id=$1', p['id'], base)

    async def plan_upsert(self, ctx, car_id, work_code, interval_km=None, interval_months=None, initial_date=None, initial_odometer_km=None, active=True):
        code, _ = v.work(work_code, plan=True)
        km = v.integer(interval_km, 'interval_km', 100, 100000, True)
        months = v.integer(interval_months, 'interval_months', 1, 60, True)
        if km is None and months is None:
            v.invalid('interval_km', 'Укажите хотя бы один интервал')
        if not isinstance(active, bool):
            v.invalid('active', 'Неверное состояние плана')
        async with self.pool.acquire() as c, c.transaction():
            car = await self.owned(c, ctx, car_id, True)
            day = v.service_date(initial_date, car['year'], self.today(car['timezone']))
            base_km = v.integer(initial_odometer_km, 'initial_odometer_km', 0, car['odometer_km'])
            await self.chronology(c, car, day, base_km)
            base = await c.fetchval('SELECT id FROM maintenance_records WHERE car_id=$1 AND work_code=$2 AND service_date>=$3 AND odometer_km>=$4 ORDER BY service_date DESC,odometer_km DESC,id DESC LIMIT 1', car['id'], code, day, base_km)
            old = await c.fetchrow('SELECT * FROM maintenance_plans WHERE car_id=$1 AND work_code=$2', car['id'], code)
            vals = dict(interval_km=km, interval_months=months, initial_date=day, initial_odometer_km=base_km, active=active, last_record_id=base)
            if old:
                changed = any(old[k] != val for k, val in vals.items())
                await c.execute('UPDATE maintenance_plans SET interval_km=$2,interval_months=$3,initial_date=$4,initial_odometer_km=$5,active=$6,last_record_id=$7,cycle_no=cycle_no+$8,updated_at=now() WHERE id=$1', old['id'], *vals.values(), int(changed))
            else:
                await c.execute('INSERT INTO maintenance_plans(id,car_id,work_code,interval_km,interval_months,initial_date,initial_odometer_km,active,last_record_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)', uuid4(), car['id'], code, *vals.values())
            await self.sync(c, car['id'])
            return next(dict(p, **v.plan_state(p, car['odometer_km'], self.today(car['timezone']), self.config)) for p in await self.plans(c, car['id']) if p['work_code'] == code)

    async def record_get(self, ctx, record_id):
        async with self.pool.acquire() as c:
            row = await c.fetchrow('SELECT r.* FROM maintenance_records r JOIN cars c ON c.id=r.car_id WHERE r.id=$1 AND c.user_id=$2', v.uid(record_id), ctx.user_id)
            if not row:
                raise v.AppError('NOT_FOUND', 'Автомобиль или запись недоступны')
            return dict(row)

    async def record_list(self, ctx, car_id, mode='owner', period='all', page=1):
        if mode not in ('owner', 'sto') or period not in ('all', '2y'):
            v.invalid('period', 'Неверный фильтр истории')
        page = v.integer(page, 'page', 1, 1000)
        async with self.pool.acquire() as c:
            car = await self.owned(c, ctx, car_id)
            since = v.add_months(self.today(car['timezone']), -24) if period == '2y' else None
            rows = await c.fetch('SELECT * FROM maintenance_records WHERE car_id=$1 AND ($2::date IS NULL OR service_date>=$2) ORDER BY service_date DESC,odometer_km DESC,id DESC LIMIT 6 OFFSET $3', car['id'], since, (page-1)*5)
            return dict(items=[dict(r) for r in rows[:5]], page=page, has_next=len(rows)>5)

    async def report_snapshot(self, ctx, car_id):
        async with self.pool.acquire() as c, c.transaction(isolation='repeatable_read', readonly=True):
            car = await self.owned(c, ctx, car_id)
            rows = await c.fetch('SELECT * FROM maintenance_records WHERE car_id=$1 ORDER BY service_date,odometer_km,id', car['id'])
            if not rows:
                raise v.AppError('EMPTY_HISTORY', 'Добавьте первую запись, чтобы создать отчёт')
            return dict(car=car, records=[dict(r) for r in rows], exported_at=self.clock())

    def journal(self, kind, identifier):
        # Append and fsync BEFORE deleting. Separate mount is required in production.
        directory = Path(self.config.deletion_journal_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = self.clock()
        with (directory / (now.strftime('%Y-%m-%d') + '.jsonl')).open('a', encoding='utf-8') as f:
            f.write(payload(dict(kind=kind, id=str(identifier), at=now.isoformat())) + '\n')
            f.flush()
            os.fsync(f.fileno())

    async def record_delete(self, ctx, record_id, confirmed=False):
        if not confirmed:
            raise v.AppError('CONFLICT', 'Подтвердите удаление')
        rid = v.uid(record_id)
        async with self.pool.acquire() as c, c.transaction():
            cid = await c.fetchval('SELECT r.car_id FROM maintenance_records r JOIN cars c ON c.id=r.car_id WHERE r.id=$1 AND c.user_id=$2', rid, ctx.user_id)
            if cid is None:
                return dict(deleted=True)
            await self.owned(c, ctx, cid, True)
            ids = [r['id'] for r in await c.fetch('SELECT id FROM maintenance_plans WHERE last_record_id=$1', rid)]
            self.journal('record', rid)
            await c.execute('DELETE FROM maintenance_records WHERE id=$1', rid)
            await self.rebase(c, cid, ids)
            await self.sync(c, cid)
            return dict(deleted=True)

    async def car_delete(self, ctx, car_id, confirmed=False):
        if not confirmed:
            raise v.AppError('CONFLICT', 'Подтвердите удаление')
        async with self.pool.acquire() as c, c.transaction():
            cid = await c.fetchval('SELECT id FROM cars WHERE id=$1 AND user_id=$2 FOR UPDATE', v.uid(car_id), ctx.user_id)
            if cid:
                self.journal('car', cid)
                await c.execute('DELETE FROM cars WHERE id=$1', cid)
            return dict(deleted=True)

    async def user_delete(self, ctx, confirmed=False):
        if not confirmed:
            raise v.AppError('CONFLICT', 'Подтвердите удаление аккаунта')
        async with self.pool.acquire() as c, c.transaction():
            await c.fetchrow('SELECT id FROM users WHERE id=$1 FOR UPDATE', ctx.user_id)
            self.journal('user', ctx.user_id)
            await c.execute('DELETE FROM users WHERE id=$1', ctx.user_id)
            return dict(deleted=True)

    async def sync(self, c, car_id):
        """Caller holds cars row lock; reconcile desired events without resetting retry timers."""
        row = await c.fetchrow('SELECT c.*,u.timezone,u.delivery_enabled FROM cars c JOIN users u ON u.id=c.user_id WHERE c.id=$1', car_id)
        if not row:
            return
        car = dict(row)
        now, today = self.clock(), self.today(car['timezone'])
        desired = []
        for p in await self.plans(c, car_id):
            state = v.plan_state(p, car['odometer_km'], today, self.config)
            if state['stage']:
                stage = state['stage']
                desired.append((f'{p["id"]}:{p["cycle_no"]}:{stage}', p['id'], 'maintenance', stage, p['cycle_no']))
        period = (today-car['odometer_as_of']).days // self.config.odometer_refresh_days
        if period >= 1:
            desired.append((f'{car_id}:{car["odometer_as_of"]}:{period}', None, 'odometer', 'refresh', None))
        keys = [d[0] for d in desired] if car['delivery_enabled'] else []
        current_refresh = [d[0] for d in desired if d[2] == 'odometer']
        await c.execute("UPDATE notification_outbox o SET retired_at=$2 WHERE o.car_id=$1 AND o.retired_at IS NULL AND ((o.event_type='maintenance' AND NOT EXISTS(SELECT 1 FROM maintenance_plans p WHERE p.id=o.plan_id AND p.cycle_no=o.cycle_no)) OR (o.event_type='odometer' AND NOT(o.dedupe_key=ANY($3::text[]))))", car_id, now, current_refresh)
        await c.execute("UPDATE notification_outbox SET status='cancelled' WHERE car_id=$1 AND status='pending' AND NOT(dedupe_key=ANY($2::text[]))", car_id, keys)
        if car['delivery_enabled']:
            for key, pid, event, stage, cycle in desired:
                await c.execute("INSERT INTO notification_outbox(id,car_id,plan_id,event_type,stage,cycle_no,dedupe_key,next_attempt_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(dedupe_key) DO UPDATE SET status='pending',next_attempt_at=EXCLUDED.next_attempt_at WHERE notification_outbox.status='cancelled'", uuid4(), car_id, pid, event, stage, cycle, key, v.delivery_time(now, car['timezone'], self.config))
