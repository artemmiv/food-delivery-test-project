import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

from zoneinfo import ZoneInfo
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from dotenv import load_dotenv

# Загрузка переменных окружения из .env (если есть)
load_dotenv()

# ========================
# 1. НАСТРОЙКА ЛОГГИРОВАНИЯ И ОКРУЖЕНИЯ
# ========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/drainbot")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TZ = ZoneInfo("Europe/Moscow")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в окружении")

# ========================
# 2. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ
# ========================
client: AsyncIOMotorClient | None = None
mongo = None
users_col = None
cars_col = None
orders_col = None


async def init_db():
    global client, mongo, users_col, cars_col, orders_col
    client = AsyncIOMotorClient(MONGO_URI)
    mongo = client.get_default_database()
    users_col = mongo.get_collection("users")
    cars_col = mongo.get_collection("cars")
    orders_col = mongo.get_collection("orders")

    # Индексы
    await users_col.create_index("telegramId", unique=True)
    await cars_col.create_index("licensePlate", unique=True)
    await orders_col.create_index([("car", 1), ("datetime", 1)])


# ========================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ========================
MONTHS_SHORT_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def is_admin(chat_id: int) -> bool:
    return str(chat_id) in ADMIN_IDS


def format_date(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def format_hour_slot_label(hour: int) -> str:
    return f"{hour:02d}:00-{(hour + 1):02d}:00"


def reply_user_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["🚗 Записаться на откачку"], ["📋 Мои заказы"], ["✏️ Редактировать профиль"]],
        resize_keyboard=True,
    )


def reply_admin_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🚗 Управление автомобилями", "📋 Управление заказами"],
            ["👥 Управление клиентами", "📢 Рассылка"],
            ["📊 Статистика"],
        ],
        resize_keyboard=True,
    )


async def get_available_hours(car_id: ObjectId, selected_date: datetime, working_hours: Dict[str, int]) -> List[int]:
    start_of_day = datetime(selected_date.year, selected_date.month, selected_date.day, 0, 0, 0, tzinfo=TZ)
    end_of_day = datetime(selected_date.year, selected_date.month, selected_date.day, 23, 59, 59, tzinfo=TZ)

    existing = orders_col.find(
        {
            "car": car_id,
            "datetime": {"$gte": start_of_day, "$lte": end_of_day},
            "status": {"$in": ["new", "confirmed"]},
        }
    )
    booked: List[int] = [o["datetime"].astimezone(TZ).hour async for o in existing]

    hours: List[int] = []
    for hour in range(int(working_hours["start"]), int(working_hours["end"])):
        if hour not in booked:
            hours.append(hour)
    return hours


# ========================
# 4. СОСТОЯНИЯ ДЛЯ ДИАЛОГОВ
# ========================
profile_state: Dict[int, Dict[str, Any]] = {}
admin_state: Dict[int, Dict[str, Any]] = {}


# ========================
# 5. ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        user = await users_col.find_one({"telegramId": chat_id})
        if not user:
            await users_col.insert_one({
                "telegramId": chat_id,
                "name": None,
                "phone": None,
                "address": None,
                "createdAt": datetime.now(tz=TZ),
            })
            await context.bot.send_message(chat_id, "Добро пожаловать! Заполните ваш профиль:")
            profile_state[chat_id] = {"step": "name"}
            await context.bot.send_message(chat_id, "Введите ваше ФИО:")
            return

        menu = reply_admin_main_menu() if is_admin(chat_id) else reply_user_main_menu()
        await context.bot.send_message(chat_id, "Главное меню:", reply_markup=menu)
    except Exception as e:
        logger.exception("Start error")
        await context.bot.send_message(chat_id, "⚠️ Произошла ошибка. Попробуйте позже.")


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    # Профиль пользователя (пошагово)
    if chat_id in profile_state:
        try:
            step = profile_state[chat_id].get("step")
            if step == "name":
                name = text.strip()
                await users_col.update_one({"telegramId": chat_id}, {"$set": {"name": name}})
                profile_state[chat_id]["step"] = "phone"
                await update.message.reply_text("Введите ваш телефон в формате +79991234567:")
                return
            elif step == "phone":
                phone = text.strip()
                if not re.match(r"^\+?[0-9]{10,15}$", phone):
                    await update.message.reply_text("Неверный формат телефона. Пример: +79991234567")
                    return
                await users_col.update_one({"telegramId": chat_id}, {"$set": {"phone": phone}})
                profile_state[chat_id]["step"] = "address"
                await update.message.reply_text("Введите ваш адрес:")
                return
            elif step == "address":
                address = text.strip()
                await users_col.update_one({"telegramId": chat_id}, {"$set": {"address": address}})
                del profile_state[chat_id]
                menu = reply_admin_main_menu() if is_admin(chat_id) else reply_user_main_menu()
                await update.message.reply_text("✅ Профиль сохранен!", reply_markup=menu)
                return
        except Exception:
            logger.exception("Profile flow error")
            del profile_state[chat_id]
            await update.message.reply_text("⚠️ Ошибка сохранения профиля. Попробуйте /start")
            return

    # Админские пошаговые состояния
    if is_admin(chat_id) and chat_id in admin_state:
        try:
            state = admin_state[chat_id]
            if state.get("action") == "add_car":
                await add_car_step_handler(update, context, text)
                return
            if state.get("action") == "broadcast":
                await do_broadcast(chat_id, context, text)
                return
            if state.get("action") == "find_user":
                await admin_find_user_execute(chat_id, context, text)
                return
        except Exception:
            logger.exception("Admin state error")
            del admin_state[chat_id]
            await update.message.reply_text("⚠️ Ошибка при обработке данных")
            return

    # Обычные команды по тексту
    if text == "✏️ Редактировать профиль":
        profile_state[chat_id] = {"step": "name"}
        await update.message.reply_text("Введите ваше ФИО:")
        return

    if text == "🚗 Записаться на откачку":
        try:
            user = await users_col.find_one({"telegramId": chat_id})
            if not user or not user.get("name") or not user.get("phone") or not user.get("address"):
                await update.message.reply_text("Пожалуйста, сначала заполните ваш профиль")
                profile_state[chat_id] = {"step": "name"}
                await update.message.reply_text("Введите ваше ФИО:")
                return

            cars_cursor = cars_col.find({"isActive": True})
            cars = [c async for c in cars_cursor]
            if not cars:
                await update.message.reply_text("В настоящее время нет доступных автомобилей.")
                return

            buttons = [
                [InlineKeyboardButton(text=f"{c['brand']} ({c['licensePlate']})", callback_data=f"select_car_{c['_id']}")]
                for c in cars
            ]
            await update.message.reply_text("Выберите автомобиль:", reply_markup=InlineKeyboardMarkup(buttons))
            return
        except Exception:
            logger.exception("Schedule error")
            await update.message.reply_text("⚠️ Ошибка при загрузке автомобилей")
            return

    if text == "📋 Мои заказы":
        try:
            cursor = orders_col.find({"user": (await users_col.find_one({"telegramId": chat_id}))['_id']}).sort("createdAt", -1).limit(5)
            orders = [o async for o in cursor]
            if not orders:
                await update.message.reply_text("У вас пока нет заказов")
                return
            lines = []
            for o in orders:
                car = await cars_col.find_one({"_id": o["car"]})
                lines.append(
                    f"🆔 {o['_id']}\n" \
                    f"Статус: {o['status']}\n" \
                    f"📅 {format_date(o['datetime'])}\n" \
                    f"🚗 {car.get('brand','-')} ({car.get('licensePlate','-')})"
                )
            await update.message.reply_text("\n\n".join(lines))
            return
        except Exception:
            logger.exception("My orders error")
            await update.message.reply_text("⚠️ Ошибка при загрузке заказов")
            return

    if is_admin(chat_id):
        if text == "🚗 Управление автомобилями":
            await handle_car_management(update, context)
            return
        if text == "📋 Управление заказами":
            await handle_order_management(update, context)
            return
        if text == "👥 Управление клиентами":
            await handle_user_management(update, context)
            return
        if text == "📢 Рассылка":
            admin_state[chat_id] = {"action": "broadcast"}
            await update.message.reply_text("Введите сообщение для рассылки:")
            return
        if text == "📊 Статистика":
            users_count = await users_col.count_documents({})
            active_cars = await cars_col.count_documents({"isActive": True})
            new_orders_count = await orders_col.count_documents({"status": "new"})
            await update.message.reply_text(
                f"📊 Статистика системы:\n\n"
                f"👥 Всего пользователей: {users_count}\n"
                f"🚗 Активных автомобилей: {active_cars}\n"
                f"🆕 Новых заказов: {new_orders_count}"
            )
            return


# ========================
# 6. CALLBACK-ОБРАБОТЧИКИ
# ========================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is None:
        return
    query = update.callback_query
    chat_id = query.message.chat.id
    data = query.data or ""

    try:
        if data.startswith("select_car_"):
            car_id = data.split("_")[2]
            await show_year_selection(chat_id, context, car_id)
            await query.answer()
            return

        if data.startswith("book:" ):
            parts = data.split(":")  # book:<kind>:<carId>:<...>
            kind = parts[1]
            if kind == "y":
                car_id = parts[2]
                year = int(parts[3])
                await show_month_selection(chat_id, context, car_id, year)
            elif kind == "m":
                car_id = parts[2]
                year = int(parts[3])
                month = int(parts[4])
                await show_day_selection(chat_id, context, car_id, year, month)
            elif kind == "d":
                car_id = parts[2]
                year = int(parts[3])
                month = int(parts[4])
                day = int(parts[5])
                await show_time_slots(chat_id, context, car_id, year, month, day)
            elif kind == "t":
                car_id = parts[2]
                year = int(parts[3])
                month = int(parts[4])
                day = int(parts[5])
                hour = int(parts[6])
                dt = datetime(year, month, day, hour, 0, 0, tzinfo=TZ)
                await create_order(chat_id, context, car_id, dt)
            await query.answer()
            return

        if data.startswith("admin_"):
            if not is_admin(chat_id):
                await query.answer()
                return
            if data == "admin_add_car":
                await admin_begin_add_car(chat_id, context)
            elif data in ("admin_list_cars", "admin_toggle_car"):
                await send_cars_list(chat_id, context)
            elif data == "admin_new_orders":
                await send_orders_list(chat_id, context, kind="new")
            elif data == "admin_confirmed_orders":
                await send_orders_list(chat_id, context, kind="confirmed")
            elif data == "admin_all_orders":
                await send_orders_list(chat_id, context, kind="all")
            elif data == "admin_list_users":
                await send_users_list(chat_id, context)
            elif data == "admin_find_user":
                admin_state[chat_id] = {"action": "find_user"}
                await context.bot.send_message(chat_id, "Введите имя или телефон для поиска:")
            await query.answer()
            return

        if data == "noop":
            await query.answer()
            return

        if data.startswith("car_action_" ):
            parts = data.split("_")  # car_action_toggle_<id> | car_action_remove_<id>
            action = parts[2]
            car_id = parts[3]
            await car_action(chat_id, context, action, car_id)
            await query.answer()
            return

        if data.startswith("order_action_"):
            parts = data.split("_")  # order_action_confirm_<id> | order_action_cancel_<id>
            action = parts[2]
            order_id = parts[3]
            await order_action(chat_id, context, action, order_id)
            await query.answer()
            return

        await query.answer()
    except Exception:
        logger.exception("Callback processing error")
        try:
            await query.answer()
        except Exception:
            pass
        await context.bot.send_message(chat_id, "⚠️ Произошла ошибка при обработке запроса")


# ========================
# 7. КАЛЕНДАРЬ: ГОД → МЕСЯЦ → ДЕНЬ → ВРЕМЯ
# ========================
async def show_year_selection(chat_id: int, context: ContextTypes.DEFAULT_TYPE, car_id: str):
    now = datetime.now(tz=TZ)
    years = [now.year, now.year + 1]
    buttons = [[InlineKeyboardButton(text=str(y), callback_data=f"book:y:{car_id}:{y}") for y in years]]
    await context.bot.send_message(chat_id, "📅 Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))


async def show_month_selection(chat_id: int, context: ContextTypes.DEFAULT_TYPE, car_id: str, year: int):
    now = datetime.now(tz=TZ)
    current_year = now.year
    current_month = now.month

    months = []
    for m in range(1, 13):
        if year > current_year or (year == current_year and m >= current_month):
            months.append((m, MONTHS_SHORT_RU[m - 1]))

    if not months:
        await context.bot.send_message(chat_id, "🚫 Нет доступных месяцев в выбранном году")
        return

    rows = []
    for i in range(0, len(months), 3):
        row = [InlineKeyboardButton(text=label, callback_data=f"book:m:{car_id}:{year}:{m}") for m, label in months[i:i+3]]
        rows.append(row)

    await context.bot.send_message(chat_id, f"📅 Год {year}. Выберите месяц:", reply_markup=InlineKeyboardMarkup(rows))


async def show_day_selection(chat_id: int, context: ContextTypes.DEFAULT_TYPE, car_id: str, year: int, month: int):
    car = await cars_col.find_one({"_id": ObjectId(car_id)})
    if not car:
        await context.bot.send_message(chat_id, "🚫 Автомобиль не найден")
        return

    now = datetime.now(tz=TZ)
    days_in_month = (datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=TZ) - timedelta(days=1)).day

    allowed_days: List[int] = []
    for day in range(1, days_in_month + 1):
        d = datetime(year, month, day, tzinfo=TZ)
        if d.date() < now.date():
            continue
        if d.weekday() == 6:
            wd = 0  # В Python: Monday=0..Sunday=6; в исходнике 0=вск
        else:
            wd = d.weekday() + 1
        if wd in car["schedule"]["days"]:
            allowed_days.append(day)

    if not allowed_days:
        await context.bot.send_message(chat_id, "🚫 В этом месяце нет доступных дней для записи")
        return

    rows = []
    max_cols = 7
    for i in range(0, len(allowed_days), max_cols):
        row = [InlineKeyboardButton(text=str(day), callback_data=f"book:d:{car_id}:{year}:{month}:{day}") for day in allowed_days[i:i+max_cols]]
        rows.append(row)

    await context.bot.send_message(chat_id, f"📅 {MONTHS_SHORT_RU[month - 1]} {year}. Выберите день:", reply_markup=InlineKeyboardMarkup(rows))


async def show_time_slots(chat_id: int, context: ContextTypes.DEFAULT_TYPE, car_id: str, year: int, month: int, day: int):
    car = await cars_col.find_one({"_id": ObjectId(car_id)})
    if not car:
        await context.bot.send_message(chat_id, "🚫 Автомобиль не найден")
        return

    selected = datetime(year, month, day, tzinfo=TZ)
    available = await get_available_hours(ObjectId(car_id), selected, car["schedule"]["hours"])

    now = datetime.now(tz=TZ)
    filtered = [h for h in available if (selected.date() > now.date()) or (selected.date() == now.date() and h > now.hour)]

    if not filtered:
        await context.bot.send_message(chat_id, "🚫 Нет доступного времени для записи")
        return

    rows = []
    cols = 3
    for i in range(0, len(filtered), cols):
        row = [InlineKeyboardButton(text=format_hour_slot_label(h), callback_data=f"book:t:{car_id}:{year}:{month}:{day}:{h}") for h in filtered[i:i+cols]]
        rows.append(row)

    await context.bot.send_message(chat_id, "⏰ Выберите время:", reply_markup=InlineKeyboardMarkup(rows))


async def create_order(chat_id: int, context: ContextTypes.DEFAULT_TYPE, car_id: str, dt: datetime):
    user = await users_col.find_one({"telegramId": chat_id})
    car = await cars_col.find_one({"_id": ObjectId(car_id)})
    if not user or not car:
        await context.bot.send_message(chat_id, "🚫 Ошибка создания заказа. Данные не найдены.")
        return

    existing = await orders_col.find_one({
        "car": car["_id"],
        "datetime": dt,
        "status": {"$in": ["new", "confirmed"]},
    })
    if existing:
        await context.bot.send_message(chat_id, "⚠️ Это время уже занято, выберите другое")
        return

    order_doc = {
        "user": user["_id"],
        "car": car["_id"],
        "datetime": dt,
        "status": "new",
        "createdAt": datetime.now(tz=TZ),
    }
    result = await orders_col.insert_one(order_doc)

    await context.bot.send_message(
        chat_id,
        "✅ Заказ успешно создан!\n\n"
        f"📅 Дата: {format_date(dt)}\n"
        f"🚗 Автомобиль: {car['brand']} ({car['licensePlate']})\n\n"
        "Ожидайте подтверждения от администратора."
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                int(admin_id),
                "🆕 Новый заказ!\n\n"
                f"👤 Клиент: {user.get('name') or 'Не указано'}\n"
                f"📞 Телефон: {user.get('phone') or 'Не указан'}\n"
                f"🚗 Авто: {car['brand']} ({car['licensePlate']})\n"
                f"📅 Дата: {format_date(dt)}\n"
                f"🆔 ID: {result.inserted_id}",
            )
        except Exception:
            logger.exception("Не удалось уведомить администратора %s", admin_id)


# ========================
# 8. АДМИН: МЕНЮ И ДЕЙСТВИЯ
# ========================
async def handle_car_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="Добавить автомобиль", callback_data="admin_add_car")],
            [InlineKeyboardButton(text="Список автомобилей", callback_data="admin_list_cars")],
            [InlineKeyboardButton(text="Изменить статус", callback_data="admin_toggle_car")],
        ]
    )
    await update.message.reply_text("Выберите действие:", reply_markup=kb)


async def handle_order_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="Новые заказы", callback_data="admin_new_orders")],
            [InlineKeyboardButton(text="Подтвержденные", callback_data="admin_confirmed_orders")],
            [InlineKeyboardButton(text="Все заказы", callback_data="admin_all_orders")],
        ]
    )
    await update.message.reply_text("Выберите тип заказов:", reply_markup=kb)


async def handle_user_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="Список клиентов", callback_data="admin_list_users")],
            [InlineKeyboardButton(text="Поиск клиента", callback_data="admin_find_user")],
        ]
    )
    await update.message.reply_text("Выберите действие:", reply_markup=kb)


async def send_cars_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    cursor = cars_col.find({}).sort("brand", 1)
    cars = [c async for c in cursor]
    if not cars:
        await context.bot.send_message(chat_id, "Список автомобилей пуст")
        return

    rows = []
    for c in cars:
        rows.append([
            InlineKeyboardButton(text=f"{c['brand']} ({c['licensePlate']}) {'✅' if c.get('isActive', True) else '❌'}", callback_data="noop"),
            InlineKeyboardButton(text=("Выключить" if c.get("isActive", True) else "Включить"), callback_data=f"car_action_toggle_{c['_id']}")
        ])
        rows.append([
            InlineKeyboardButton(text="Удалить", callback_data=f"car_action_remove_{c['_id']}")
        ])
    await context.bot.send_message(chat_id, "🚗 Автомобили:", reply_markup=InlineKeyboardMarkup(rows))


async def car_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE, action: str, car_id: str):
    car = await cars_col.find_one({"_id": ObjectId(car_id)})
    if not car:
        await context.bot.send_message(chat_id, "🚫 Автомобиль не найден")
        return

    if action == "toggle":
        new_state = not car.get("isActive", True)
        await cars_col.update_one({"_id": car["_id"]}, {"$set": {"isActive": new_state}})
        await context.bot.send_message(
            chat_id,
            "✅ Статус автомобиля обновлен:\n"
            f"Марка: {car['brand']}\n"
            f"Гос. номер: {car['licensePlate']}\n"
            f"Новый статус: {'✅ Активен' if new_state else '❌ Неактивен'}",
        )
    elif action == "remove":
        await cars_col.delete_one({"_id": car["_id"]})
        await context.bot.send_message(
            chat_id,
            "✅ Автомобиль удален:\n"
            f"Марка: {car['brand']}\n"
            f"Гос. номер: {car['licensePlate']}",
        )

    await send_cars_list(chat_id, context)


async def send_orders_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE, kind: str):
    q: Dict[str, Any] = {}
    if kind in ("new", "confirmed"):
        q["status"] = kind
    cursor = orders_col.find(q).sort("createdAt", -1).limit(10)
    orders = [o async for o in cursor]

    if not orders:
        await context.bot.send_message(chat_id, "Список пуст")
        return

    for o in orders:
        user = await users_col.find_one({"_id": o["user"]})
        car = await cars_col.find_one({"_id": o["car"]})
        text = (
            f"🆔 {o['_id']}\n"
            f"Статус: {o['status']}\n"
            f"📅 {format_date(o['datetime'])}\n"
            f"🚗 {car.get('brand','-')} ({car.get('licensePlate','-')})\n"
            f"👤 {user.get('name','-')}\n"
            f"📞 {user.get('phone','-')}"
        )
        buttons: List[InlineKeyboardButton] = []
        if o["status"] == "new":
            buttons.append(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"order_action_confirm_{o['_id']}"))
        if o["status"] in ("new", "confirmed"):
            buttons.append(InlineKeyboardButton(text="❌ Отменить", callback_data=f"order_action_cancel_{o['_id']}"))
        await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None)


async def order_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE, action: str, order_id: str):
    o = await orders_col.find_one({"_id": ObjectId(order_id)})
    if not o:
        await context.bot.send_message(chat_id, "🚫 Заказ не найден")
        return
    user = await users_col.find_one({"_id": o["user"]})
    car = await cars_col.find_one({"_id": o["car"]})

    if action == "confirm":
        await orders_col.update_one({"_id": o["_id"]}, {"$set": {"status": "confirmed"}})
        await context.bot.send_message(
            chat_id,
            "✅ Заказ подтвержден:\n\n"
            f"🆔 ID: {o['_id']}\n"
            f"👤 Клиент: {user.get('name','Не указано')}\n"
            f"📞 Телефон: {user.get('phone','Не указан')}\n"
            f"🚗 Авто: {car.get('brand')} ({car.get('licensePlate')})\n"
            f"📅 Дата: {format_date(o['datetime'])}"
        )
        try:
            await context.bot.send_message(
                user["telegramId"],
                "✅ Ваш заказ подтвержден!\n\n"
                f"📅 Дата: {format_date(o['datetime'])}\n"
                f"🚗 Автомобиль: {car.get('brand')} ({car.get('licensePlate')})\n\n"
                "Ожидаем вас в указанное время!",
            )
        except Exception:
            logger.exception("Не удалось уведомить пользователя %s", user.get("telegramId"))
    elif action == "cancel":
        cancel_reason = "Отменен администратором"
        await orders_col.update_one({"_id": o["_id"]}, {"$set": {"status": "canceled", "cancelReason": cancel_reason}})
        await context.bot.send_message(
            chat_id,
            "✅ Заказ отменен:\n\n"
            f"🆔 ID: {o['_id']}\n"
            f"👤 Клиент: {user.get('name','Не указано')}\n"
            f"📞 Телефон: {user.get('phone','Не указан')}\n"
            f"🚗 Авто: {car.get('brand')} ({car.get('licensePlate')})\n"
            f"📅 Дата: {format_date(o['datetime'])}\n"
            f"❗ Причина: {cancel_reason}"
        )
        try:
            await context.bot.send_message(
                user["telegramId"],
                "❌ Ваш заказ отменен\n\n"
                f"📅 Дата: {format_date(o['datetime'])}\n"
                f"🚗 Автомобиль: {car.get('brand')} ({car.get('licensePlate')})\n\n"
                f"❗ Причина: {cancel_reason}\n\n"
                "Вы можете создать новый заказ.",
            )
        except Exception:
            logger.exception("Не удалось уведомить пользователя %s", user.get("telegramId"))


# ========================
# 9. АДМИН: ДОБАВЛЕНИЕ АВТО И ДРУГОЕ
# ========================
async def admin_begin_add_car(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    admin_state[chat_id] = {
        "action": "add_car",
        "step": 0,
        "temp": {},
        "steps": [
            {"q": "Введите марку автомобиля:", "field": "brand"},
            {
                "q": "Введите гос. номер автомобиля:",
                "field": "licensePlate",
                "parser": lambda s: parse_license_plate(s),
            },
            {
                "q": "Введите объем цистерны (в куб.м):",
                "field": "capacity",
                "parser": lambda s: parse_float(s, "Объем"),
            },
            {
                "q": "Введите максимальную длину шлангов (в метрах):",
                "field": "hoseLength",
                "parser": lambda s: parse_float(s, "Длина"),
            },
            {
                "q": "Введите рабочие дни через запятую (0-воскресенье, 1-понедельник и т.д.):\nПример: 1,2,3,4,5",
                "field": "days",
                "parser": lambda s: parse_days(s),
            },
            {
                "q": "Введите часы работы через пробел (начало и конец):\nПример: 8 20",
                "field": "hours",
                "parser": lambda s: parse_hours(s),
            },
        ],
    }
    await context.bot.send_message(chat_id, admin_state[chat_id]["steps"][0]["q"])


def parse_license_plate(s: str) -> str:
    license_plate = s.strip().upper()
    if not re.match(r"^[А-ЯЁ]{1}\d{3}[А-ЯЁ]{2}$", license_plate):
        raise ValueError("Неверный формат номера. Пример: А123БВ")
    return license_plate


def parse_float(s: str, field: str) -> float:
    try:
        return float(str(s).replace(",", ".").strip())
    except Exception:
        raise ValueError(f"Некорректное число для поля: {field}")


def parse_days(s: str) -> List[int]:
    try:
        days = [int(x.strip()) for x in s.split(",") if x.strip() != ""]
        if any(d < 0 or d > 6 for d in days):
            raise ValueError
        return days
    except Exception:
        raise ValueError("Некорректные дни недели. Используйте формат: 1,2,3,4,5")


def parse_hours(s: str) -> Dict[str, int]:
    parts = str(s).strip().split()
    if len(parts) != 2:
        raise ValueError("Некорректный формат часов работы. Например: 8 20")
    try:
        start = int(parts[0])
        end = int(parts[1])
    except Exception:
        raise ValueError("Часы работы должны быть целыми числами")
    if not (0 <= start <= 23 and 1 <= end <= 24) or start >= end:
        raise ValueError("Часы работы должны быть: начало 0-23, конец 1-24 и начало < конец")
    return {"start": start, "end": end}


async def add_car_step_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    state = admin_state[chat_id]
    current = state["steps"][state["step"]]
    try:
        value = text
        if current.get("parser"):
            value = current["parser"](text)
        state["temp"][current["field"]] = value
        if state["step"] < len(state["steps"]) - 1:
            state["step"] += 1
            await update.message.reply_text(state["steps"][state["step"]]["q"]) 
        else:
            # финальная проверка часов
            hours = state["temp"]["hours"]
            if hours["start"] >= hours["end"]:
                raise ValueError("Время начала должно быть меньше времени окончания")

            # проверка уникальности номера
            existing = await cars_col.find_one({"licensePlate": state["temp"]["licensePlate"]})
            if existing:
                state["step"] = 1
                await update.message.reply_text("❌ Автомобиль с таким номером уже существует\nВведите другой номер:")
                return

            car_doc = {
                "brand": state["temp"]["brand"],
                "licensePlate": state["temp"]["licensePlate"],
                "capacity": str(state["temp"]["capacity"]),
                "hoseLength": str(state["temp"]["hoseLength"]),
                "schedule": {"days": state["temp"]["days"], "hours": hours},
                "isActive": True,
            }
            await cars_col.insert_one(car_doc)

            msg = (
                "✅ Автомобиль успешно добавлен!\n\n"
                f"Марка: {car_doc['brand']}\n"
                f"Гос. номер: {car_doc['licensePlate']}\n"
                f"Объем: {car_doc['capacity']} куб.м\n"
                f"Длина шлангов: {car_doc['hoseLength']} м\n"
                f"Рабочие дни: {', '.join(str(d) for d in car_doc['schedule']['days'])}\n"
                f"Часы работы: {hours['start']}:00 - {hours['end']}:00"
            )
            del admin_state[chat_id]
            await update.message.reply_text(msg, reply_markup=reply_admin_main_menu())
    except ValueError as ve:
        await update.message.reply_text(str(ve))
    except Exception:
        logger.exception("Add car step error")
        del admin_state[chat_id]
        await update.message.reply_text("Произошла ошибка. Процесс добавления прерван. Начните заново.")


async def do_broadcast(chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str):
    if len(text) > 4096:
        await context.bot.send_message(chat_id, "⚠️ Сообщение слишком длинное (максимум 4096 символов)")
        return

    try:
        cursor = users_col.find({})
        success = 0
        failed: List[int] = []
        async for user in cursor:
            try:
                await context.bot.send_message(user["telegramId"], f"📢 Сообщение от администратора:\n\n{text}")
                success += 1
                await asyncio.sleep(0.1)
            except Exception:
                logger.exception("Не удалось отправить пользователю %s", user.get("telegramId"))
                failed.append(user.get("telegramId"))
        if chat_id in admin_state:
            del admin_state[chat_id]
        result = (
            "✅ Рассылка выполнена:\n"
            f"Доставлено: {success} из {success + len(failed)}\n"
        )
        if failed:
            result += f"Не удалось отправить: {len(failed)} пользователям"
            if len(failed) <= 10:
                result += f" (ID: {', '.join(str(x) for x in failed)})"
        await context.bot.send_message(chat_id, result, reply_markup=reply_admin_main_menu())
    except Exception:
        logger.exception("Broadcast error")
        await context.bot.send_message(chat_id, "⚠️ Ошибка при выполнении рассылки")
        if chat_id in admin_state:
            del admin_state[chat_id]


async def admin_find_user_execute(chat_id: int, context: ContextTypes.DEFAULT_TYPE, query: str):
    try:
        if re.match(r"^\+?\d{5,}$", query.strip()):
            cursor = users_col.find({"phone": {"$regex": re.escape(query.strip()), "$options": "i"}}).limit(10)
        else:
            cursor = users_col.find({"name": {"$regex": query.strip(), "$options": "i"}}).limit(10)
        users = [u async for u in cursor]
        if not users:
            await context.bot.send_message(chat_id, "Ничего не найдено")
        else:
            lines = [f"• {u.get('name','(без имени)')} — {u.get('phone','-')} — {u.get('address','-')}" for u in users]
            await context.bot.send_message(chat_id, "Результаты поиска:\n\n" + "\n".join(lines))
    except Exception:
        logger.exception("Find user error")
        await context.bot.send_message(chat_id, "Ошибка поиска")
    finally:
        if chat_id in admin_state:
            del admin_state[chat_id]


# ========================
# 10. ТОЧКА ВХОДА
# ========================
async def main():
    await init_db()

    application: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    logger.info("Бот запускается...")
    await application.initialize()
    try:
        await application.start()
        await application.updater.start_polling()
        # Ожидание до остановки (Ctrl+C)
        await application.updater.idle()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        if client:
            client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass