from datetime import datetime, timezone
from itertools import count

import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, SendPhoto, SendDocument, AnswerCallbackQuery
from aiogram.types import Message, Update

from app.bot import UI
from app.reports import Reports


class MemoryTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls=[]

    async def close(self):
        pass

    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method)
        if isinstance(method,(SendMessage,SendPhoto,SendDocument)):
            return Message(message_id=len(self.calls),date=datetime.now(timezone.utc),chat={'id':method.chat_id,'type':'private'},text=getattr(method,'text',None))
        return True

    async def stream_content(self,*args,**kwargs):
        yield b''


@pytest_asyncio.fixture
async def ui(env):
    s,ctx,car,now=env
    session=MemoryTelegram()
    bot=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=session)
    ui=UI(s,Reports(s))
    dp=Dispatcher()
    dp.include_router(ui.router)
    numbers=count(1)

    async def message(text,uid=111111,chat_type='private'):
        raw=dict(update_id=next(numbers),message=dict(message_id=next(numbers),date=now[0],chat=dict(id=uid,type=chat_type),from_user=dict(id=uid,is_bot=False,first_name='Тест'),text=text))
        await dp.feed_update(bot,Update.model_validate(raw))

    async def click(label,uid=111111,data=None):
        if data is None:
            last=next(c for c in reversed(session.calls) if isinstance(c,SendMessage) and c.reply_markup)
            data=next(b.callback_data for row in last.reply_markup.inline_keyboard for b in row if label in b.text)
        raw=dict(update_id=next(numbers),callback_query=dict(id=str(next(numbers)),from_user=dict(id=uid,is_bot=False,first_name='Тест'),chat_instance='test',data=data,message=dict(message_id=next(numbers),date=now[0],chat=dict(id=uid,type='private'))))
        await dp.feed_update(bot,Update.model_validate(raw))
        return data

    yield ui,session,message,click
    await bot.session.close()


def texts(session):
    return '\n'.join(c.text for c in session.calls if isinstance(c,SendMessage))


async def test_onboarding_timezone_confirmation_and_car(ui,env):
    ui,session,message,click=ui
    s,_,_,_=env
    await message('/start',333333)
    await click('Новосибирск',333333)
    assert await s.user_get(333333) is None
    await click('Сохранить',333333)
    for value in ['Toyota','Camry','2022','15000']:
        await message(value,333333)
    data=await click('Сохранить',333333)
    assert 'Сохранено' in texts(session)
    await click('',333333,data=data)
    assert 'Повторная запись не создана' in texts(session)


async def test_record_dialog_back_cancel_replay_and_pdf(ui,env):
    ui,session,message,click=ui
    s,ctx,car,_=env
    await message('/start')
    await click('Веста')
    await click('Добавить запись')
    await message('31.02.2026')
    assert 'Такой даты не существует' in texts(session)
    await message('20.09.2026')
    await message('10000')
    await click('Замена масла')
    await message('4500,50')
    await click('Назад')
    await message('4500,00')
    for _ in range(3):
        await click('Пропустить')
    data=await click('Сохранить')
    await click('',data=data)
    result=await s.record_list(ctx,car['id'])
    assert len(result['items'])==1
    assert str(result['items'][0]['cost_rub'])=='4500.00'
    await click('PDF отчёт')
    assert any(isinstance(c,SendDocument) for c in session.calls)
    assert not list(ui.reports.directory.glob('*.pdf'))
    await click('Добавить запись')
    await message('/cancel')
    assert len((await s.record_list(ctx,car['id']))['items'])==1


async def test_group_and_stale_callbacks(ui,env):
    ui,session,message,click=ui
    await message('/start',chat_type='group')
    assert 'Откройте личный чат' in texts(session)
    assert 111111 not in ui.sessions
    await message('/start')
    last=next(c for c in reversed(session.calls) if isinstance(c,SendMessage) and c.reply_markup)
    old=last.reply_markup.inline_keyboard[0][0].callback_data
    await click('Настройки')
    await click('',data=old)
    assert 'действие устарело' in texts(session)


async def test_plan_dialog_and_disable(ui,env):
    ui,session,message,click=ui
    s,ctx,car,_=env
    await message('/start')
    await click('Веста')
    await click('Напоминания')
    await click('Добавить')
    await click('Замена масла')
    await message('10000')
    await message('6')
    await message('31.01.2026')
    await message('0')
    await click('Сохранить')
    assert len(await s.plan_list(ctx,car['id']))==1
    await click('Напоминания')
    await click('Замена масла')
    await click('Отключить')
    await click('Подтвердить')
    assert not (await s.plan_list(ctx,car['id']))[0]['active']
