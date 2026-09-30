"""
Телеграм-бот "Магазин овощей и фруктов"
=========================================

Функции:
- Каталог товаров (из products.json)
- Корзина (добавление/удаление товаров)
- Оформление заказа (адрес доставки)
- Оплата через ЮKassa (YooKassa)
- Проверка статуса оплаты

Как запустить — см. README.md
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from yookassa import Configuration, Payment

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ (заполняется через переменные окружения — см. .env.example)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Смотрите .env.example")

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY
    PAYMENTS_ENABLED = True
else:
    PAYMENTS_ENABLED = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# КАТАЛОГ ТОВАРОВ
# ---------------------------------------------------------------------------

with open(os.path.join(os.path.dirname(__file__), "products.json"), encoding="utf-8") as f:
    PRODUCTS = {p["id"]: p for p in json.load(f)}


# ---------------------------------------------------------------------------
# ХРАНИЛИЩЕ КОРЗИН (в памяти; для продакшена лучше заменить на базу данных)
# ---------------------------------------------------------------------------

@dataclass
class Cart:
    items: dict = field(default_factory=dict)  # product_id -> количество

    def add(self, product_id: str, qty: int = 1):
        self.items[product_id] = self.items.get(product_id, 0) + qty

    def remove(self, product_id: str):
        if product_id in self.items:
            self.items[product_id] -= 1
            if self.items[product_id] <= 0:
                del self.items[product_id]

    def total(self) -> int:
        return sum(PRODUCTS[pid]["price"] * qty for pid, qty in self.items.items())

    def is_empty(self) -> bool:
        return len(self.items) == 0

    def clear(self):
        self.items.clear()


# user_id -> Cart
CARTS: dict[int, Cart] = {}
# payment_id -> user_id, для сопоставления оплаты с заказом
PENDING_PAYMENTS: dict[str, int] = {}


def get_cart(user_id: int) -> Cart:
    if user_id not in CARTS:
        CARTS[user_id] = Cart()
    return CARTS[user_id]


# ---------------------------------------------------------------------------
# СОСТОЯНИЯ (для сбора адреса доставки)
# ---------------------------------------------------------------------------

class OrderStates(StatesGroup):
    waiting_for_address = State()


# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------

def catalog_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in PRODUCTS.values():
        builder.button(
            text=f"{p['emoji']} {p['name']} — {p['price']}₽/{p['unit']}",
            callback_data=f"add:{p['id']}",
        )
    builder.button(text="🛒 Моя корзина", callback_data="cart")
    builder.adjust(1)
    return builder.as_markup()


def cart_keyboard(cart: Cart) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for pid, qty in cart.items.items():
        p = PRODUCTS[pid]
        builder.button(text=f"➖", callback_data=f"remove:{pid}")
        builder.button(text=f"{p['emoji']} {p['name']} x{qty}", callback_data="noop")
        builder.button(text=f"➕", callback_data=f"add:{pid}")
    builder.adjust(3)
    if not cart.is_empty():
        builder.row(InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад в каталог", callback_data="catalog"))
    return builder.as_markup()


def payment_check_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Проверить оплату", callback_data=f"check:{payment_id}")
    return builder.as_markup()


# ---------------------------------------------------------------------------
# ХЕНДЛЕРЫ
# ---------------------------------------------------------------------------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Добро пожаловать в магазин свежих овощей и фруктов! 🥒🍅🥭\n\n"
        "Выберите товар, чтобы добавить его в корзину:",
        reply_markup=catalog_keyboard(),
    )


@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    await message.answer("Каталог товаров:", reply_markup=catalog_keyboard())


@router.callback_query(F.data == "catalog")
async def show_catalog(callback: CallbackQuery):
    await callback.message.edit_text("Каталог товаров:", reply_markup=catalog_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("add:"))
async def add_to_cart(callback: CallbackQuery):
    product_id = callback.data.split(":")[1]
    cart = get_cart(callback.from_user.id)
    cart.add(product_id)
    await callback.answer(f"Добавлено: {PRODUCTS[product_id]['name']}")
    if callback.message.text and "Ваша корзина" in callback.message.text:
        await render_cart(callback)


@router.callback_query(F.data.startswith("remove:"))
async def remove_from_cart(callback: CallbackQuery):
    product_id = callback.data.split(":")[1]
    cart = get_cart(callback.from_user.id)
    cart.remove(product_id)
    await callback.answer("Убрано из корзины")
    await render_cart(callback)


@router.callback_query(F.data == "cart")
async def show_cart(callback: CallbackQuery):
    await render_cart(callback)
    await callback.answer()


async def render_cart(callback: CallbackQuery):
    cart = get_cart(callback.from_user.id)
    if cart.is_empty():
        text = "Ваша корзина пуста. Добавьте что-нибудь из каталога! 🛒"
    else:
        lines = ["Ваша корзина:\n"]
        for pid, qty in cart.items.items():
            p = PRODUCTS[pid]
            lines.append(f"{p['emoji']} {p['name']} x{qty} = {p['price'] * qty}₽")
        lines.append(f"\n💰 Итого: {cart.total()}₽")
        text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=cart_keyboard(cart))
    except Exception:
        pass


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "checkout")
async def start_checkout(callback: CallbackQuery, state: FSMContext):
    cart = get_cart(callback.from_user.id)
    if cart.is_empty():
        await callback.answer("Корзина пуста!", show_alert=True)
        return
    await callback.message.answer(
        "Укажите, пожалуйста, адрес доставки одним сообщением\n"
        "(город, улица, дом, квартира):"
    )
    await state.set_state(OrderStates.waiting_for_address)
    await callback.answer()


@router.message(OrderStates.waiting_for_address)
async def process_address(message: Message, state: FSMContext):
    address = message.text
    cart = get_cart(message.from_user.id)
    total = cart.total()

    if not PAYMENTS_ENABLED:
        order_summary = "\n".join(
            f"{PRODUCTS[pid]['emoji']} {PRODUCTS[pid]['name']} x{qty}"
            for pid, qty in cart.items.items()
        )
        await message.answer(
            f"✅ Заказ оформлен (демо-режим, оплата не подключена)!\n\n"
            f"{order_summary}\n\n"
            f"Сумма: {total}₽\n"
            f"Адрес доставки: {address}\n\n"
            f"Чтобы включить приём реальных платежей — заполните "
            f"YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в .env"
        )
        cart.clear()
        await state.clear()
        return

    idempotence_key = str(uuid.uuid4())
    payment = Payment.create(
        {
            "amount": {"value": f"{total}.00", "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/mandarin_dolka_bot",
            },
            "capture": True,
            "description": f"Заказ овощей/фруктов на сумму {total}₽",
            "metadata": {"telegram_user_id": message.from_user.id, "address": address},
        },
        idempotence_key,
    )

    PENDING_PAYMENTS[payment.id] = message.from_user.id

    await message.answer(
        f"Сумма к оплате: {total}₽\n"
        f"Адрес доставки: {address}\n\n"
        f"Оплатите по ссылке ниже, затем нажмите «Проверить оплату»:\n"
        f"{payment.confirmation.confirmation_url}",
        reply_markup=payment_check_keyboard(payment.id),
    )
    await state.clear()


@router.callback_query(F.data.startswith("check:"))
async def check_payment(callback: CallbackQuery):
    payment_id = callback.data.split(":", 1)[1]
    payment = Payment.find_one(payment_id)

    if payment.status == "succeeded":
        user_id = PENDING_PAYMENTS.get(payment_id, callback.from_user.id)
        cart = get_cart(user_id)
        cart.clear()
        await callback.message.edit_text(
            "✅ Оплата получена! Заказ передан в обработку.\n"
            "Спасибо за покупку 🥒🍅"
        )
    elif payment.status == "pending":
        await callback.answer("Оплата ещё не поступила. Попробуйте через минуту.", show_alert=True)
    else:
        await callback.answer(f"Статус платежа: {payment.status}", show_alert=True)


# ---------------------------------------------------------------------------
# ЗАПУСК БОТА
# ---------------------------------------------------------------------------

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен. Оплата через ЮKassa: %s", "включена" if PAYMENTS_ENABLED else "выключена (демо-режим)")

    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
