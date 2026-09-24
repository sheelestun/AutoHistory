import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

WORKS = {
    'ENGINE_OIL': 'Замена масла', 'OIL_FILTER': 'Масляный фильтр',
    'AIR_FILTER': 'Воздушный фильтр', 'CABIN_FILTER': 'Салонный фильтр',
    'BRAKE_FLUID': 'Тормозная жидкость', 'OTHER': 'Другое',
}
DISCLAIMER = 'Сведения внесены владельцем. AutoHistory не подтверждает выполнение работ и достоверность пробега.'
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bot_token: str = ''
    database_url: str = ''
    max_users: int = 50
    max_cars: int = 3
    max_records: int = 500
    photo_max_bytes: int = 5 * 1024**2
    pdf_max_bytes: int = 10 * 1024**2
    remind_days: int = 7
    remind_km: int = 500
    odometer_refresh_days: int = 30
    fsm_ttl_min: int = 30
    send_hour_from: int = 9
    send_hour_to: int = 20
    pdf_tmp_dir: str = 'tmp/reports'
    deletion_journal_dir: str = 'tmp/deletions'
    log_level: str = 'INFO'

    @classmethod
    def load(cls):
        load_dotenv()
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            value = os.getenv(name.upper(), getattr(defaults, name))
            values[name] = int(value) if isinstance(getattr(defaults, name), int) else value
        result = cls(**values)
        if not 0 <= result.send_hour_from < result.send_hour_to <= 24:
            raise ValueError('Invalid delivery window')
        for name, value in values.items():
            if isinstance(value, int) and name not in ('send_hour_from',) and value <= 0:
                raise ValueError(f'{name} must be positive')
        return result
