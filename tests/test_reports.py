from datetime import date, datetime, timezone
from decimal import Decimal
import time

import pytest
from pypdf import PdfReader

from app.reports import render
from app.domain import AppError


def sample(n):
    return dict(car=dict(brand='Лада',model='Веста',year=2020,odometer_km=150000,odometer_as_of=date(2026,9,24),telegram_user_id=987654321),exported_at=datetime(2026,9,24,tzinfo=timezone.utc),records=[dict(service_date=date(2026,9,20),odometer_km=i*100,work_code='OTHER',custom_work='Проверка <деталей> & замена '+ 'я'*30,cost_rub=Decimal('4500.50') if i%2==0 else None,station='СТО «Проверка» '+ 'Ж'*100,note='Примечание: '+ 'длинный текст '*34,photo_file_id='PRIVATE_PHOTO_KEY') for i in range(n)])


@pytest.mark.parametrize('n',[1,500])
def test_pdf(n,tmp_path):
    path=tmp_path/f'report-{n}.pdf'
    start=time.monotonic()
    render(sample(n),path)
    assert time.monotonic()-start < 15
    assert path.stat().st_size < 10*1024**2
    reader=PdfReader(path)
    text='\n'.join(p.extract_text() for p in reader.pages)
    assert 'Лада Веста' in text
    if n > 1:
        assert 'Не указано' in text
    assert '\x00' not in text
    assert '987654321' not in text and 'PRIVATE_PHOTO_KEY' not in text
    assert 'AutoHistory не подтверждает' in text
    assert 'Проверка <деталей> & замена' in text


def test_empty(tmp_path):
    with pytest.raises(AppError):
        render(sample(0),tmp_path/'empty.pdf')


def test_multiline_note_can_split_across_pages(tmp_path):
    data=sample(1)
    data['records'][0]['note']='я\n'*249
    path=tmp_path/'multiline.pdf'
    render(data,path)
    reader=PdfReader(path)
    assert len(reader.pages)>1
    text='\n'.join(p.extract_text() for p in reader.pages)
    assert 'Известные расходы' in text
