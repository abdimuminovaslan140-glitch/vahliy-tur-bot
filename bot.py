import asyncio
import calendar
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ============================================================
# SOZLAMALAR
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")            # @BotFather dan olingan token, .env faylida
ADMIN_ID = 1671888527                          # Asosiy admin (xabarnomalar shu ID ga boradi)

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi. Iltimos, .env fayliga BOT_TOKEN=... qatorini qo'shing."
    )
DB_PATH = os.getenv("DB_PATH", "vahliy_bot.db")
REMINDER_BEFORE_MINUTES = 5
PAGE_SIZE = 10
UZ_TZ = ZoneInfo("Asia/Tashkent")

# .env faylidagi ADMIN_IDS ro'yxati (masalan: ADMIN_IDS=111,222,333)
# Agar .env da ko'rsatilmagan bo'lsa, faqat yuqoridagi ADMIN_ID admin bo'ladi.
_env_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {
    int(x.strip()) for x in _env_admin_ids.split(",") if x.strip().isdigit()
}
if not ADMIN_IDS:
    ADMIN_IDS = {ADMIN_ID}
else:
    ADMIN_ID = next(iter(ADMIN_IDS))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=UZ_TZ)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# Qo'lda kiritilgan telefon raqamlarini tekshirish uchun (masalan: +998901234567,
# 998901234567, 90 123 45 67, (90) 123-45-67 va h.k.)
PHONE_RE = re.compile(r"^\+?\d[\d\s\-\(\)]{6,17}\d$")

# Buyurtma statuslari uchun o'zbekcha nomlar (ro'yxatlarda ko'rsatish uchun)
STATUS_NAMES = {
    "new": "🆕 Yangi",
    "awaiting_payment": "💳 To'lov kutilmoqda",
    "screenshot_sent": "📸 Chek yuborilgan",
    "accepted": "✅ Qabul qilingan",
    "rejected": "❌ Rad etilgan",
    "cancelled_expired": "⏰ Muddati o'tib bekor bo'lgan",
    "cancelled_user": "🚫 Mijoz bekor qilgan",
}

# Admin "Buyurtmalar ro'yxati" menyusidagi kategoriyalar (tartib shu bo'yicha chiqadi)
ORDER_CATEGORIES = [
    ("new", "🆕 Yangi buyurtmalar"),
    ("awaiting_payment", "💳 To'lov kutilayotganlar"),
    ("screenshot_sent", "📸 Chek yuborganlar"),
    ("accepted", "✅ Qabul qilinganlar"),
    ("rejected", "❌ Rad etilganlar"),
    ("cancelled_expired", "⏰ Muddati o'tib bekor bo'lganlar"),
    ("cancelled_user", "🚫 Mijoz bekor qilganlar"),
]


# ============================================================
# MA'LUMOTLAR BAZASI (SQLite)
# ============================================================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            full_name TEXT,
            phone TEXT,
            reason TEXT,
            time_str TEXT,
            date_str TEXT,
            status TEXT DEFAULT 'new',
            payment_method TEXT,
            reject_reason TEXT,
            reminder_msg_ids TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Botga /start bosgan barcha foydalanuvchilar shu jadvalga tushadi.
    # used_service = 0  -> shunchaki ko'rgan / qiziqqan
    # used_service = 1  -> xizmatdan foydalanish uchun buyurtma boshlagan
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            full_name TEXT,
            username TEXT,
            started_at TEXT,
            used_service INTEGER DEFAULT 0
        )
    """)

    # Eski bazalarda yangi ustun bo'lmasligi mumkin - xato chiqsa e'tiborsiz qoldiramiz.
    for alter_sql in (
        "ALTER TABLE orders ADD COLUMN reminder_msg_ids TEXT",
    ):
        try:
            cur.execute(alter_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def create_order(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (user_id, status, created_at) VALUES (?, 'new', ?)",
        (user_id, datetime.now().isoformat()),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


def update_order(order_id: int, **fields):
    if not fields:
        return
    conn = get_conn()
    cur = conn.cursor()
    keys = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [order_id]
    cur.execute(f"UPDATE orders SET {keys} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_order(order_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_orders_by_status(statuses):
    conn = get_conn()
    cur = conn.cursor()
    q_marks = ",".join("?" for _ in statuses)
    cur.execute(
        f"SELECT * FROM orders WHERE status IN ({q_marks}) ORDER BY id DESC",
        list(statuses),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def set_setting(key: str, value: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_setting(key: str, default=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def upsert_user(user_id: int, full_name: str, username: str | None):
    """/start bosgan har bir foydalanuvchini yozib/yangilab boradi."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, full_name, username, started_at, used_service) "
        "VALUES (?, ?, ?, ?, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name, "
        "username = excluded.username",
        (user_id, full_name, username, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def mark_user_used_service(user_id: int):
    """Foydalanuvchi xizmatdan foydalanish uchun buyurtma boshlaganda chaqiriladi."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET used_service = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def set_reminder_msg_id(order_id: int, admin_id: int, message_id: int):
    """5 daqiqalik eslatma har bir admin uchun qaysi xabarda yuborilganini saqlaydi
    (keyinchalik 'gaplashdim' bosilganda o'sha xabarga javob (reply) qilish uchun)."""
    order = get_order(order_id)
    if order is None:
        return
    try:
        data = json.loads(order["reminder_msg_ids"]) if order["reminder_msg_ids"] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    data[str(admin_id)] = message_id
    update_order(order_id, reminder_msg_ids=json.dumps(data))


def get_reminder_msg_id(order, admin_id: int):
    if order is None or not order["reminder_msg_ids"]:
        return None
    try:
        data = json.loads(order["reminder_msg_ids"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data.get(str(admin_id))


# ============================================================
# FSM HOLATLARI
# ============================================================

class OrderStates(StatesGroup):
    waiting_phone = State()
    waiting_name = State()
    waiting_reason = State()
    waiting_time = State()
    waiting_day = State()
    waiting_payment_method = State()
    waiting_paid_confirm = State()
    waiting_screenshot = State()


class AdminStates(StatesGroup):
    waiting_card_number = State()
    waiting_reject_reason = State()


# ============================================================
# KLAVIATURALAR VA KALENDAR
# ============================================================

MONTHS_UZ = {
    1: "Yanvar", 2: "Fevral", 3: "Mart", 4: "Aprel",
    5: "May", 6: "Iyun", 7: "Iyul", 8: "Avgust",
    9: "Sentyabr", 10: "Oktyabr", 11: "Noyabr", 12: "Dekabr",
}

MAIN_MENU_TEXT = "🌙 Vahliy uyg'otish xizmatidan foydalanmoqchiman"
BTN_SEND_PHONE = "📱 Raqamimni yuborish"
BTN_PAID = "✅ To'lov qildim"
BTN_CANCEL_ORDER = "🚫 Buyurtmani bekor qilish"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MAIN_MENU_TEXT)]],
        resize_keyboard=True,
    )


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SEND_PHONE, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def paid_confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PAID)],
            [KeyboardButton(text=BTN_CANCEL_ORDER)],
        ],
        resize_keyboard=True,
    )


def _chunk(items, size=4):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def day_only_kb(month_num: int, year: int) -> ReplyKeyboardMarkup:
    """
    Faqat berilgan (joriy) oy va yilga tegishli kunlar tugmalarini quradi
    (bugungi kundan oldingi kunlarsiz). Boshqa oylar hech qachon ko'rsatilmaydi.
    """
    today = datetime.now(UZ_TZ).date()
    days_in_month = calendar.monthrange(year, month_num)[1]
    start_day = today.day if (year == today.year and month_num == today.month) else 1
    day_buttons = [KeyboardButton(text=str(d)) for d in range(start_day, days_in_month + 1)]
    rows = list(_chunk(day_buttons, 5))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def payment_method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Payme", callback_data="pay_payme"),
            InlineKeyboardButton(text="💳 Click", callback_data="pay_click"),
        ]
    ])


def admin_decision_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Qabul qilaman", callback_data=f"accept_{order_id}"),
            InlineKeyboardButton(text="❌ Qabul qilmayman", callback_data=f"reject_{order_id}"),
        ]
    ])


def admin_reject_reason_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Sabab bor", callback_data=f"rejreason_yes_{order_id}"),
            InlineKeyboardButton(text="🚫 Sabab yo'q", callback_data=f"rejreason_no_{order_id}"),
        ]
    ])


def admin_talked_kb(order_id: int) -> InlineKeyboardMarkup:
    """Mijoz bilan gaplashish vaqti kelganda (uyg'otish vaqtining o'zida) chiqadigan tugma."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, gaplashdim", callback_data=f"talked_{order_id}")]
    ])


def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Karta raqamini o'rnatish")],
            [KeyboardButton(text="📋 Buyurtmalar ro'yxati")],
            [KeyboardButton(text="👥 Foydalanuvchilar ro'yxati")],
            [KeyboardButton(text="🚪 Admin paneldan chiqish")],
        ],
        resize_keyboard=True,
    )


def order_categories_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"ordercat|{status}")]
        for status, label in ORDER_CATEGORIES
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_categories_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Xizmatdan foydalanganlar", callback_data="usercat|used")],
        [InlineKeyboardButton(text="👀 Qiziqqanlar", callback_data="usercat|interested")],
    ])


def pagination_kb(prefix: str, key: str, page: int, total_pages: int):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Oldingi", callback_data=f"{prefix}|{key}|{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Keyingi ▶️", callback_data=f"{prefix}|{key}|{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[nav])


# ============================================================
# FOYDALANUVCHI OQIMI
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    # /start bosgan har bir kishi "qiziqqanlar" ro'yxatiga yoziladi.
    user = message.from_user
    upsert_user(user.id, user.full_name, user.username)

    await message.answer(
        "Assalomu alaykum! 🌙\n\n"
        "Vahliy uyg'otish xizmatiga xush kelibsiz.\n"
        "Xizmatdan foydalanish uchun quyidagi tugmani bosing 👇",
        reply_markup=main_menu_kb(),
    )


@dp.message(F.text == MAIN_MENU_TEXT)
async def start_order(message: Message, state: FSMContext):
    # Foydalanuvchi xizmatdan foydalanish uchun tugmani bosdi ->
    # endi u "foydalanuvchilar" ro'yxatiga o'tadi.
    mark_user_used_service(message.from_user.id)

    order_id = create_order(message.from_user.id)
    await state.update_data(order_id=order_id)
    await state.set_state(OrderStates.waiting_phone)
    await message.answer(
        "Telefon raqamingizni kiriting 👇\n"
        "Pastdagi tugma orqali o'zingizning raqamingizni yuborishingiz mumkin, "
        "yoki xohlagan raqamni qo'lda yozib yuborishingiz mumkin "
        "(masalan: +998901234567):",
        reply_markup=phone_kb(),
    )


@dp.message(OrderStates.waiting_phone, F.contact)
async def get_phone_contact(message: Message, state: FSMContext):
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("Iltimos, o'zingizning telefon raqamingizni yuboring.", reply_markup=phone_kb())
        return
    phone = message.contact.phone_number
    data = await state.get_data()
    update_order(data["order_id"], phone=phone)
    await state.set_state(OrderStates.waiting_name)
    await message.answer(
        "Rahmat! Endi ismingizni kiriting:",
        reply_markup=remove_kb(),
    )


@dp.message(OrderStates.waiting_phone, F.text)
async def get_phone_text(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not PHONE_RE.match(phone):
        await message.answer(
            "Noto'g'ri raqam formati. Iltimos, telefon raqamini to'g'ri "
            "kiriting (masalan: +998901234567) yoki pastdagi tugma orqali "
            "raqamingizni yuboring.",
            reply_markup=phone_kb(),
        )
        return

    data = await state.get_data()
    update_order(data["order_id"], phone=phone)
    await state.set_state(OrderStates.waiting_name)
    await message.answer(
        "Rahmat! Endi ismingizni kiriting:",
        reply_markup=remove_kb(),
    )


@dp.message(OrderStates.waiting_phone)
async def get_phone_wrong(message: Message):
    await message.answer(
        "Iltimos, telefon raqamingizni matn ko'rinishida yozing yoki "
        "pastdagi \"📱 Raqamimni yuborish\" tugmasi orqali yuboring.",
        reply_markup=phone_kb(),
    )


@dp.message(OrderStates.waiting_name)
async def get_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Iltimos, ismingizni kiriting:")
        return
    data = await state.get_data()
    update_order(data["order_id"], full_name=name)
    await state.update_data(full_name=name)
    await state.set_state(OrderStates.waiting_reason)
    await message.answer(
        f"Rahmat, {name}!\n\n"
        "Nima uchun uyg'otish xizmatidan foydalanmoqchisiz? "
        "Sababini yozing (masalan: ishga borish, imtihon, parvoz va h.k.):"
    )


@dp.message(OrderStates.waiting_reason)
async def get_reason(message: Message, state: FSMContext):
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Iltimos, sababni yozing:")
        return
    data = await state.get_data()
    update_order(data["order_id"], reason=reason)
    await state.set_state(OrderStates.waiting_time)
    await message.answer(
        "Soat nechida uyg'otish kerak?\n"
        "Formatda kiriting: SS:DD (masalan: 07:30)"
    )


@dp.message(OrderStates.waiting_time)
async def get_time(message: Message, state: FSMContext):
    text = message.text.strip()
    match = TIME_RE.match(text)
    if not match:
        await message.answer(
            "Noto'g'ri format. Iltimos, vaqtni SS:DD ko'rinishida kiriting "
            "(masalan: 07:30):"
        )
        return

    # Soat bir xonali kiritilgan bo'lsa ham (masalan "2:12"), doim ikki xonali
    # ko'rinishda ("02:12") saqlaymiz - aks holda sana bilan qo'shib parslashda xato chiqadi.
    hour_part, minute_part = match.groups()
    text = f"{int(hour_part):02d}:{minute_part}"

    data = await state.get_data()
    update_order(data["order_id"], time_str=text)

    # Faqat joriy oyning kunlari ko'rsatiladi (masalan hozir avgust bo'lsa,
    # faqat avgust kunlari chiqadi; sentyabr bo'lsa, faqat sentyabr kunlari).
    today = datetime.now(UZ_TZ).date()
    await state.update_data(selected_month=today.month, selected_year=today.year)
    await state.set_state(OrderStates.waiting_day)
    await message.answer(
        f"{MONTHS_UZ[today.month]} oyidan kunni tanlang 👇",
        reply_markup=day_only_kb(today.month, today.year),
    )


@dp.message(OrderStates.waiting_day)
async def get_day(message: Message, state: FSMContext):
    data = await state.get_data()
    today = datetime.now(UZ_TZ).date()
    month_num = data.get("selected_month") or today.month
    year = data.get("selected_year") or today.year

    text = message.text.strip()
    if not text.isdigit():
        await message.answer(
            "Iltimos, pastdagi tugmalardan kunni tanlang.",
            reply_markup=day_only_kb(month_num, year),
        )
        return

    day_num = int(text)
    try:
        selected = date(year, month_num, day_num)
    except ValueError:
        await message.answer(
            "Noto'g'ri kun. Iltimos, pastdagi tugmalardan qaytadan tanlang.",
            reply_markup=day_only_kb(month_num, year),
        )
        return

    if selected < date.today():
        await message.answer(
            "O'tib ketgan kunni tanlab bo'lmaydi. Iltimos, qaytadan tanlang.",
            reply_markup=day_only_kb(month_num, year),
        )
        return

    # Bugungi kun tanlansa, tanlangan vaqt allaqachon o'tib ketgan bo'lmasin.
    if selected == today:
        time_text = get_order(data["order_id"])["time_str"]
        target = _parse_dt(selected.isoformat(), time_text)
        if target <= datetime.now(UZ_TZ):
            await message.answer(
                "Bu vaqt allaqachon o'tib ketgan. Iltimos, kelajakdagi vaqtni kiriting.",
                reply_markup=day_only_kb(month_num, year),
            )
            return

    update_order(data["order_id"], date_str=selected.isoformat())
    await state.set_state(OrderStates.waiting_payment_method)
    await message.answer(
        "So'rovingiz qabul qilindi ✅\n\n"
        "Endi to'lovni amalga oshirish kerak. To'lov usulini tanlang:",
        reply_markup=remove_kb(),
    )
    await message.answer("To'lov usuli:", reply_markup=payment_method_kb())


@dp.callback_query(F.data.in_(["pay_payme", "pay_click"]))
async def choose_payment_method(callback: CallbackQuery, state: FSMContext):
    method = "Payme" if callback.data == "pay_payme" else "Click"
    data = await state.get_data()
    card_number = get_setting("card_number", "Karta raqami hali kiritilmagan")
    update_order(data["order_id"], payment_method=method, status="awaiting_payment")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"💳 {method} orqali to'lov qiling.\n\n"
        f"Karta raqami: <code>{card_number}</code>\n\n"
        "To'lovni amalga oshirgach, pastdagi tugmani bosing 👇\n"
        "Agar fikringizdan qaytsangiz, buyurtmani bekor qilishingiz ham mumkin.",
        parse_mode="HTML",
        reply_markup=paid_confirm_kb(),
    )
    await state.set_state(OrderStates.waiting_paid_confirm)
    await callback.answer()


@dp.message(OrderStates.waiting_paid_confirm, F.text == BTN_PAID)
async def paid_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    order = get_order(data.get("order_id"))
    if order is None:
        await state.clear()
        await message.answer(
            "Xatolik yuz berdi, iltimos qaytadan urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return

    # Agar mijoz belgilagan uyg'otish vaqti to'lov paytida allaqachon o'tib
    # ketgan bo'lsa - buyurtma avtomatik bekor qilinadi, adminga hech narsa
    # yuborilmaydi va mijoz "bekor qilinganlar" ro'yxatiga tushadi.
    target = _order_datetime(order)
    if target <= datetime.now(UZ_TZ):
        update_order(order["id"], status="cancelled_expired")
        await message.answer(
            "❌ Afsuski, belgilangan uyg'otish vaqti allaqachon o'tib ketgani "
            "sababli buyurtmangiz avtomatik ravishda bekor qilindi.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    await state.set_state(OrderStates.waiting_screenshot)
    await message.answer(
        "To'lov chekining skrinshotini (rasmini) yuboring 📸",
        reply_markup=remove_kb(),
    )


@dp.message(OrderStates.waiting_paid_confirm, F.text == BTN_CANCEL_ORDER)
async def cancel_order_by_user(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if order_id:
        update_order(order_id, status="cancelled_user")
    await message.answer(
        "🚫 Buyurtmangiz bekor qilindi.",
        reply_markup=main_menu_kb(),
    )
    await state.clear()


@dp.message(OrderStates.waiting_screenshot, F.photo)
async def get_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data["order_id"]
    order = get_order(order_id)
    update_order(order_id, status="screenshot_sent")

    caption = (
        f"🆕 Yangi to'lov cheki!\n\n"
        f"👤 Ism: {order['full_name']}\n"
        f"📞 Telefon: {order['phone']}\n"
        f"🎯 Sababi: {order['reason']}\n"
        f"🕐 Vaqti: {order['time_str']}\n"
        f"📅 Sanasi: {order['date_str']}\n"
        f"💳 To'lov usuli: {order['payment_method']}\n"
        f"🆔 Buyurtma raqami: {order_id}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=message.photo[-1].file_id,
                caption=caption,
                reply_markup=admin_decision_kb(order_id),
            )
        except Exception:
            logging.exception("Order #%s chekini admin %s ga yuborishda xato.", order_id, admin_id)
    await message.answer(
        "Chekingiz adminga yuborildi. Tez orada tasdiqlanadi, iltimos kuting ⏳",
        reply_markup=main_menu_kb(),
    )
    await state.clear()


@dp.message(OrderStates.waiting_screenshot)
async def get_screenshot_wrong(message: Message):
    await message.answer("Iltimos, chekning skrinshotini rasm sifatida yuboring 📸")


# ============================================================
# ADMIN QARORI: QABUL QILISH / RAD ETISH
# ============================================================

@dp.callback_query(F.data.startswith("accept_"))
async def admin_accept(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Sizda bu amal uchun ruxsat yo'q.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])
    order = get_order(order_id)
    if order is None:
        await callback.answer("Buyurtma topilmadi", show_alert=True)
        return

    update_order(order_id, status="accepted")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Buyurtma #{order_id} qabul qilindi.")

    await bot.send_message(
        chat_id=order["user_id"],
        text=(
            "✅ Bo'ldi! Sizning uyg'otish xizmatingiz qabul qilindi.\n\n"
            f"📅 Sana: {order['date_str']}\n"
            f"🕐 Vaqt: {order['time_str']}\n\n"
            "Belgilangan kuni va vaqtda albatta sizni uyg'otamiz."
        ),
    )

    schedule_reminder(order_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("reject_"))
async def admin_reject_ask_reason(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Sizda bu amal uchun ruxsat yo'q.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Rad etish sababini ko'rsatasizmi?",
        reply_markup=admin_reject_reason_kb(order_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rejreason_no_"))
async def admin_reject_no_reason(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Sizda bu amal uchun ruxsat yo'q.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])
    order = get_order(order_id)
    update_order(order_id, status="rejected")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"❌ Buyurtma #{order_id} rad etildi.")

    await bot.send_message(
        chat_id=order["user_id"],
        text="❌ Afsuski, buyurtmangiz qabul qilinmadi.",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rejreason_yes_"))
async def admin_reject_yes_reason(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Sizda bu amal uchun ruxsat yo'q.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])
    await state.update_data(reject_order_id=order_id)
    await state.set_state(AdminStates.waiting_reject_reason)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Rad etish sababini yozing:")
    await callback.answer()


@dp.message(AdminStates.waiting_reject_reason)
async def admin_reject_reason_text(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    data = await state.get_data()
    order_id = data["reject_order_id"]
    order = get_order(order_id)
    reason = message.text.strip()
    update_order(order_id, status="rejected", reject_reason=reason)

    await message.answer(f"❌ Buyurtma #{order_id} rad etildi. Sabab yuborildi.")

    await bot.send_message(
        chat_id=order["user_id"],
        text=f"❌ Afsuski, buyurtmangiz qabul qilinmadi.\nSabab: {reason}",
    )
    await state.clear()


# ============================================================
# ESLATMAGA "GAPLASHDIM" TASDIG'I
# ============================================================

@dp.callback_query(F.data.startswith("talked_"))
async def admin_talked(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Sizda bu amal uchun ruxsat yo'q.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])
    order = get_order(order_id)

    await callback.message.edit_reply_markup(reply_markup=None)

    reply_text = "✅ Bu mijoz bilan gaplashildi."
    reminder_msg_id = get_reminder_msg_id(order, callback.from_user.id)
    try:
        if reminder_msg_id:
            # 5 daqiqa oldin yuborilgan eslatma xabariga aynan javob (reply) qilib yuboriladi.
            await bot.send_message(
                chat_id=callback.message.chat.id,
                text=reply_text,
                reply_to_message_id=reminder_msg_id,
            )
        else:
            await callback.message.reply(reply_text)
    except Exception:
        logging.exception("Order #%s uchun 'gaplashildi' javobini yuborishda xato.", order_id)
        await callback.message.answer(reply_text)

    await callback.answer()


# ============================================================
# ADMIN PANEL
# ============================================================

@dp.message(Command("admin"))
async def admin_login_start(message: Message, state: FSMContext):
    if message.from_user.id in ADMIN_IDS:
        await state.clear()
        await message.answer(
            "✅ Admin panelga xush kelibsiz!",
            reply_markup=admin_menu_kb(),
        )
    else:
        await message.answer("❌ Sizda admin panelga kirish huquqi yo'q.")


@dp.message(F.text == "🚪 Admin paneldan chiqish")
async def admin_logout(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await message.answer("Admin paneldan chiqdingiz.", reply_markup=main_menu_kb())


@dp.message(F.text == "💳 Karta raqamini o'rnatish")
async def admin_set_card_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminStates.waiting_card_number)
    await message.answer(
        "Yangi karta raqamini kiriting (masalan: 8600 1234 5678 9012):",
        reply_markup=remove_kb(),
    )


@dp.message(AdminStates.waiting_card_number)
async def admin_set_card_save(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    card_number = message.text.strip()
    set_setting("card_number", card_number)
    await state.clear()
    await message.answer(
        f"✅ Karta raqami saqlandi: {card_number}",
        reply_markup=admin_menu_kb(),
    )


# ---------- Buyurtmalar ro'yxati: kategoriyalar + paginatsiya ----------

def format_order_line(o) -> str:
    return (
        f"#{o['id']} | {STATUS_NAMES.get(o['status'], o['status'])}\n"
        f"👤 {o['full_name'] or '-'} | 📞 {o['phone'] or '-'}\n"
        f"📅 {o['date_str'] or '-'} 🕐 {o['time_str'] or '-'}\n"
        f"🎯 {o['reason'] or '-'}\n"
    )


def format_user_line(u) -> str:
    uname = f"@{u['username']}" if u["username"] else "username yo'q"
    return f"👤 {u['full_name'] or '-'} ({uname}) | ID: {u['user_id']}"


async def render_orders_page(target_message, status: str, page: int, edit: bool):
    label = dict(ORDER_CATEGORIES).get(status, status)
    orders = get_orders_by_status([status])
    total = len(orders)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = max(0, min(page, total_pages - 1))
    chunk = orders[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    if not chunk:
        text = f"{label}\n\nHozircha bu ro'yxat bo'sh."
    else:
        text = f"{label} ({total} ta)\n\n" + "\n".join(format_order_line(o) for o in chunk)

    kb = pagination_kb("ordpage", status, page, total_pages)
    if edit:
        try:
            await target_message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await target_message.answer(text, reply_markup=kb)


async def render_users_page(target_message, utype: str, page: int, edit: bool):
    label = "✅ Xizmatdan foydalanganlar" if utype == "used" else "👀 Qiziqqanlar"
    all_users = get_all_users()
    if utype == "used":
        filtered = [u for u in all_users if u["used_service"]]
    else:
        filtered = [u for u in all_users if not u["used_service"]]

    total = len(filtered)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = max(0, min(page, total_pages - 1))
    chunk = filtered[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    if not chunk:
        text = f"{label}\n\nHozircha bu ro'yxat bo'sh."
    else:
        text = f"{label} ({total} ta)\n\n" + "\n".join(format_user_line(u) for u in chunk)

    kb = pagination_kb("usrpage", utype, page, total_pages)
    if edit:
        try:
            await target_message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await target_message.answer(text, reply_markup=kb)


@dp.message(F.text == "📋 Buyurtmalar ro'yxati")
async def admin_orders_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Qaysi ro'yxatni ko'rmoqchisiz?", reply_markup=order_categories_kb())


@dp.callback_query(F.data.startswith("ordercat|"))
async def show_order_category(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    status = callback.data.split("|", 1)[1]
    await render_orders_page(callback.message, status, 0, edit=False)
    await callback.answer()


@dp.callback_query(F.data.startswith("ordpage|"))
async def paginate_orders(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    _, status, page_str = callback.data.split("|", 2)
    await render_orders_page(callback.message, status, int(page_str), edit=True)
    await callback.answer()


@dp.message(F.text == "👥 Foydalanuvchilar ro'yxati")
async def admin_users_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Qaysi ro'yxatni ko'rmoqchisiz?", reply_markup=user_categories_kb())


@dp.callback_query(F.data.startswith("usercat|"))
async def show_user_category(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    utype = callback.data.split("|", 1)[1]
    await render_users_page(callback.message, utype, 0, edit=False)
    await callback.answer()


@dp.callback_query(F.data.startswith("usrpage|"))
async def paginate_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    _, utype, page_str = callback.data.split("|", 2)
    await render_users_page(callback.message, utype, int(page_str), edit=True)
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


# ============================================================
# ESLATMA (SCHEDULER)
# ============================================================

def _parse_dt(date_str: str, time_str: str) -> datetime:
    """"YYYY-MM-DD" va "H:MM"/"HH:MM" ko'rinishidagi qiymatlarni birlashtirib,
    O'zbekiston vaqt zonasidagi datetime obyektiga aylantiradi. Soat bir xonali
    ("2:12") yoki ikki xonali ("02:12") bo'lishidan qat'i nazar to'g'ri ishlaydi
    (strptime, farqli o'laroq fromisoformat'dan, bir xonali soatni ham qabul qiladi).
    """
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UZ_TZ)


def _order_datetime(order):
    """Buyurtma sanasi va vaqtini O‘zbekiston vaqti bilan qaytaradi."""
    return _parse_dt(order["date_str"], order["time_str"])


def schedule_reminder(order_id: int):
    """Buyurtma uchun ikkita xabar rejalashtiradi:
    1) uyg'otish vaqtidan 5 daqiqa oldin - oddiy eslatma (tugmasiz);
    2) aynan mijoz aytgan vaqtda - "mijoz bilan gaplashdingizmi?" so'rovi tugma bilan.
    """
    order = get_order(order_id)
    if not order or order["status"] != "accepted":
        return
    if not order["date_str"] or not order["time_str"]:
        logging.warning("Order #%s sana yoki vaqtsiz.", order_id)
        return

    try:
        target = _order_datetime(order)
        now = datetime.now(UZ_TZ)

        # Uyg'otish vaqti allaqachon o'tib ketgan bo'lsa, hech narsa rejalashtirilmaydi.
        if target <= now:
            logging.info("Order #%s uchun vaqt o'tgan, reminder o'tkazib yuborildi.", order_id)
            return

        early_run = target - timedelta(minutes=REMINDER_BEFORE_MINUTES)
        # Agar bot kech ishga tushgan bo'lsa (5 daqiqalik eslatma vaqti allaqachon
        # o'tgan, lekin asosiy vaqt hali kelmagan bo'lsa), erta eslatmani darhol yuboramiz.
        if early_run <= now:
            early_run = now + timedelta(seconds=3)

        scheduler.add_job(
            send_admin_reminder_early,
            trigger="date",
            run_date=early_run,
            args=[order_id],
            id=f"reminder_early_{order_id}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            send_admin_ask_talked,
            trigger="date",
            run_date=target,
            args=[order_id],
            id=f"reminder_talk_{order_id}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logging.info(
            "Order #%s uchun eslatmalar rejalashtirildi: erta=%s, so'rov=%s",
            order_id, early_run.isoformat(), target.isoformat(),
        )
    except (ValueError, TypeError):
        logging.exception("Order #%s reminder vaqtida xato.", order_id)


def restore_reminders():
    """Restart/redeploydan keyin DB'dagi accepted buyurtmalarni tiklaydi."""
    restored = 0
    for order in get_orders_by_status(["accepted"]):
        try:
            if _order_datetime(order) > datetime.now(UZ_TZ):
                schedule_reminder(order["id"])
                restored += 1
        except Exception:
            logging.exception("Order #%s reminderini tiklashda xato.", order["id"])
    logging.info("%s ta reminder qayta tiklandi.", restored)


async def send_admin_reminder_early(order_id: int):
    """Uyg'otish vaqtidan 5 daqiqa oldin yuboriladigan oddiy eslatma (tugmasiz)."""
    order = get_order(order_id)
    if not order or order["status"] != "accepted":
        return

    text = (
        "⏰ <b>5 daqiqalik eslatma</b>\n\n"
        f"👤 <b>Ism:</b> {order['full_name'] or '-'}\n"
        f"📞 <b>Telefon:</b> {order['phone'] or '-'}\n"
        f"📅 <b>Sana:</b> {order['date_str'] or '-'}\n"
        f"🕐 <b>Vaqt:</b> {order['time_str'] or '-'}\n"
        f"🎯 <b>Sabab:</b> {order['reason'] or '-'}\n\n"
        "5 daqiqadan so'ng belgilangan uyg'otish vaqti keladi."
    )

    for admin_id in ADMIN_IDS:
        try:
            sent = await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            set_reminder_msg_id(order_id, admin_id, sent.message_id)
        except Exception:
            logging.exception("Erta eslatma #%s admin %s ga yuborilmadi.", order_id, admin_id)


async def send_admin_ask_talked(order_id: int):
    """Aynan mijoz belgilagan vaqtda yuboriladigan 'gaplashdingizmi?' so'rovi (tugma bilan)."""
    order = get_order(order_id)
    if not order or order["status"] != "accepted":
        return

    text = (
        "❓ <b>Uyg'otish vaqti keldi</b>\n\n"
        f"👤 <b>Ism:</b> {order['full_name'] or '-'}\n"
        f"📞 <b>Telefon:</b> {order['phone'] or '-'}\n"
        f"🕐 <b>Vaqt:</b> {order['time_str'] or '-'}\n\n"
        "Iltimos, hozir shu raqamga qo'ng'iroq qiling.\n"
        "Mijoz bilan gaplashdingizmi?"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                reply_markup=admin_talked_kb(order_id),
            )
        except Exception:
            logging.exception("So'rov #%s admin %s ga yuborilmadi.", order_id, admin_id)


# ============================================================
# ISHGA TUSHIRISH
# ============================================================

async def main():
    init_db()
    logging.info("Bot ishga tushmoqda...")
    logging.info("Adminlar: %s", sorted(ADMIN_IDS))
    logging.info("Reminder: %s daqiqa oldin", REMINDER_BEFORE_MINUTES)

    scheduler.start()
    restore_reminders()

    try:
        await dp.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())