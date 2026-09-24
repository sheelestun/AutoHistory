from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import domain as d
from app.config import Config


@pytest.mark.parametrize('value', ['31.02.2026','09/10/26','1.01.2026','2026-01-01'])
def test_strict_dates(value):
    with pytest.raises(d.AppError):
        d.parse_date(value)


@pytest.mark.parametrize('value', ['-1','NaN','45,000.999','1e3','1.001','10000000.01'])
def test_bad_money(value):
    with pytest.raises(d.AppError):
        d.money(value)


def test_money_missing_is_not_zero():
    assert d.money(None) is None
    assert d.money('0') == Decimal('0.00')
    assert d.money('4500,50') == Decimal('4500.50')


def test_calendar():
    assert d.add_months(date(2026,1,31),1) == date(2026,2,28)
    assert d.add_months(date(2024,1,31),1) == date(2024,2,29)
    assert d.add_months(date(2024,2,29),12) == date(2025,2,28)
    assert d.add_months(date(2024,2,29),-24) == date(2022,2,28)


def test_thresholds():
    p = dict(initial_date=date(2026,1,31),initial_odometer_km=1000,interval_km=1000,interval_months=1,active=True)
    assert d.plan_state(p,1499,date(2026,2,20),Config())['stage'] is None
    assert d.plan_state(p,1500,date(2026,2,20),Config())['stage'] == 'soon'
    assert d.plan_state(p,1000,date(2026,2,21),Config())['stage'] == 'soon'
    assert d.plan_state(p,2000,date(2026,2,28),Config())['reasons'] == ['пробег','дата']
    p['interval_km']=100
    p['interval_months']=None
    assert d.plan_state(p,1000,date(2026,1,31),Config())['stage'] is None
    assert d.plan_state(p,1001,date(2026,1,31),Config())['stage'] == 'soon'


@pytest.mark.parametrize('hour,day,expected', [(1,24,2),(2,24,2),(12,24,12),(13,25,2),(23,25,2)])
def test_delivery_window(hour,day,expected):
    result=d.delivery_time(datetime(2026,9,24,hour,tzinfo=timezone.utc),'Asia/Novosibirsk',Config())
    assert result.day == day and result.hour == expected


def test_photo_validation():
    data=dict(photo_file_id='file',photo_unique_id='unique',photo_kind='document',photo_mime='application/pdf',photo_size=5)
    with pytest.raises(d.AppError):
        d.photo(data,Config())
    data['photo_mime']='image/png'
    data['photo_size']=5242881
    with pytest.raises(d.AppError):
        d.photo(data,Config())
