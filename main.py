import asyncio
import os
import sqlite3
import threading
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from crypto_pay_api import CryptoPayAsync, CryptoPayConfig

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN")
CARD = os.getenv("CARD_NUMBER")
RATE = float(os.getenv("RATE", "0.6"))
PORT = int(os.getenv("PORT", 10000))

router = Router()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- База данных ----------
conn = sqlite3.connect("orders.db", check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, gold INTEGER, rub REAL,
    method TEXT, invoice_id TEXT, status TEXT DEFAULT 'pending'
)""")
conn.commit()

# ---------- Состояния ----------
class Buy(StatesGroup):
    gold = State()
    method = State()
    screenshot = State()

# ---------- Health Check (для Render) ----------
async def health_handler(request):
    return web.Response(text="OK")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ---------- Пользовательские команды ----------
@router.message(Command("start"))
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Купить голду", callback_data="buy")],
        [InlineKeyboardButton(text="📋 Мои заказы", callback_data="my")],
    ])
    await m.answer(
        f"🔥 Текущий курс: {RATE} руб/голда\n"
        f"Минимум: 100 голды\n"
        f"После оплаты выкупаю предмет на рынке.",
        reply_markup=kb
    )

@router.callback_query(F.data == "buy")
async def buy_start(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Введите количество голды (целое число):")
    await state.set_state(Buy.gold)
    await c.answer()

@router.message(Buy.gold)
async def buy_gold(m: Message, state: FSMContext):
    try:
        gold = int(m.text)
        assert gold >= 100
    except:
        await m.answer("Введите целое число от 100:")
        return
    rub = round(gold * RATE, 2)
    await state.update_data(gold=gold, rub=rub)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 CryptoBot (USDT)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="💳 Карта", callback_data="pay_card")],
    ])
    await m.answer(f"К оплате: {rub} руб.\nВыберите способ:", reply_markup=kb)
    await state.set_state(Buy.method)

# ---------- CryptoBot 自动支付 ----------
@router.callback_query(F.data == "pay_crypto")
async def pay_crypto(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cfg = CryptoPayConfig(token=CRYPTO_TOKEN)
    async with CryptoPayAsync(cfg) as cp:
        inv = await cp.create_invoice(
            asset="USDT", amount=round(data["rub"] / 90, 2),
            description=f"Standoff2 Gold {data['gold']} | user:{c.from_user.id}",
            payload=str(c.from_user.id)
        )
    conn.execute("INSERT INTO orders(user_id,gold,rub,method,invoice_id) VALUES(?,?,?,?,?)",
                 (c.from_user.id, data["gold"], data["rub"], "crypto", str(inv["invoice_id"])))
    conn.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_{inv['invoice_id']}")],
    ])
    await c.message.answer(f"Оплатите по ссылке:\n{inv['pay_url']}", reply_markup=kb)
    await state.clear()
    await c.answer()

@router.callback_query(F.data.startswith("check_"))
async def check_crypto(c: CallbackQuery):
    inv_id = int(c.data.split("_")[1])
    cfg = CryptoPayConfig(token=CRYPTO_TOKEN)
    async with CryptoPayAsync(cfg) as cp:
        invs = await cp.get_invoices(invoice_ids=[inv_id])
    if not invs or invs[0]["status"] != "paid":
        await c.answer("❌ Платёж не найден или ещё не оплачен.", show_alert=True)
        return
    conn.execute("UPDATE orders SET status='paid' WHERE invoice_id=?", (str(inv_id),))
    conn.commit()
    await c.message.answer("✅ Оплата получена! Админ уведомлён, скоро выкуплю предмет.")
    await bot.send_message(ADMIN_ID, f"💰 Оплата CryptoBot!\nUser: {c.from_user.id}\nID счёта: {inv_id}\nПроверьте и выкупите предмет.")
    await c.answer()

# ---------- Оплата картой (ручная) ----------
@router.callback_query(F.data == "pay_card")
async def pay_card(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    conn.execute("INSERT INTO orders(user_id,gold,rub,method) VALUES(?,?,?,?)",
                 (c.from_user.id, data["gold"], data["rub"], "card"))
    conn.commit()
    await c.message.answer(
        f"Переведите {data['rub']} руб. на карту:\n`{CARD}`\n\n"
        f"После перевода отправьте скриншот чека сюда.",
        parse_mode="Markdown"
    )
    await state.set_state(Buy.screenshot)
    await c.answer()

@router.message(Buy.screenshot, F.photo)
async def card_screenshot(m: Message, state: FSMContext):
    data = await state.get_data()
    photo_id = m.photo[-1].file_id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ok_{m.from_user.id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"no_{m.from_user.id}")],
    ])
    await bot.send_photo(ADMIN_ID, photo_id,
        caption=f"💳 Оплата картой\nUser: {m.from_user.id}\nГолда: {data['gold']}\nСумма: {data['rub']} руб.",
        reply_markup=kb)
    await m.answer("Скриншот отправлен админу. Ожидайте подтверждения.")
    await state.clear()

@router.callback_query(F.data.startswith("ok_"))
async def admin_ok(c: CallbackQuery):
    uid = int(c.data.split("_")[1])
    conn.execute("UPDATE orders SET status='paid' WHERE user_id=? AND status='pending' AND method='card'", (uid,))
    conn.commit()
    await bot.send_message(uid, "✅ Оплата подтверждена! Выкупаю предмет.")
    await c.message.edit_caption(c.message.caption + "\n\n✅ Подтверждено")
    await c.answer()

@router.callback_query(F.data.startswith("no_"))
async def admin_no(c: CallbackQuery):
    uid = int(c.data.split("_")[1])
    await bot.send_message(uid, "❌ Оплата отклонена. Свяжитесь с админом.")
    await c.message.edit_caption(c.message.caption + "\n\n❌ Отклонено")
    await c.answer()

# ---------- Мои заказы ----------
@router.callback_query(F.data == "my")
async def my_orders(c: CallbackQuery):
    rows = conn.execute("SELECT id,gold,rub,method,status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (c.from_user.id,)).fetchall()
    if not rows:
        await c.message.answer("Заказов нет.")
        return
    text = "\n".join([f"#{r[0]} | {r[1]}g | {r[2]}₽ | {r[3]} | {r[4]}" for r in rows])
    await c.message.answer(f"Последние заказы:\n{text}")
    await c.answer()

# ---------- Запуск ----------
async def main():
    # Запуск health-сервера для Render (в фоне, не блокирует бота)
    asyncio.create_task(start_health_server())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())