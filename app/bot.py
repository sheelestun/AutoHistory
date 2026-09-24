"""Telegram UI: opaque, short-lived button contexts; per-user serialized dialogs."""
import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile, BotCommand

from app import domain as v
from app.config import Config, WORKS, DISCLAIMER
from app.db import pool
from app.reports import Reports, work_name, cost
from app.service import Service, AuthContext

log = logging.getLogger(__name__)


@dataclass
class Session:
    telegram_id: int | None = None
    touched: float = field(default_factory=time.monotonic)
    actions: dict = field(default_factory=dict)
    form: str | None = None
    fields: list = field(default_factory=list)
    step: int = 0
    values: dict = field(default_factory=dict)
    key: str = field(default_factory=lambda: str(uuid4()))
    car_id: str | None = None


LABELS = {
    'brand': 'Марка автомобиля (1–60 символов)', 'model': 'Модель (1–60 символов)',
    'year': 'Год выпуска', 'odometer_km': 'Пробег в километрах (0–2 000 000)',
    'service_date': 'Дата обслуживания (ДД.ММ.ГГГГ)', 'work_code': 'Выберите вид работ',
    'custom_work': 'Название работы (до 60 символов)', 'cost_rub': 'Стоимость в рублях, например 4500,50. Можно пропустить',
    'station': 'Название СТО (до 120 символов). Следующим шагом можно добавить примечание',
    'note': 'Примечание (до 500 символов), необязательно',
    'photo': 'Прикрепите одно фото JPEG/PNG до 5 МиБ или пропустите. На заказ-наряде могут быть персональные данные — проверьте изображение перед отправкой',
    'interval_km': 'Интервал в километрах: 100–100 000. Можно пропустить, если зададите месяцы',
    'interval_months': 'Интервал в календарных месяцах: 1–60. Нужен хотя бы один интервал',
    'initial_date': 'Дата последнего ТО — исходная точка (ДД.ММ.ГГГГ)',
    'initial_odometer_km': 'Пробег на момент этого ТО. Не больше текущего показания',
    'timezone_name': 'Выберите часовой пояс или введите IANA-название, например Asia/Novosibirsk',
}
OPTIONAL = {'cost_rub', 'station', 'note', 'photo', 'interval_km', 'interval_months'}


def fmt(value):
    if value is None:
        return 'Не указано'
    if hasattr(value, 'strftime'):
        return value.strftime('%d.%m.%Y')
    return str(value)


def record_text(r, detailed=False):
    result = f'{r["service_date"]:%d.%m.%Y} · {r["odometer_km"]} км\n{work_name(r)}\nСТО: {r.get("station") or "Не указано"}'
    if detailed:
        result += '\nСтоимость: ' + cost(r['cost_rub'])
        if r.get('note'):
            result += '\n' + r['note']
    return result


def plan_text(p):
    status = 'Включено' if p['active'] else 'Отключено'
    targets = []
    if p.get('next_date'):
        targets.append(fmt(p['next_date']))
    if p.get('next_odometer_km') is not None:
        targets.append(f'{p["next_odometer_km"]} км')
    return f'{WORKS[p["work_code"]]}: {status}\nСледующее ТО: ' + ' или '.join(targets)


class UI:
    def __init__(self, service, reports):
        self.service, self.reports = service, reports
        self.sessions = {}
        self.locks = {}
        self.capacity = asyncio.Semaphore(5)
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)

    def session(self, user_id):
        s = self.sessions.get(user_id)
        if s is None or time.monotonic()-s.touched > self.service.config.fsm_ttl_min*60:
            s = Session()
            self.sessions[user_id] = s
        s.touched = time.monotonic()
        s.telegram_id = user_id
        return s

    def markup(self, s, buttons):
        s.actions.clear()
        rows = []
        for label, action, args in buttons:
            token = uuid4().hex
            data = 'b:' + token
            assert len(data.encode()) <= 60
            s.actions[token] = (action, args)
            rows.append([InlineKeyboardButton(text=label, callback_data=data)])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    async def show(self, m, s, text, buttons=()):
        assert len(text) <= 3500
        await m.answer(text, reply_markup=self.markup(s, buttons), parse_mode=None)

    async def invoke(self, name, ctx, **kwargs):
        result = await self.service.call(name, ctx, **kwargs)
        if not result['ok']:
            error = result['error']
            raise v.AppError(error['code'], error['message'], error['field'], error['retryable'])
        return result['data']

    async def guard(self, event, handler):
        m = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(m, Message) or m.chat.type != 'private' or not event.from_user:
            if isinstance(event, CallbackQuery):
                await event.answer('Бот работает только в личном чате', show_alert=True)
            elif m:
                await m.answer('Откройте личный чат с ботом')
            return
        user_id = event.from_user.id
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        # Acquire per-user first; waiting clicks must not occupy all five workers.
        async with lock:
            if self.capacity.locked():
                await m.answer('Бот занят. Попробуйте через несколько секунд; сохранённые данные доступны')
                return
            async with self.capacity:
                s = self.session(user_id)
                try:
                    user = await self.service.user_get(user_id)
                    ctx = AuthContext(user['id']) if user else None
                    await handler(m, s, ctx, user_id)
                except v.AppError as exc:
                    if s.form and exc.field in s.fields:
                        s.step = s.fields.index(exc.field)
                    await m.answer(exc.message)
                    if s.form:
                        await self.prompt(m, s)
                except Exception as exc:
                    request_id = uuid4()
                    log.error('handler_failure request_id=%s class=%s', request_id, type(exc).__name__)
                    with suppress(TelegramAPIError):
                        await m.answer(f'Не удалось завершить действие. Повторите позже. Код: {request_id}')

    async def message(self, message):
        async def handle(m, s, ctx, user_id):
            command = (m.text or '').split('@')[0].strip()
            if command == '/help':
                await m.answer('AutoHistory — бесплатная сервисная книжка. Добавляйте ТО, настраивайте интервалы и обновляйте пробег вручную.\n/start — меню\n/cancel — отмена\n/delete_me — удалить аккаунт\n\nФото хранятся в Telegram, резервная копия БД не содержит оригиналы. ' + DISCLAIMER)
            elif command == '/cancel':
                s.form = None
                s.values.clear()
                await self.home(m, s, ctx)
            elif command == '/start':
                had_form = s.form is not None
                s.form = None
                s.values.clear()
                user = await self.invoke('user.start', None, telegram_id=user_id)
                if had_form:
                    await m.answer('Незавершённый ввод отменён. Сохранённые записи остаются в истории')
                if not user:
                    await self.begin(m, s, 'timezone', ['timezone_name'])
                else:
                    await self.home(m, s, AuthContext(user['id']))
            elif not ctx:
                if s.form == 'timezone':
                    await self.input(m, s)
                else:
                    await self.begin(m, s, 'timezone', ['timezone_name'])
            elif command == '/delete_me':
                await self.delete_confirm(m, s, 'user', None)
            elif s.form:
                await self.input(m, s)
            else:
                await m.answer('Незавершённые формы хранятся 30 минут и сбрасываются при перезапуске. Откройте /start, чтобы продолжить')
        await self.guard(message, handle)

    async def callback(self, callback):
        async def handle(m, s, ctx, user_id):
            await callback.answer()
            data = callback.data or ''
            if data.startswith('s:'):
                key = data[2:]
                if s.form and key == s.key and s.step == len(s.fields):
                    await self.save(m, s, ctx, user_id)
                    return
                if ctx:
                    # A repeated successful confirmation remains safe across restarts.
                    async with self.service.pool.acquire() as c:
                        old = await c.fetchval('SELECT id FROM cars WHERE creation_key=$1 AND user_id=$2', v.uid(key), ctx.user_id)
                        if not old:
                            old = await c.fetchval('SELECT r.car_id FROM maintenance_records r JOIN cars c ON c.id=r.car_id WHERE r.creation_key=$1 AND c.user_id=$2', v.uid(key), ctx.user_id)
                    if old:
                        await m.answer('Уже сохранено. Повторная запись не создана')
                        await self.car(m, s, ctx, old)
                        return
                raise v.AppError('CONFLICT', 'Это действие устарело. Откройте меню заново')
            entry = s.actions.get(data[2:]) if data.startswith('b:') else None
            if entry is None:
                raise v.AppError('CONFLICT', 'Это действие устарело. Откройте меню заново')
            action, args = entry
            if action == 'input':
                await self.accept(m, s, args)
            elif action == 'back':
                if s.step == 0:
                    s.form = None
                    await self.home(m, s, ctx)
                else:
                    s.step -= 1
                    await self.prompt(m, s)
            elif action == 'cancel':
                s.form = None
                s.values.clear()
                await self.home(m, s, ctx)
            elif action == 'save':
                await self.save(m, s, ctx, user_id)
            elif ctx is None:
                await self.begin(m, s, 'timezone', ['timezone_name'])
            elif action == 'home':
                s.form = None
                await self.home(m, s, ctx)
            elif action == 'car':
                await self.car(m, s, ctx, args)
            elif action == 'new_car':
                await self.begin(m, s, 'car', ['brand', 'model', 'year', 'odometer_km'])
            elif action == 'new_record':
                await self.service.car_get(ctx, args)
                await self.begin(m, s, 'record', ['service_date', 'odometer_km', 'work_code', 'cost_rub', 'station', 'note', 'photo'], args)
            elif action == 'odo':
                await self.service.car_get(ctx, args[0])
                await self.begin(m, s, 'correction' if args[1] else 'odometer', ['odometer_km'], args[0])
            elif action == 'history':
                await self.history(m, s, ctx, *args)
            elif action == 'record':
                r = await self.service.record_get(ctx, args)
                buttons = [('Удалить запись', 'delete_confirm', ('record', args)), ('К автомобилю', 'car', r['car_id'])]
                if r['photo_file_id']:
                    buttons.insert(0, ('Открыть фото', 'photo', args))
                await self.show(m, s, record_text(r, True), buttons)
            elif action == 'photo':
                r = await self.service.record_get(ctx, args)
                try:
                    if r['photo_kind'] == 'document':
                        await m.answer_document(r['photo_file_id'])
                    else:
                        await m.answer_photo(r['photo_file_id'])
                except TelegramAPIError:
                    await m.answer('Вложение недоступно в Telegram. Текст записи сохранён')
            elif action == 'plans':
                await self.plans(m, s, ctx, args)
            elif action == 'new_plan':
                await self.service.car_get(ctx, args)
                await self.begin(m, s, 'plan', ['work_code', 'interval_km', 'interval_months', 'initial_date', 'initial_odometer_km'], args)
            elif action == 'plan_choice':
                cid, code = args
                p = next(p for p in await self.service.plan_list(ctx, cid) if p['work_code'] == code)
                await self.show(m, s, plan_text(p) + '\nИсходная точка: ' + fmt(p['initial_date']) + f', {p["initial_odometer_km"]} км', [('Изменить', 'edit_plan', (cid, code)), ('Отключить' if p['active'] else 'Включить', 'toggle_confirm', (cid, code, not p['active'])), ('Назад', 'plans', cid)])
            elif action == 'edit_plan':
                cid, code = args
                p = next(p for p in await self.service.plan_list(ctx, cid) if p['work_code'] == code)
                await self.begin(m, s, 'plan', ['work_code', 'interval_km', 'interval_months', 'initial_date', 'initial_odometer_km'], cid, {k: p[k] for k in ('work_code','interval_km','interval_months','initial_date','initial_odometer_km')})
            elif action == 'toggle_confirm':
                await self.show(m, s, 'Подтвердите изменение напоминания', [('Подтвердить', 'toggle', args), ('Отмена', 'plans', args[0])])
            elif action == 'toggle':
                cid, code, active = args
                p = next(p for p in await self.service.plan_list(ctx, cid) if p['work_code'] == code)
                await self.invoke('plan.upsert', ctx, car_id=cid, active=active, **{k:p[k] for k in ('work_code','interval_km','interval_months','initial_date','initial_odometer_km')})
                await self.plans(m, s, ctx, cid)
            elif action == 'pdf':
                async def send(path, filename):
                    await m.answer_document(FSInputFile(path, filename=filename))
                await self.reports.send(ctx, args, send, m.answer)
            elif action == 'car_settings':
                car = await self.service.car_get(ctx, args)
                await self.show(m, s, 'Настройки ' + car['brand'] + ' ' + car['model'], [('Исправить пробег', 'odo', (args, True)), ('Удалить автомобиль', 'delete_confirm', ('car', args)), ('Назад', 'car', args)])
            elif action == 'settings':
                await self.show(m, s, 'Настройки', [('Часовой пояс', 'timezone', None), ('Удалить аккаунт', 'delete_confirm', ('user', None)), ('Мои автомобили', 'home', None)])
            elif action == 'timezone':
                await self.begin(m, s, 'timezone', ['timezone_name'])
            elif action == 'delete_confirm':
                kind, identifier = args
                if kind == 'car':
                    car = await self.service.car_get(ctx, identifier)
                    detail = car['brand'] + ' ' + car['model']
                elif kind == 'record':
                    detail = record_text(await self.service.record_get(ctx, identifier))
                else:
                    detail = 'Все автомобили, ТО и напоминания'
                await self.delete_confirm(m, s, kind, identifier, detail)
            elif action == 'delete':
                kind, identifier = args
                kwargs = {} if kind == 'user' else {kind + '_id': identifier}
                await self.invoke(kind + '.delete', ctx, confirmed=True, **kwargs)
                s.form = None
                s.values.clear()
                await m.answer('Данные удалены. Сообщения и пересланные файлы в Telegram могут сохраниться. Резервные копии хранятся до 7 дней')
                if kind == 'user':
                    self.sessions.pop(user_id, None)
                    await m.answer('Для нового аккаунта нажмите /start')
                else:
                    await self.home(m, s, ctx)
            elif action == 'use_base':
                s.values['initial_date'], s.values['initial_odometer_km'] = args
                s.step = len(s.fields)
                await self.prompt(m, s)
        await self.guard(callback, handle)

    async def home(self, m, s, ctx):
        if ctx is None:
            await self.begin(m, s, 'timezone', ['timezone_name'])
            return
        cars = await self.service.car_list(ctx)
        buttons = [(f'{c["brand"]} {c["model"]} · {c["year"]}', 'car', c['id']) for c in cars]
        buttons += [('Добавить автомобиль', 'new_car', None), ('Настройки', 'settings', None)]
        await self.show(m, s, 'Мои автомобили' if cars else 'Добро пожаловать в AutoHistory. Добавьте первый автомобиль', buttons)

    async def car(self, m, s, ctx, car_id):
        car = await self.service.car_get(ctx, car_id)
        plans = await self.service.plan_list(ctx, car_id)
        text = f'{car["brand"]} {car["model"]} · {car["year"]}\nПробег: {car["odometer_km"]} км от {car["odometer_as_of"]:%d.%m.%Y}'
        if (self.service.today(car['timezone'])-car['odometer_as_of']).days >= self.service.config.odometer_refresh_days:
            text += '\nПоказания устарели. Обновите пробег для точных напоминаний'
        if plans:
            text += '\n\n' + '\n\n'.join(plan_text(p) for p in plans)
        await self.show(m, s, text, [
            ('Добавить запись ТО', 'new_record', car_id), ('Моя история', 'history', (car_id,'all','owner',1)),
            ('Обновить пробег', 'odo', (car_id,False)), ('Напоминания', 'plans', car_id),
            ('История для СТО', 'history', (car_id,'2y','sto',1)), ('PDF отчёт', 'pdf', car_id),
            ('Настройки автомобиля', 'car_settings', car_id), ('Мои автомобили', 'home', None)])

    async def history(self, m, s, ctx, car_id, period, mode, page):
        result = await self.service.record_list(ctx, car_id, mode, period, page)
        title = ('История для СТО' if mode == 'sto' else 'Моя история') + (' · 2 года' if period == '2y' else ' · вся')
        # The compact page omits notes, limiting five maximum-length records to < 3500.
        text = title + f'\nСтраница {page}\n\n' + ('\n\n'.join(record_text(r) for r in result['items']) or 'История пока пуста')
        buttons = [(f'{r["service_date"]:%d.%m.%Y} · Подробнее', 'record', r['id']) for r in result['items']] if mode == 'owner' else []
        if page > 1:
            buttons.append(('← Назад', 'history', (car_id,period,mode,page-1)))
        if result['has_next']:
            buttons.append(('Далее →', 'history', (car_id,period,mode,page+1)))
        buttons += [('Вся история' if period == '2y' else 'За 2 года', 'history', (car_id,'all' if period == '2y' else '2y',mode,1)), ('К автомобилю', 'car', car_id)]
        await self.show(m, s, text, buttons)

    async def plans(self, m, s, ctx, car_id):
        plans = await self.service.plan_list(ctx, car_id)
        await self.show(m, s, 'Напоминания\n\n' + ('\n\n'.join(plan_text(p) for p in plans) or 'Задайте интервал по пробегу и/или месяцам. Пробег вводится вручную'), [(WORKS[p['work_code']], 'plan_choice', (car_id,p['work_code'])) for p in plans] + [('Добавить / настроить правило', 'new_plan', car_id), ('К автомобилю', 'car', car_id)])

    async def delete_confirm(self, m, s, kind, identifier, detail='Все автомобили, ТО и напоминания'):
        s.form = None
        await self.show(m, s, 'Удалить?\n' + detail + '\n\nВосстановить из интерфейса нельзя. Копии в Telegram не удаляются', [('Отмена', 'home', None), ('Удалить', 'delete', (kind,identifier))])

    async def begin(self, m, s, form, fields, car_id=None, values=None):
        s.form, s.fields, s.step, s.values, s.car_id = form, list(fields), 0, values or {}, car_id
        s.key = str(uuid4())
        await self.prompt(m, s)

    async def prompt(self, m, s):
        if s.step >= len(s.fields):
            summary = ['Проверьте данные перед сохранением:']
            for key in s.fields:
                if key == 'photo':
                    value = 'Прикреплено' if s.values.get('photo_file_id') else 'Без фото'
                elif key == 'work_code':
                    value = WORKS.get(s.values.get(key), '')
                else:
                    value = fmt(s.values.get(key))
                summary.append(LABELS[key].split(' (')[0].split(':')[0] + ': ' + value)
            if s.form == 'correction':
                summary.append('Подтвердите исправление текущего показания пробега')
            markup = self.markup(s, [('Назад', 'back', None), ('Отмена', 'cancel', None)])
            markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='Сохранить', callback_data='s:'+s.key)])
            await m.answer('\n'.join(summary), reply_markup=markup)
            return
        key = s.fields[s.step]
        buttons = []
        if key == 'work_code':
            buttons += [(name,'input',code) for code,name in WORKS.items() if s.form != 'plan' or code != 'OTHER']
        if key == 'timezone_name':
            buttons += [(name,'input',tz) for name,tz in [('Москва (UTC+3)','Europe/Moscow'),('Екатеринбург (UTC+5)','Asia/Yekaterinburg'),('Новосибирск (UTC+7)','Asia/Novosibirsk'),('Владивосток (UTC+10)','Asia/Vladivostok')]]
        if key in OPTIONAL:
            buttons.append(('Пропустить','input',None))
        if key in s.values and key != 'photo':
            buttons.append(('Оставить: ' + fmt(s.values[key])[:35], 'input', s.values[key]))
        if key == 'initial_date':
            user = await self.service.user_get(s.telegram_id)
            if user:
                records = await self.service.report_snapshot(AuthContext(user['id']), s.car_id) if await self._has_records(AuthContext(user['id']), s.car_id) else None
                matches = [r for r in records['records'] if r['work_code'] == s.values['work_code']] if records else []
                if matches:
                    last = matches[-1]
                    buttons.append((f'Взять ТО {last["service_date"]:%d.%m.%Y}, {last["odometer_km"]} км', 'use_base', (last['service_date'],last['odometer_km'])))
        buttons += [('Назад','back',None), ('Отмена','cancel',None)]
        await self.show(m, s, f'Шаг {s.step+1} из {len(s.fields)}\n' + LABELS[key], buttons)

    async def _has_records(self, ctx, car_id):
        return bool((await self.service.record_list(ctx, car_id))['items'])

    async def input(self, m, s):
        if s.step >= len(s.fields):
            await m.answer('Нажмите «Сохранить» или «Назад», чтобы исправить данные')
            return
        key = s.fields[s.step]
        if key == 'photo':
            if m.photo:
                item = m.photo[-1]
                value = dict(photo_file_id=item.file_id, photo_unique_id=item.file_unique_id, photo_kind='photo', photo_mime='image/jpeg', photo_size=item.file_size)
            elif m.document:
                item = m.document
                value = dict(photo_file_id=item.file_id, photo_unique_id=item.file_unique_id, photo_kind='document', photo_mime=item.mime_type, photo_size=item.file_size)
            else:
                v.invalid('photo', 'Отправьте изображение или нажмите «Пропустить»')
        else:
            if m.text is None:
                v.invalid(key, 'На этом шаге нужен текст')
            value = m.text
        await self.accept(m, s, value)

    async def accept(self, m, s, value):
        if not s.form or s.step >= len(s.fields):
            raise v.AppError('CONFLICT', 'Это действие устарело. Откройте /start')
        key = s.fields[s.step]
        if value is None and key not in OPTIONAL:
            v.invalid(key, 'Обязательное поле')
        if key in ('brand','model','custom_work','station','note'):
            value = v.text(value,key,{'station':120,'note':500}.get(key,60),key in OPTIONAL)
        elif key == 'year':
            value = v.integer(value,key,1900,datetime.now().year)
        elif key in ('odometer_km','initial_odometer_km'):
            value = v.integer(value,key)
        elif key in ('service_date','initial_date'):
            value = v.parse_date(value)
        elif key == 'cost_rub':
            value = v.money(value)
        elif key == 'interval_km':
            value = v.integer(value,key,100,100000,True)
        elif key == 'interval_months':
            value = v.integer(value,key,1,60,True)
            if value is None and s.values.get('interval_km') is None:
                v.invalid(key, 'Укажите хотя бы один интервал')
        elif key == 'timezone_name':
            value = v.zone(value)
        elif key == 'work_code':
            if value not in WORKS or (s.form == 'plan' and value == 'OTHER'):
                v.invalid(key, 'Выберите вид работ кнопкой')
            if 'custom_work' in s.fields:
                s.fields.remove('custom_work')
            s.values.pop('custom_work',None)
            if value == 'OTHER':
                s.fields.insert(s.step+1,'custom_work')
        elif key == 'photo':
            value = value or {}
            v.photo(value,self.service.config)
            for k in ('photo_file_id','photo_unique_id','photo_kind','photo_mime','photo_size'):
                s.values.pop(k,None)
            s.values.update(value)
        # Validate car-dependent date and chronology at input, then again in service.
        if s.car_id and key in ('service_date','initial_date','initial_odometer_km','odometer_km'):
            user = await self.service.user_get(s.telegram_id)
            ctx = AuthContext(user['id'])
            async with self.service.pool.acquire() as c:
                car = await self.service.owned(c,ctx,s.car_id)
                if key in ('service_date','initial_date'):
                    value = v.service_date(value,car['year'],self.service.today(car['timezone']))
                elif key == 'initial_odometer_km':
                    value = v.integer(value,key,0,car['odometer_km'])
                    await self.service.chronology(c,car,s.values['initial_date'],value)
                elif s.form == 'record' and s.values.get('service_date'):
                    await self.service.chronology(c,car,s.values['service_date'],value)
                elif s.form == 'odometer' and value < car['odometer_km']:
                    v.invalid(key,'Для уменьшения пробега используйте настройки автомобиля')
        s.values[key] = value
        s.step += 1
        await self.prompt(m,s)

    async def save(self, m, s, ctx, user_id):
        if not s.form or s.step != len(s.fields):
            raise v.AppError('CONFLICT','Подтверждение устарело')
        form, cid = s.form, s.car_id
        values = {k:val for k,val in s.values.items() if k != 'photo'}
        if form == 'timezone':
            user = await self.invoke('user.start',None,telegram_id=user_id,**values)
            ctx = AuthContext(user['id'])
        elif ctx is None:
            raise v.AppError('CONFLICT','Откройте /start')
        elif form == 'car':
            car = await self.invoke('car.create',ctx,creation_key=s.key,**values)
            cid = car['id']
        elif form == 'record':
            await self.invoke('record.create',ctx,car_id=cid,creation_key=s.key,**values)
        elif form == 'plan':
            await self.invoke('plan.upsert',ctx,car_id=cid,**values)
        else:
            await self.invoke('car.set_odometer',ctx,car_id=cid,correction=form=='correction',confirmed=True,**values)
        s.form = None
        s.values.clear()
        await m.answer('Сохранено')
        if form == 'record':
            # The review screen contained the full input; repeat the saved business fields.
            async with self.service.pool.acquire() as c:
                saved = await c.fetchrow('SELECT r.* FROM maintenance_records r JOIN cars c ON c.id=r.car_id WHERE r.creation_key=$1 AND c.user_id=$2', v.uid(s.key), ctx.user_id)
            if saved:
                await m.answer(record_text(dict(saved), True))
        if cid:
            await self.car(m,s,ctx,cid)
        elif not await self.service.car_list(ctx):
            await self.begin(m,s,'car',['brand','model','year','odometer_km'])
        else:
            await self.home(m,s,ctx)

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            for user_id, s in list(self.sessions.items()):
                if time.monotonic()-s.touched > self.service.config.fsm_ttl_min*60:
                    if not self.locks.get(user_id,asyncio.Lock()).locked():
                        self.sessions.pop(user_id,None)
            self.reports.cleanup()


async def main():
    config = Config.load()
    logging.basicConfig(level=config.log_level,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if not config.bot_token or not config.database_url:
        raise SystemExit('Заполните BOT_TOKEN и DATABASE_URL в .env')
    db = await pool(config)
    service = Service(db,config)
    reports = Reports(service)
    reports.cleanup()
    ui = UI(service,reports)
    dispatcher = Dispatcher()
    dispatcher.include_router(ui.router)
    janitor = asyncio.create_task(ui.cleanup_loop())
    try:
        async with Bot(config.bot_token,session=AiohttpSession(timeout=20)) as bot:
            await bot.set_my_commands([BotCommand(command='start',description='Мои автомобили'),BotCommand(command='help',description='Справка'),BotCommand(command='cancel',description='Отменить ввод'),BotCommand(command='delete_me',description='Удалить аккаунт')])
            await dispatcher.start_polling(bot,allowed_updates=['message','callback_query'],tasks_concurrency_limit=100)
    finally:
        janitor.cancel()
        with suppress(asyncio.CancelledError):
            await janitor
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
