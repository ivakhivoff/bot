import asyncio
from datetime import datetime, timedelta
from itertools import cycle
import json
import logging
import os
from pathlib import Path
import pathlib
import threading
from logging.handlers import TimedRotatingFileHandler
import time
import traceback

from aiogram import Bot, Dispatcher, executor, types
from aiogram import types
import aiogram
from config import ADMINS, API_HASH, API_ID, BOT_TOKEN
from filters import Admin
from aiogram.dispatcher import FSMContext
import csv


from telethon import TelegramClient
from telethon.events import NewMessage
from aiogram.types import (
    InputFile,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ContentType,
)
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# from models.settings import Setting
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.date import DateTrigger
from aiogram.utils.exceptions import BotBlocked


def create_timed_rotating_log(path):
    logger = logging.getLogger("Rotating Log")
    logger.setLevel(logging.INFO)

    handler = TimedRotatingFileHandler(
        "logs/" + path, when="d", interval=1, backupCount=5
    )
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())

    return logger


chat_counter = {}

logger = create_timed_rotating_log("logs.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] {%(filename)s:%(funcName)s:%(lineno)d} %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

jobstores = {"default": SQLAlchemyJobStore(url="sqlite:///jobs.sqlite")}
scheduler = AsyncIOScheduler(jobstores=jobstores)
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)
dp.filters_factory.bind(Admin)


@dp.message_handler(commands=["id"])
async def get_id(message: types.Message):
    await message.answer(message.from_user.id)


@dp.message_handler(commands=["start"], state="*", is_admin=True)
async def get_id(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("Додати розсилку", callback_data="add_mailings"),
        InlineKeyboardButton("Акаунти", callback_data="accounts"),
    )
    await message.answer("Меню", reply_markup=kb)


class MailingStates(StatesGroup):
    msg = State()
    chats = State()
    mailtype = State()
    by_time = State()
    choose_acc = State()


@dp.callback_query_handler(text="add_mailings")
async def proccess_upd(call: CallbackQuery, state: FSMContext):
    await MailingStates.msg.set()
    await call.answer()
    await state.update_data(texts=[])
    await call.message.answer(
        "Відправте повідомлення для розсилки",
        reply_markup=get_cancel_kb(),
    )


from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import (
    ImportChatInviteRequest,
    GetHistoryRequest,
    CheckChatInviteRequest,
)
from telethon.tl.types import ChatInviteAlready
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
)


# @dp.message_handler(state=MailingStates.msg, content_types=["photo"])
# async def proccess_upd(message: Message, state: FSMContext):
#     await message.photo[-1].download(message.photo[-1])

#     async with state.proxy() as data:
#         data["texts"].append(message.text)

#     await MailingStates.by_time.set()
#     await message.answer("Введіть інтервал в форматі гг:хх:сс")


@dp.message_handler(state=MailingStates.msg)
async def proccess_upd(message: Message, state: FSMContext):
    async with state.proxy() as data:
        data["texts"].append(message.text)

    await MailingStates.by_time.set()
    await message.answer("Введіть інтервал в форматі гг:хх:сс")


from models.settings import Setting

from aiogram.utils.callback_data import CallbackData

cancel_mail_cb = CallbackData("cancel_mail_cb", "mail_id")


@dp.callback_query_handler(cancel_mail_cb.filter())
async def _make_mail(call: CallbackQuery, state: FSMContext, callback_data: dict):
    await call.answer()
    mail_id = callback_data["mail_id"]
    Setting.delete_by_id(mail_id)
    await call.message.answer("Успішно!")
    try:
        scheduler.remove_job(f"mailing_{mail_id}")
    except:
        logger.exception("error delete mailing")
        # await call.message.answer("Виникла помилка")


@dp.message_handler(state=MailingStates.by_time)
async def proccess_upd(message: Message, state: FSMContext):
    try:
        t = datetime.strptime(message.text, "%H:%M:%S")
    except:
        await message.answer("Неправильний формат")
        return
    await MailingStates.choose_acc.set()
    await state.update_data(time=t)
    files = os.listdir("accounts")
    text = "Пусто"
    if files:
        text = "\n".join(filter(lambda f: f.endswith(".session"), files))
    await message.answer(f"Введіть назву аккаунта\nАкаунти: {text}")


@dp.message_handler(state=MailingStates.choose_acc)
async def proccess_upd(message: Message, state: FSMContext):
    files = os.listdir("accounts")
    if message.text not in files:
        await message.answer("Акаунт не знайдено")
        return
    async with state.proxy() as data:
        s = Setting.create(
            chats="",
            texts="".join(data["texts"]),
            by_time=data["time"],
            chats_links=message.text,
        )

        scheduler.add_job(
            make_mail,
            args=("", s.id),
            id=f"mailing_{s.id}",
            trigger="interval",
            hours=data["time"].hour,
            minutes=data["time"].minute,
            seconds=data["time"].second,
        )
        await message.answer(
            f"Розсилка з айді {s.id}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton(
                    "Відмінити", callback_data=cancel_mail_cb.new(str(s.id))
                )
            ),
        )
    await state.finish()


def get_next_acc():
    i = 0
    while True:
        files = os.listdir("accounts")
        try:
            f = files[i]
        except IndexError:
            i = 0
            continue
        if not f.endswith(".session"):
            i += 1
            continue
        i += 1
        yield f


accs_gen = get_next_acc()

from telethon.types import Channel


async def make_mail(chat_ids, mail_id):
    s = Setting.get(id=mail_id)
    client = TelegramClient(
        "accounts/" + s.chats_links, api_hash=API_HASH, api_id=API_ID
    )
    await client.connect()
    if not await client.is_user_authorized():
        for adm in ADMINS:
            try:
                await bot.send_message(adm, f"Акаунт розлогінений: {s.chats_links}")
            except:
                pass
        return
    texts = s.texts
    s.save()
    try:
        async for dialog in client.iter_dialogs():
            if type(dialog.entity) != Channel:
                continue
            try:
                if not dialog.entity.megagroup:
                    continue
            except Exception:
                continue
            try:
                await client.send_message(dialog.entity, texts)
            except Exception as e:
                logger.info(f"channel {dialog.entity.id}, account {s.chats_links}")
                logger.exception("err", exc_info=True)
                continue
            await asyncio.sleep(1)
    finally:
        await client.disconnect()


class DeleteAcc(StatesGroup):
    name = State()


class AddAcc(StatesGroup):
    file = State()
    phone = State()
    code = State()
    password = State()


@dp.callback_query_handler(text="accounts")
async def proccess_upd(call: CallbackQuery):
    await call.answer()
    files = os.listdir("accounts")
    text = "Пусто"
    if files:
        text = ", ".join(filter(lambda f: f.endswith(".session"), files))
    kb = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("Додати аккаунт", callback_data="add_account"),
        InlineKeyboardButton("Видалити аккаунт", callback_data="delete_account"),
    )
    await call.message.answer(f"Акаунти: {text}", reply_markup=kb)


def get_cancel_kb():
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("Відмінити", callback_data="cancel")
    )


@dp.callback_query_handler(text="delete_account")
async def proccess_upd(call: CallbackQuery):
    await call.answer()
    await DeleteAcc.name.set()
    await call.message.answer("Відправте імя аккаунту", reply_markup=get_cancel_kb())


# @dp.callback_query_handler(text="add_account")
# async def proccess_upd(call: CallbackQuery):
#     await call.answer()
#     await AddAcc.file.set()
#     await call.message.answer("Відправте сесіон-аккаунт", reply_markup=get_cancel_kb())


@dp.callback_query_handler(text="add_account")
async def proccess_upd(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await AddAcc.file.set()
    await call.message.answer("Відправте номер", reply_markup=get_cancel_kb())


work_dir = pathlib.Path().resolve()


@dp.message_handler(state=AddAcc.file)
async def proccess_upd(message: Message, state: FSMContext):
    # global main_client
    # await state.finish()
    await AddAcc.phone.set()
    phone = message.text
    client = TelegramClient("accounts/" + phone, api_hash=API_HASH, api_id=API_ID)
    await client.connect()
    info = await client.send_code_request(phone)
    await client.disconnect()
    await state.update_data(c=phone, info=info.phone_code_hash)

    await message.answer(
        "Введіть код (між кожної цифрою крапка), а в наступному рядку пароль до акаунту, якщо він є"
    )


from telethon.errors.rpcerrorlist import SessionPasswordNeededError


@dp.message_handler(state=AddAcc.phone)
async def proccess_upd(message: Message, state: FSMContext):
    # global main_client
    # await state.finish()
    data = await state.get_data()
    phone = data["c"]
    client = TelegramClient("accounts/" + phone, api_hash=API_HASH, api_id=API_ID)
    await client.connect()

    await state.finish()
    code = message.text.split()
    password = None
    if len(code) > 1:
        password = code[1]
    code = "".join(code[0].split("."))
    try:
        try:
            await client.sign_in(phone, code, phone_code_hash=data["info"])
        except SessionPasswordNeededError:
            await client.sign_in(
                password=password,
            )
    except Exception as e:
        logging.exception("error")
    else:
        await message.answer("Успішно")
    finally:
        await client.disconnect()


@dp.callback_query_handler(text="cancel", state="*", is_admin=True)
async def proxys_get(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.finish()
    await call.message.answer("Відмінено")


@dp.message_handler(state=DeleteAcc.name)
async def proccess_upd(message: Message, state: FSMContext):
    await state.finish()
    os.remove("accounts/" + message.text)
    await message.answer("Успішно")


async def main():
    # await main2()
    scheduler.start()
    await dp.start_polling()


if __name__ == "__main__":
    asyncio.run(main())
