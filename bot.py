import os
import sqlite3
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me")

# Парсим список админов из .env
admin_ids_raw = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]
    MAIN_ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0
except (ValueError, IndexError):
    exit("Ошибка: Неверный формат ADMIN_IDS в файле .env!")

if not BOT_TOKEN or not ADMIN_IDS:
    exit("Ошибка: Переменные BOT_TOKEN или ADMIN_IDS не настроены!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_FILE = "vpn_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        referrer_code TEXT,
        parent_referrer TEXT DEFAULT 'нет',
        ton_wallet TEXT DEFAULT 'не указан'
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER,
        target_type TEXT,
        channel TEXT DEFAULT 'Не указан (для друзей)',
        ref_level_1 TEXT DEFAULT 'нет',
        ref_level_2 TEXT DEFAULT 'нет',
        status TEXT DEFAULT 'Новая',
        status_code TEXT DEFAULT 'new',
        processed_by TEXT DEFAULT 'Не обработан',
        locked_by_admin_id INTEGER DEFAULT NULL
    )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- ФУНКЦИИ БАЗЫ ДАННЫХ (ПОЛЬЗОВАТЕЛИ) ---

def add_user(tg_id, username, referrer_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    username_str = username if username else f"id_{tg_id}"
    parent_referrer = "нет"
    
    # Защита от саморефералов: реферер не должен совпадать с ID или username юзера
    if referrer_code and referrer_code != "нет":
        if str(referrer_code) != str(tg_id) and str(referrer_code) != str(username):
            cursor.execute("SELECT tg_id, username FROM users WHERE username = ? OR tg_id = ?", (str(referrer_code), str(referrer_code)))
            row = cursor.fetchone()
            if row: 
                parent_referrer = row[1] if row[1] and not row[1].startswith("id_") else str(row[0])
            
    cursor.execute("INSERT OR IGNORE INTO users (tg_id, username, referrer_code, parent_referrer) VALUES (?, ?, ?, ?)", 
                   (tg_id, username_str, referrer_code, parent_referrer))
    conn.commit()
    conn.close()

def update_user_wallet(tg_id, wallet):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET ton_wallet = ? WHERE tg_id = ?", (wallet, tg_id))
    conn.commit()
    conn.close()

def get_user_wallet(tg_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT ton_wallet FROM users WHERE tg_id = ?", (tg_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "не указан"

def get_tg_id_by_code(code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id FROM users WHERE username = ? OR tg_id = ?", (str(code), str(code)))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None
# --- ФУНКЦИИ БАЗЫ ДАННЫХ (ЗАЯВКИ И РЕФЕРАЛЫ) ---

def add_order(client_id, target_type, channel, ref_level_1):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    ref_level_2 = "нет"
    if ref_level_1 != "нет":
        cursor.execute("SELECT parent_referrer FROM users WHERE username = ? OR tg_id = ?", (ref_level_1, ref_level_1))
        row = cursor.fetchone()
        if row: ref_level_2 = row
        
    cursor.execute("""
        INSERT INTO orders (client_id, target_type, channel, ref_level_1, ref_level_2, status, status_code) 
        VALUES (?, ?, ?, ?, ?, 'Новая', 'new')
    """, (client_id, target_type, channel, ref_level_1, ref_level_2))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id, ref_level_2

def get_user_orders(client_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, target_type, status FROM orders WHERE client_id = ? ORDER BY id DESC", (client_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_installed_orders():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT o.id, o.target_type, o.channel, u.username, o.client_id, o.ref_level_1, o.ref_level_2 
        FROM orders o 
        JOIN users u ON o.client_id = u.tg_id 
        WHERE o.status_code = 'install'
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_partner_stats(partner_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT target_type, channel, status FROM orders WHERE ref_level_1 = ?", (str(partner_code),))
    level_1_orders = cursor.fetchall()
    cursor.execute("SELECT target_type, channel, status FROM orders WHERE ref_level_2 = ?", (str(partner_code),))
    level_2_orders = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_code = ?", (str(partner_code),))
    row = cursor.fetchone()
    sub_partners_count = row[0] if row else 0
    conn.close()
    return level_1_orders, level_2_orders, sub_partners_count


# --- СОСТОЯНИЯ FSM ---
class VPNOrder(StatesGroup):
    target_type = State()
    channel = State()

class PartnerReg(StatesGroup):
    entering_wallet = State()

class AdminCalculation(StatesGroup):
    entering_revenues = State()


# --- ИНТЕРФЕЙС И ГЛАВНОЕ МЕНЮ ---

def main_menu(user_id):
    buttons = []
    
    # Если зашел администратор
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="💼 Панель CRM (Заявки)", callback_data="admin_crm_list")])
        buttons.append([InlineKeyboardButton(text="🗄️ Архив (Закрытые заявки)", callback_data="admin_crm_archive")])
        if user_id == MAIN_ADMIN_ID:
            buttons.append([InlineKeyboardButton(text="🧮 Расчет выплат", callback_data="admin_start_calc")])
    # Если зашел обычный клиент
    else:
        buttons.append([InlineKeyboardButton(text="🚀 Хочу запустить свой VPN", callback_data="role_client")])
        buttons.append([InlineKeyboardButton(text="📊 Мои заявки", callback_data="client_orders_status")])
        buttons.append([InlineKeyboardButton(text="🤝 Партнерская программа", callback_data="role_partner")])
        buttons.append([InlineKeyboardButton(text="💬 Канал поддержки", url=SUPPORT_LINK)])
        
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    referrer = "нет"
    if command.args:
        referrer = command.args
        await state.update_data(referrer=referrer)
        
    add_user(message.from_user.id, message.from_user.username, referrer)
    
    welcome_text = (
        "👋 Добро пожаловать в панель администратора CRM!" if message.from_user.id in ADMIN_IDS
        else "Привет! Я бот-ассистент сервиса VPN-конструктора.\nПомогаю запустить ваш собственный VPN за 5 минут без ИТ-знаний."
    )
    await message.answer(welcome_text, reply_markup=main_menu(message.from_user.id))
# --- МОНИТОРИНГ СТАТУСОВ ДЛЯ КЛИЕНТА ---

@dp.callback_query(F.data == "client_orders_status")
async def client_orders_status(callback: types.CallbackQuery):
    await callback.answer()
    orders = get_user_orders(callback.from_user.id)
    
    if not orders:
        return await callback.message.answer("📭 У вас пока нет активных заявок.")
        
    report = "📋 <b>Статус ваших заявок:</b>\n\n"
    
    for o_id, t_type, status in orders:
        st_lower = status.lower()
        icon = "⏳"
        if "работ" in st_lower: icon = "⚙️"
        elif "оффер" in st_lower: icon = "📩"
        elif "установл" in st_lower: icon = "✅"
        elif "отказ" in st_lower: icon = "❌"
        
        report += f"🔹 <b>Заявка #{o_id}</b> ({t_type})\n└ Статус: {icon} {status.strip()}\n\n"
        
    buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]
    await callback.message.answer(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("Главное меню:", reply_markup=main_menu(callback.from_user.id))


# --- УПРАВЛЕНИЕ ЗАЯВКАМИ В CRM (ДЛЯ АДМИНОВ) ---

def get_crm_keyboard(order_id, current_status):
    buttons = []
    if "Новая" in current_status:
        buttons.append([InlineKeyboardButton(text="🎯 Взять в работу", callback_data=f"crm_status_{order_id}_work")])
        buttons.append([InlineKeyboardButton(text="❌ Отказать", callback_data=f"crm_status_{order_id}_reject")])
    elif "В работе" in current_status:
        buttons.append([InlineKeyboardButton(text="📩 Отправить оффер", callback_data=f"crm_status_{order_id}_offer")])
        buttons.append([InlineKeyboardButton(text="❌ Отказать", callback_data=f"crm_status_{order_id}_reject")])
    elif "Оффер отправлен" in current_status:
        buttons.append([InlineKeyboardButton(text="🚀 VPN Установлен", callback_data=f"crm_status_{order_id}_install")])
        buttons.append([InlineKeyboardButton(text="❌ Отказать", callback_data=f"crm_status_{order_id}_reject")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin_crm_list")
async def admin_crm_list(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("У вас нет прав.", show_alert=True)
    await callback.answer()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT o.id, o.target_type, o.status, u.username 
        FROM orders o 
        JOIN users u ON o.client_id = u.tg_id 
        WHERE o.status_code NOT IN ('install', 'reject')
        ORDER BY o.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return await callback.message.answer("🎉 Все активные заявки обработаны! Новых пока нет.")
        
    await callback.message.answer("📂 <b>Список активных заявок в CRM:</b>", parse_mode="HTML")
    for o_id, t_type, status, username in rows:
        msg = (
            f"📦 <b>Заявка #{o_id}</b>\n"
            f"👤 От: @{username}\n"
            f"🎯 Тип: {t_type}\n"
            f"📊 Статус: <code>{status}</code>"
        )
        await callback.message.answer(msg, parse_mode="HTML", reply_markup=get_crm_keyboard(o_id, status))


@dp.callback_query(F.data == "admin_crm_archive")
async def admin_crm_archive(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("У вас нет прав.", show_alert=True)
    await callback.answer()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT o.id, o.target_type, o.status, u.username, o.processed_by 
        FROM orders o 
        JOIN users u ON o.client_id = u.tg_id 
        WHERE o.status_code IN ('install', 'reject')
        ORDER BY o.id DESC LIMIT 50
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return await callback.message.answer("📭 В архиве пока нет закрытых заявок.")
        
    await callback.message.answer("🗄️ <b>Архив закрытых заявок (Последние 50):</b>", parse_mode="HTML")
    for o_id, t_type, status, username, processed_by in rows:
        msg = (
            f"📦 <b>Заявка #{o_id}</b> (Архив)\n"
            f"👤 От: @{username}\n"
            f"🎯 Тип: {t_type}\n"
            f"📊 Итог: <code>{status}</code>\n"
            f"🧑‍💻 Лог: {processed_by}"
        )
        await callback.message.answer(msg, parse_mode="HTML")


@dp.callback_query(F.data.startswith("crm_status_"))
async def handle_crm_status_change(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("У вас нет прав.", show_alert=True)
        
    parts = callback.data.split("_")
    order_id = int(parts[2])
    action = parts[3]
    
    status_map = {"work": "В работе", "reject": "❌ Отказано", "offer": "Оффер отправлен", "install": "Установлен"}
    code_map = {"work": "work", "reject": "reject", "offer": "offer", "install": "install"}
    
    new_status = status_map.get(action, "В обработке")
    new_code = code_map.get(action, "processing")
    manager_username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID: {callback.from_user.id}"
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT locked_by_admin_id, status, client_id, target_type FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return await callback.answer("Ошибка: Заявка не найдена в базе.", show_alert=True)
        
    locked_by, current_db_status, client_id, target_type = row
    
    if action == "work" and locked_by is not None and locked_by != callback.from_user.id:
        conn.close()
        return await callback.answer("⚠️ Эта заявка уже взята в работу другим менеджером!", show_alert=True)
        
    if locked_by is not None and locked_by != callback.from_user.id:
        conn.close()
        return await callback.answer("🔒 С этой заявкой уже ведет работу другой администратор.", show_alert=True)
        
    if action == "work":
        cursor.execute("UPDATE orders SET status = ?, status_code = ?, processed_by = ?, locked_by_admin_id = ? WHERE id = ?", 
                       (new_status, new_code, f"{new_status} ({manager_username})", callback.from_user.id, order_id))
    else:
        cursor.execute("UPDATE orders SET status = ?, status_code = ?, processed_by = ? WHERE id = ?", 
                       (new_status, new_code, f"{new_status} ({manager_username})", order_id))
        
    conn.commit()
    conn.close()
    
    await callback.answer(f"Установлен статус: {new_status}")
    
    if action == "work":
        try:
            await bot.send_message(
                chat_id=client_id,
                text=(
                    f"⚙️ <b>Ваша заявка #{order_id} принята в работу!</b>\n\n"
                    f"Менеджер {manager_username} уже занимается развертыванием вашего VPN-сервиса (Тип: {target_type}). "
                    f"Ожидайте, скоро вам придет коммерческое предложение в ЛС!"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
            
    lines = callback.message.text.split("\n")
    clean_lines = [l for l in lines if not l.startswith("📊 Статус:") and not l.startswith("🧑‍💻")]
    updated_text = "\n".join(clean_lines) + f"\n📊 Статус: <code>{new_status}</code>\n🧑‍💻 Менеджер: {manager_username}"
    
    new_markup = None if new_code in ["install", "reject"] else get_crm_keyboard(order_id, new_status)
    await callback.message.edit_text(updated_text, parse_mode="HTML", reply_markup=new_markup)
# --- СЦЕНАРИЙ СОЗДАНИЯ ЗАЯВКИ КЛИЕНТОМ ---

@dp.callback_query(F.data == "role_client")
async def start_client_flow(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="👥 Для себя и друзей", callback_data="tgt_friends")], 
        [InlineKeyboardButton(text="📢 Для подписчиков канала", callback_data="tgt_channel")]
    ]
    await callback.message.answer(
        "Шаг 1: Для какой аудитории вы хотите создать VPN-сервис?", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(VPNOrder.target_type)


@dp.callback_query(VPNOrder.target_type)
async def process_target_type(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "tgt_friends":
        user_data = await state.get_data()
        ref_l1 = user_data.get("referrer", "нет")
        await state.clear()
        
        order_id, ref_l2 = add_order(callback.from_user.id, "Для друзей", "Не требуется", ref_l1)
        await send_admin_alerts(callback.from_user, "Для друзей", "Не требуется", ref_l1, ref_l2, order_id)
        await callback.message.answer("Заявка успешно отправлена! Скоро менеджер свяжется с вами.")
    elif callback.data == "tgt_channel":
        await state.update_data(target_type="Для канала")
        await callback.message.answer("Шаг 2: Отправьте ссылку на ваш Telegram-канал:")
        await state.set_state(VPNOrder.channel)


@dp.message(VPNOrder.channel)
async def process_channel(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    ref_l1 = user_data.get("referrer", "нет")
    target_type = user_data.get("target_type", "Для канала")
    await state.clear()
    
    order_id, ref_l2 = add_order(message.from_user.id, target_type, message.text, ref_l1)
    await send_admin_alerts(message.from_user, target_type, message.text, ref_l1, ref_l2, order_id)
    await message.answer("Заявка успешно отправлена! Скоро менеджер свяжется с вами.")


async def send_admin_alerts(user, target_type, channel, ref_l1, ref_l2, order_id):
    admin_alert = (
        f"🚨 <b>НОВАЯ ЗАЯВКА НА VPN #{order_id}</b>\n"
        f"----------------------------------------\n"
        f"👤 От: @{user.username or 'нет'} (ID: {user.id})\n"
        f"🎯 Цель: {target_type}\n"
        f"🔗 Канал: {channel}\n"
        f"👥 L1: {ref_l1} | L2: {ref_l2}\n"
        f"📊 Статус: <code>Новая</code>"
    )
    markup = get_crm_keyboard(order_id, "Новая")
    for admin_id in ADMIN_IDS:
        try: 
            await bot.send_message(chat_id=admin_id, text=admin_alert, parse_mode="HTML", reply_markup=markup)
        except Exception: 
            pass


# --- ПАРТНЕРСКАЯ ПРОГРАММА ---

async def show_partner_cabinet(message_or_callback, user_id, username, state: FSMContext):
    wallet = get_user_wallet(user_id)
    is_callback = isinstance(message_or_callback, types.CallbackQuery)
    target_msg = message_or_callback.message if is_callback else message_or_callback

    if wallet == "не указан":
        text_reg = (
            "🤝 <b>Регистрация в партнерской программе</b>\n\n"
            "Мы выплачиваем вознаграждения в токенах экосистемы TON.\n"
            "Пожалуйста, отправьте адрес вашего TON-кошелька:\n\n"
            "⚠️ <b>ВАЖНОЕ ПРАВИЛО:</b> Любые формы спам-рассылок категорически запрещены. "
            "Партнеры, уличенные в использовании спама, будут навсегда исключены с аннуляцией баланса."
        )
        await target_msg.answer(text_reg, parse_mode="HTML")
        await state.set_state(PartnerReg.entering_wallet)
    else:
        partner_code = username if username else str(user_id)
        bot_info = await bot.get_me()
        ref_link = f"https://t.me{bot_info.username}?start={partner_code}"
        
        level_1, level_2, sub_partners = get_partner_stats(partner_code)
        stats_text = (
            f"🕸 <b>Ваша сеть:</b>\n"
            f"└ Под-партнеры: {sub_partners}\n"
            f"└ Уровень 1 (30%): {len(level_1)} шт.\n"
            f"└ Уровень 2 (10%): {len(level_2)} шт."
        )
        
        cabinet_text = (
            f"💼 <b>Личный кабинет партнера</b>\n\n"
            f"💳 Ваш TON-кошелек: <code>{wallet}</code>\n\n"
            f"🔗 <b>Ваша рекламная ссылка:</b>\n<code>{ref_link}</code>\n\n"
            f"{stats_text}\n\n"
            f"💰 Вы получаете 30% (L1) и 10% (L2) от чистой прибыли сервиса.\n\n"
            f"🚫 Спам строго запрещен. При фиксации жалоб — мгновенное обнуление начислений."
        )
        
        buttons = [[InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_menu")]]
        await target_msg.answer(cabinet_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "role_partner")
async def partner_cabinet(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_partner_cabinet(callback, callback.from_user.id, callback.from_user.username, state)


@dp.message(PartnerReg.entering_wallet)
async def process_wallet(message: types.Message, state: FSMContext):
    wallet_address = message.text.strip()
    if len(wallet_address) < 40: 
        return await message.answer("❌ Неверный адрес кошелька TON. Попробуйте еще раз:")
        
    update_user_wallet(message.from_user.id, wallet_address)
    await state.clear()
    await message.answer("✅ TON-кошелек успешно привязан!")
    await show_partner_cabinet(message, message.from_user.id, message.from_user.username, state)


# --- ИСПРАВЛЕННЫЙ МОДУЛЬ РАСЧЕТА ВЫПЛАТ АДМИНОМ ---

@dp.callback_query(F.data == "admin_start_calc")
async def admin_start_calculation(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id != MAIN_ADMIN_ID: 
        return
        
    active_orders = get_installed_orders()
    if not active_orders: 
        return await callback.message.answer("❌ Нет запущенных VPN-сервисов (Установлен) для расчета.")
        
    # Сбрасываем и инициализируем состояние заново
    await state.set_data({"queue": active_orders, "current_index": 0, "revenues": {}})
    await ask_next_channel_revenue(callback.message, state)


async def ask_next_channel_revenue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    queue = data.get('queue', [])
    idx = data.get('current_index', 0)
    
    if idx < len(queue):
        order = queue[idx]
        order_id, target_type, channel, username, client_id, ref_l1, ref_l2 = order
        display_name = f"@{username} (Друзья)" if target_type == "Для друзей" else channel
        
        await message.answer(f"📊 <b>Шаг {idx+1}/{len(queue)}</b>\nВведите сумму выручки для Заявки #{order_id} от:\n<code>{display_name}</code>", parse_mode="HTML")
        await state.set_state(AdminCalculation.entering_revenues)
    else: 
        await generate_final_report(message, state)


@dp.message(AdminCalculation.entering_revenues)
async def process_channel_revenue(message: types.Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID: 
        return
        
    try: 
        revenue = float(message.text.strip())
    except ValueError: 
        return await message.answer("❌ Введите корректное число:")
        
    data = await state.get_data()
    queue = data.get('queue', [])
    idx = data.get('current_index', 0)
    revenues = data.get('revenues', {})
    
    # Защита от выхода за границы, если админ прислал текст повторно
    if idx >= len(queue):
        return await generate_final_report(message, state)
        
    order = queue[idx]
    order_id = order[0] # Берем строго числовой ID заявки
    
    # Сохраняем выручку строго по ID заявки в качестве ключа
    revenues[str(order_id)] = revenue
    
    # Сразу обновляем индекс и данные, чтобы избежать параллельных накладок
    await state.update_data(revenues=revenues, current_index=idx + 1)
    
    # Переходим к следующему шагу
    await ask_next_channel_revenue(message, state)

async def generate_final_report(message: types.Message, state: FSMContext):
    data = await state.get_data()
    queue = data.get('queue', [])
    revenues = data.get('revenues', {})
    
    # Сбрасываем только переменные шагов расчета, сохраняя возможность повторного запуска в следующем месяце
    await state.update_data(current_index=0, revenues={})
    await state.set_state(None) # Выходим из режима ввода цифр
    
    partner_payouts = {}
    details_log = ""
    total_received_money = 0
    total_clean_profit = 0
    
    for order in queue:
        order_id, target_type, channel, username, client_id, ref_l1, ref_l2 = order
        
        incoming_sum = revenues.get(str(order_id), 0.0)
        total_received_money += incoming_sum
        display_name = f"@{username} (Друзья)" if target_type == "Для друзей" else channel
        
        ref_l1_share = incoming_sum * 0.30 if ref_l1 != "нет" else 0.0
        ref_l2_share = incoming_sum * 0.10 if ref_l2 != "нет" else 0.0
        my_clean_share = incoming_sum - ref_l1_share - ref_l2_share
        total_clean_profit += my_clean_share
        
        details_log += f"🔹 Заявка #{order_id} | <code>{display_name}</code>\n└ Поступило: {incoming_sum:.2f}р\n"
        if ref_l1 != "нет": details_log += f" ├ L1 ({ref_l1} - 30%): {ref_l1_share:.2f}р\n"
        if ref_l2 != "нет": details_log += f" ├ L2 ({ref_l2} - 10%): {ref_l2_share:.2f}р\n"
        details_log += f" └ Ваш профит: {my_clean_share:.2f}р\n\n"
        
        if ref_l1 != "нет": partner_payouts[ref_l1] = partner_payouts.get(ref_l1, 0.0) + ref_l1_share
        if ref_l2 != "нет": partner_payouts[ref_l2] = partner_payouts.get(ref_l2, 0.0) + ref_l2_share
        
    payout_sheet = "📋 <b>ВЕДОМОСТЬ ВЫПЛАТ ПАРТНЕРАМ:</b>\n"
    for partner, amount in partner_payouts.items():
        partner_tg_id = get_tg_id_by_code(partner)
        wallet_addr = get_user_wallet(partner_tg_id) if partner_tg_id else "не указан"
        
        payout_sheet += f"👤 <code>{partner}</code> — <b>{amount:.2f} руб.</b>\n└ Кошелек: <code>{wallet_addr}</code>\n"
        
        if partner_tg_id and amount > 0:
            try: 
                await bot.send_message(
                    chat_id=partner_tg_id, 
                    text=f"🎉 <b>Подведены итоги месяца!</b>\n\nВам начислено вознаграждение: <b>{amount:.2f} руб.</b>\nВыплата отправлена на TON-кошелек:\n<code>{wallet_addr}</code>",
                    parse_mode="HTML"
                )
            except Exception: 
                pass
                
    final_report = (
        f"📊 <b>ОТЧЕТ И РАССЫЛКА</b>\n\n"
        f"{details_log}--------------------\n"
        f"{payout_sheet}\n--------------------\n"
        f"💰 Всего вошло: <b>{total_received_money:.2f} руб.</b>\n"
        f"💵 Ваша чистая доля: <b>{total_clean_profit:.2f} руб.</b>"
    )
    await message.answer(final_report, parse_mode="HTML")


async def main(): 
    await dp.start_polling(bot)


if __name__ == "__main__": 
    asyncio.run(main())
