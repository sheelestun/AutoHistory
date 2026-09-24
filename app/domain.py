"""Pure validation and calendar rules shared by bot, service and cron."""
import calendar
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import WORKS


class AppError(Exception):
    def __init__(self, code, message, field=None, retryable=False):
        super().__init__(message)
        self.code, self.message, self.field, self.retryable = code, message, field, retryable


def invalid(field, message):
    raise AppError('VALIDATION_ERROR', message, field)


def uid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise AppError('NOT_FOUND', 'Автомобиль или запись недоступны') from None


def text(value, field, maximum, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        invalid(field, 'Введите текст')
    value = value.strip()
    if optional and not value:
        return None
    if not 1 <= len(value) <= maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        invalid(field, f'Введите от 1 до {maximum} символов')
    return value


def integer(value, field, low=0, high=2_000_000, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value).strip()):
        invalid(field, f'Введите целое число от {low} до {high}')
    result = int(str(value).strip())
    if not low <= result <= high:
        invalid(field, f'Введите число от {low} до {high}')
    return result


def money(value):
    if value is None:
        return None
    s = str(value).strip().replace(',', '.')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', s):
        invalid('cost_rub', 'Введите сумму с точностью до копеек, например 4500,50')
    try:
        result = Decimal(s)
    except InvalidOperation:
        invalid('cost_rub', 'Неверная сумма')
    if not 0 <= result <= 10_000_000:
        invalid('cost_rub', 'Сумма должна быть от 0 до 10 000 000 ₽')
    return result.quantize(Decimal('.01'))


def parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', value):
        invalid('service_date', 'Введите дату в формате ДД.ММ.ГГГГ')
    try:
        return datetime.strptime(value, '%d.%m.%Y').date()
    except ValueError:
        invalid('service_date', 'Такой даты не существует')


def service_date(value, year, today):
    result = parse_date(value)
    if not date(year, 1, 1) <= result <= today:
        invalid('service_date', 'Дата должна быть не раньше года выпуска и не в будущем')
    return result


def zone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        invalid('timezone', 'Укажите IANA-пояс, например Asia/Novosibirsk')
    return value


def add_months(day, months):
    y, m = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def plan_state(plan, odometer, today, config):
    base_date = plan.get('base_date') or plan['initial_date']
    base_km = plan.get('base_km')
    if base_km is None:
        base_km = plan['initial_odometer_km']
    km = base_km + plan['interval_km'] if plan['interval_km'] else None
    day = add_months(base_date, plan['interval_months']) if plan['interval_months'] else None
    stage, reasons = None, []
    if plan['active']:
        if km is not None and odometer >= km:
            reasons.append('пробег')
        if day is not None and today >= day:
            reasons.append('дата')
        if reasons:
            stage = 'due'
        else:
            if km is not None and odometer >= km - min(config.remind_km, plan['interval_km'] - 1):
                reasons.append('пробег')
            if day is not None and today >= day - timedelta(days=config.remind_days):
                reasons.append('дата')
            if reasons:
                stage = 'soon'
    return dict(next_date=day, next_odometer_km=km, stage=stage, reasons=reasons)


def delivery_time(now, tz, config):
    local = now.astimezone(ZoneInfo(tz))
    if local.hour < config.send_hour_from:
        local = local.replace(hour=config.send_hour_from, minute=0, second=0, microsecond=0)
    elif local.hour >= config.send_hour_to:
        local = (local + timedelta(days=1)).replace(hour=config.send_hour_from, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def work(value, custom=None, plan=False):
    if value not in WORKS or (plan and value == 'OTHER'):
        invalid('work_code', 'Выберите вид работ из списка')
    return value, text(custom, 'custom_work', 60) if value == 'OTHER' else None


def photo(data, config):
    keys = ('photo_file_id', 'photo_unique_id', 'photo_kind')
    if not any(data.get(k) for k in keys):
        return dict.fromkeys(keys)
    if not all(data.get(k) for k in keys) or data['photo_kind'] not in ('photo', 'document'):
        invalid('photo', 'Прикрепите одно изображение JPEG или PNG')
    if data.get('photo_mime') not in ('image/jpeg', 'image/png'):
        invalid('photo', 'Поддерживаются только JPEG и PNG')
    size = data.get('photo_size')
    if not isinstance(size, int) or size <= 0 or size > config.photo_max_bytes:
        raise AppError('LIMIT_REACHED', 'Фото должно быть не больше 5 МиБ', 'photo')
    return {k: text(data[k], k, 1024) for k in keys}
