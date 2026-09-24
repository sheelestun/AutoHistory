import asyncio
import os
import re
import time
from decimal import Decimal
from html import escape
from pathlib import Path
from uuid import uuid4

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle

from app.config import DISCLAIMER, ROOT, WORKS
from app.domain import AppError


def work_name(record):
    return record.get('custom_work') or WORKS[record['work_code']]


def cost(value):
    return 'Не указано' if value is None else f'{value:,.2f}'.replace(',', ' ').replace('.', ',') + ' ₽'


def render(snapshot, path):
    if not snapshot['records']:
        raise AppError('EMPTY_HISTORY', 'Добавьте первую запись, чтобы создать отчёт')
    for name, file in [('Auto', 'Ubuntu-R.ttf'), ('AutoBold', 'Ubuntu-B.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(ROOT / 'assets/fonts' / file)))
    styles = {
        'body': ParagraphStyle('body', fontName='Auto', fontSize=9, leading=13, textColor=colors.HexColor('#243749'), wordWrap='CJK'),
        'small': ParagraphStyle('small', fontName='Auto', fontSize=8, leading=11, textColor=colors.HexColor('#566b7b'), wordWrap='CJK'),
        'title': ParagraphStyle('title', fontName='AutoBold', fontSize=28, leading=33, textColor=colors.HexColor('#12394a'), spaceAfter=12),
        'heading': ParagraphStyle('heading', fontName='AutoBold', fontSize=14, leading=19, spaceAfter=12, wordWrap='CJK'),
        'header': ParagraphStyle('header', fontName='AutoBold', fontSize=8, leading=11, textColor=colors.white),
    }
    def p(value, style='body'):
        return Paragraph(escape(str(value).replace('₽', 'руб.')).replace('\n', '<br/>'), styles[style])
    car, records = snapshot['car'], snapshot['records']
    story = [p('AutoHistory', 'title'), p('История обслуживания автомобиля', 'small'), Spacer(1, 18),
             p(f'{car["brand"]} {car["model"]} · {car["year"]}', 'heading'),
             p(f'Пробег: {car["odometer_km"]:,} км. Показание от {car["odometer_as_of"]:%d.%m.%Y}'.replace(',', ' ')),
             p(f'Дата выгрузки: {snapshot["exported_at"]:%d.%m.%Y} · Записей: {len(records)}', 'small'), Spacer(1, 16), p(DISCLAIMER, 'small'), Spacer(1, 18)]
    rows = [[p(x, 'header') for x in ('Дата', 'Пробег, км', 'Работы / примечание', 'Стоимость', 'СТО')]]
    for r in records:
        description = work_name(r)
        if r.get('note'):
            description += '\nПримечание: ' + r['note']
        rows.append([p(f'{r["service_date"]:%d.%m.%Y}'), p(f'{r["odometer_km"]:,}'.replace(',', ' ')), p(description), p(cost(r['cost_rub'])), p(r.get('station') or 'Не указано')])
    table = LongTable(rows, colWidths=[65, 62, 178, 82, 124], repeatRows=1, splitInRow=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#164c5d')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f5f7')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor('#164c5d')),
    ]))
    story += [table, Spacer(1, 16), p('Известные расходы: ' + cost(sum((r['cost_rub'] for r in records if r['cost_rub'] is not None), Decimal('0'))), 'heading'), p(f'Записей без стоимости: {sum(r["cost_rub"] is None for r in records)}', 'small')]
    def footer(canvas, doc):
        canvas.setFont('Auto', 8)
        canvas.setFillColor(colors.HexColor('#566b7b'))
        canvas.drawString(42, 25, 'AutoHistory · Сведения внесены владельцем')
        canvas.drawRightString(A4[0]-42, 25, str(doc.page))
    SimpleDocTemplate(str(path), pagesize=A4, rightMargin=42, leftMargin=42, topMargin=38, bottomMargin=45, title='AutoHistory — история обслуживания', author='AutoHistory').build(story, onFirstPage=footer, onLaterPages=footer)


class Reports:
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()
        self.directory = Path(service.config.pdf_tmp_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def cleanup(self):
        for path in self.directory.glob('report-*.pdf'):
            if path.stat().st_mtime < time.time()-3600:
                path.unlink(missing_ok=True)

    async def send(self, ctx, car_id, send_document, progress):
        if self.lock.locked():
            raise AppError('BUSY', 'Отчёт формируется. Попробуйте через минуту')
        async with self.lock:
            path = self.directory / f'report-{uuid4()}.pdf'
            task = None
            try:
                snapshot = await self.service.report_snapshot(ctx, car_id)
                await progress('Формирую отчёт.\n' + DISCLAIMER)
                task = asyncio.create_task(asyncio.to_thread(render, snapshot, path))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15)
                except TimeoutError:
                    await progress('Обработка продолжается. Отчёт будет отправлен после завершения')
                    await asyncio.shield(task)
                if path.stat().st_size > self.service.config.pdf_max_bytes:
                    raise AppError('TEMPORARY_UNAVAILABLE', 'Отчёт превышает 10 МиБ. Обратитесь к администратору', retryable=True)
                model = re.sub(r'[^\w-]', '_', snapshot['car']['model'])[:40]
                filename = f'AutoHistory_{model}_{snapshot["exported_at"]:%Y%m%d}.pdf'
                # Recheck access after potentially slow generation.
                await self.service.car_get(ctx, car_id)
                await send_document(path, filename)
            finally:
                try:
                    if task and not task.done():
                        # A worker thread cannot be cancelled; keep the slot until it ends.
                        await asyncio.shield(task)
                finally:
                    path.unlink(missing_ok=True)
