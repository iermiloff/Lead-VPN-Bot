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
    MAIN_ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0  # Первый ID — главный админ
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
            status TEXT DEFAULT '🆕 Новая',
            processed_by TEXT DEFAULT 'Не обработан'
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
def add_user(tg_id, username, referrer_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    parent_referrer = "нет"
    if referrer_code != "нет":
        cursor.execute("SELECT referrer_code FROM users WHERE username = ? OR tg_id = ?", (referrer_code, referrer_code))
        row = cursor.fetchone()
        if row: parent_referrer = row[0]

    cursor.execute("INSERT OR IGNORE INTO users (tg_id, username, referrer_code, parent_referrer) VALUES (?, ?, ?, ?)", 
                   (tg_id, username, referrer_code, parent_referrer))
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

def add_order(client_id, target_type, channel, ref_level_1):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    ref_level_2 = "нет"
    if ref_level_1 != "нет":
        cursor.execute("SELECT parent_referrer FROM users WHERE username = ? OR tg_id = ?", (ref_level_1, ref_level_1))
        row = cursor.fetchone()
        if row: ref_level_2 = row[0]

    cursor.execute("INSERT INTO orders (client_id, target_type, channel, ref_level_1, ref_level_2) VALUES (?, ?, ?, ?, ?)", 
                   (client_id, target_type, channel, ref_level_1, ref_level_2))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id, ref_level_2

def get_partner_stats(partner_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT target_type, channel, status FROM orders WHERE ref_level_1 = ?", (str(partner_code),))
    level_1_orders = cursor.fetchall()
    cursor.execute("SELECT target_type, channel, status FROM orders WHERE ref_level_2 = ?", (str(partner_code),))
    level_2_orders = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_code = ?", (str(partner_code),))
    sub_partners_count = cursor.fetchone()[0]
    conn.close()
    return level_1_orders, level_2_orders, sub_partners_count

def update_order_status_with_manager(order_id, status, manager_info):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = ?, processed_by = ? WHERE id = ?", (status, manager_info, order_id))
    conn.commit()
    conn.close()

def get_installed_orders():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT o.id, o.target_type, o.channel, u.username, o.client_id, o.ref_level_1, o.ref_level_2 
        FROM orders o 
        JOIN users u ON o.client_id = u.tg_id 
        WHERE o.status = '🟢 Установлен'
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- СОСТОЯНИЯ FSM ---
class VPNOrder(StatesGroup):
    target_type = State()
    channel = State()

class PartnerReg(StatesGroup):
    entering_wallet = State()

class AdminCalculation(StatesGroup):
    entering_revenues = State()

# --- КНОПКИ И СТАРТ ---
def main_menu(user_id):
    buttons = [
        [InlineKeyboardButton(text="🚀 Хочу запустить свой VPN", callback_data="role_client")],
        [InlineKeyboardButton(text="🤝 Личный кабинет (Партнерка)", callback_data="role_partner")],
        [InlineKeyboardButton(text="💬 Канал поддержки", url=SUPPORT_LINK)]
    ]
    if user_id == MAIN_ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="📊 [Админ] Расчет выплат", callback_data="admin_start_calc")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    referrer = "нет"
    if command.args:
        referrer = command.args
        await state.update_data(referrer=referrer)

    add_user(message.from_user.id, message.from_user.username, referrer)
    await message.answer("Привет! Я бот-ассистент сервиса VPN-конструктора.\nПомогаю запустить ваш собственный VPN за 5 минут без ИТ-знаний.", reply_markup=main_menu(message.from_user.id))

# --- СЦЕНАРИЙ КЛИЕНТА ---
@dp.callback_query(F.data == "role_client")
async def start_client_flow(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    buttons = [[InlineKeyboardButton(text="👥 Для себя и друзей", callback_data="tgt_friends")], [InlineKeyboardButton(text="📢 Для подписчиков канала", callback_data="tgt_channel")]]
    await callback.message.answer("Шаг 1: Для какой аудитории вы хотите создать VPN-сервис?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
        await callback.message.answer("🎉 Заявка успешно отправлена! Скоро менеджер свяжется с вами.")
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
    await message.answer("🎉 Заявка успешно отправлена! Скоро менеджер свяжется с вами.")

# --- ИНТЕРФЕЙС CRM ДЛЯ МЕНЕДЖЕРОВ ---
def get_crm_keyboard(order_id, current_status):
    buttons = []
    if current_status == "🆕 Новая":
        buttons.append([InlineKeyboardButton(text="🤝 Взять в работу", callback_data=f"crm_status_{order_id}_work")])
        buttons.append([InlineKeyboardButton(text="❌ Отказать", callback_data=f"crm_status_{order_id}_reject")])
    elif current_status == "⚙️ В работе":
        buttons.append([InlineKeyboardButton(text="📩 Отправить оффер", callback_data=f"crm_status_{order_id}_offer")])
        buttons.append([InlineKeyboardButton(text="❌ Отказать", callback_data=f"crm_status_{order_id}_reject")])
    elif current_status == "📩 Оффер отправлен":
        buttons.append([InlineKeyboardButton(text="🟢 VPN Установлен", callback_data=f"crm_status_{order_id}_install")])
        buttons.append([InlineKeyboardButton(text="❌ Отказать", callback_data=f"crm_status_{order_id}_reject")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_admin_alerts(user, target_type, channel, ref_l1, ref_l2, order_id):
    admin_alert = (
        f"🚨 **НОВАЯ ЗАЯВКА НА VPN (#{order_id})**\n"
        f"----------------------------------------\n"
        f"👤 От: @{user.username or 'нет'}\n"
        f"🎯 Цель: {target_type}\n"
        f"📢 Канал: {channel}\n"
        f"🥇 L1: `{ref_l1}` | 🥈 L2: `{ref_l2}`\n"
        f"📌 Статус: `🆕 Новая`"
    )
    markup = get_crm_keyboard(order_id, "🆕 Новая")
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(chat_id=admin_id, text=admin_alert, parse_mode="Markdown", reply_markup=markup)
        except Exception: pass

@dp.callback_query(F.data.startswith("crm_status_"))
async def handle_crm_status_change(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("У вас нет прав.")
    parts = callback.data.split("_"); order_id = int(parts[2]); action = parts[3]
    
    status_map = {"work": "⚙️ В работе", "reject": "❌ Отказано", "offer": "📩 Оффер отправлен", "install": "🟢 Установлен"}
    new_status = status_map.get(action, "В обработке")
    manager_username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID: {callback.from_user.id}"
    update_order_status_with_manager(order_id, new_status, f"{new_status} ({manager_username})")
    await callback.answer(f"Статус: {new_status}")
    
    lines = callback.message.text.split("\n")
    clean_lines = [l for l in lines if not l.startswith("📌 Статус:") and not l.startswith("ℹ️")]
    updated_text = "\n".join(clean_lines) + f"\n📌 Статус: `{new_status}`\nℹ️ {new_status} (Менеджер: {manager_username})"
    new_markup = None if new_status in ["🟢 Установлен", "❌ Отказано"] else get_crm_keyboard(order_id, new_status)
    await callback.message.edit_text(updated_text, parse_mode="Markdown", reply_markup=new_markup)

# --- СЦЕНАРИЙ ПАРТНЕРКИ + АНТИСПАМ ---
@dp.callback_query(F.data == "role_partner")
async def partner_cabinet(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    wallet = get_user_wallet(callback.from_user.id)
    
    if wallet == "не указан":
        await callback.message.answer(
            "💎 **Регистрация в партнерской программе**\n\n"
            "Мы выплачиваем вознаграждения в токенах экосистемы TON.\n"
            "Пожалуйста, отправьте адрес вашего **TON-кошелька**:\n\n"
            "🚫 **ВАЖНОЕ ПРАВИЛО:** Любые формы спам-рассылок (в ЛС, чаты, комментарии) **категорически запрещены**. "
            "Партнеры, уличенные в использовании спама, будут навсегда исключены из программы с полной аннуляцией баланса. "
            "Мы заботимся о репутации нашего сервиса."
        )
        await state.set_state(PartnerReg.entering_wallet)
    else:
        partner_code = callback.from_user.username if callback.from_user.username else callback.from_user.id
        bot_info = await bot.get_me()
        ref_link = f"https://t.me{bot_info.username}?start={partner_code}"
        
        level_1, level_2, sub_partners = get_partner_stats(partner_code)
        stats_text = f"📊 **Ваша сеть:**\n👥 Под-партнеры: {sub_partners}\n🥇 Уровень 1 (30%): {len(level_1)} шт.\n🥈 Уровень 2 (10%): {len(level_2)} шт."
        
        await callback.message.answer(
            f"🤝 **Личный кабинет партнера**\n\n👛 Ваш TON-кошелек: `{wallet}`\n\n🔗 **Ваша рекламная ссылка:**\n`{ref_link}`\n\n{stats_text}\n\nВы получаете круглые **30% (L1)** и **10% (L2)** от чистой прибыли, поступившей в сервис от ваших рефералов.\n\n⚠️ **Правила платформы:** Спам строго запрещен. При фиксации жалоб — мгновенная блокировка аккаунта и обнуление начислений.",
            parse_mode="Markdown"
        )

@dp.message(PartnerReg.entering_wallet)
async def process_wallet(message: types.Message, state: FSMContext):
    wallet_address = message.text.strip()
    if len(wallet_address) < 40: return await message.answer("❌ Неверный адрес кошелька TON. Попробуйте еще раз:")
    update_user_wallet(message.from_user.id, wallet_address)
    await state.clear()
    await message.answer("✅ TON-кошелек успешно привязан!")
    class FakeCallback: from_user = message.from_user; message = message; answer = lambda x: None
    await partner_cabinet(FakeCallback, state)

# --- МОДУЛЬ ОКРУГЛЕННОГО РАСЧЕТА (60 / 30 / 10) ---
@dp.callback_query(F.data == "admin_start_calc")
async def admin_start_calculation(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id != MAIN_ADMIN_ID: return
    active_orders = get_installed_orders()
    if not active_orders: return await callback.message.answer("❌ Нет запущенных VPN-сервисов (со статусом '🟢 Установлен') для расчета.")
    await state.update_data(queue=active_orders, current_index=0, revenues={})
    await ask_next_channel_revenue(callback.message, state)

async def ask_next_channel_revenue(message: types.Message, state: FSMContext):
    data = await state.get_data(); queue = data['queue']; idx = data['current_index']
    if idx < len(queue):
        order = queue[idx]; order_id, target_type, channel, username, client_id, ref_l1, ref_l2 = order
        display_name = f"@{username} (Друзья)" if target_type == "Для друзей" else channel
        await message.answer(f"🔢 **Шаг {idx+1}/{len(queue)}**\nВведите сумму чистой выручки (вашу долю 35%) от:\n`{display_name}`")
        await state.set_state(AdminCalculation.entering_revenues)
    else: await generate_final_report(message, state)

@dp.message(AdminCalculation.entering_revenues)
async def process_channel_revenue(message: types.Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID: return
    try: revenue = float(message.text)
    except ValueError: return await message.answer("❌ Введите число:")
    data = await state.get_data(); queue = data['queue']; idx = data['current_index']; revenues = data['revenues']
    revenues[queue[idx][0]] = revenue
    await state.update_data(revenues=revenues, current_index=idx + 1)
    await ask_next_channel_revenue(message, state)

async def generate_final_report(message: types.Message, state: FSMContext):
    data = await state.get_data(); queue = data['queue']; revenues = data['revenues']
    await state.clear()
    partner_payouts = {}; details_log = ""; total_received_money = 0; total_clean_profit = 0
    
    for order in queue:
        order_id, target_type, channel, username, client_id, ref_l1, ref_l2 = order
        incoming_sum = revenues.get(order_id, 0.0); total_received_money += incoming_sum
        display_name = f"@{username} (Друзья)" if target_type == "Для друзей" else channel
        
        # КРАСИВОЕ ОКРУГЛЕНИЕ: 30% первому уровню, 10% второму уровню от входящих денег
        ref_l1_share = incoming_sum * 0.30 if ref_l1 != "нет" else 0.0
        ref_l2_share = incoming_sum * 0.10 if ref_l2 != "нет" else 0.0
        my_clean_share = incoming_sum - ref_l1_share - ref_l2_share; total_clean_profit += my_clean_share
        
        details_log += f"🔹 `{display_name}` | Поступило вам: {incoming_sum:.2f}р\n"
        if ref_l1 != "нет": details_log += f" ├ L1 ({ref_l1} - 30%): {ref_l1_share:.2f}р\n"
        if ref_l2 != "нет": details_log += f" ├ L2 ({ref_l2} - 10%): {ref_l2_share:.2f}р\n"
        details_log += f" └ Ваш профит (60%): {my_clean_share:.2f}р\n\n"
        
        if ref_l1 != "нет": partner_payouts[ref_l1] = partner_payouts.get(ref_l1, 0.0) + ref_l1_share
        if ref_l2 != "нет": partner_payouts[ref_l2] = partner_payouts.get(ref_l2, 0.0) + ref_l2_share

    payout_sheet = "📋 **ВЕДОМОСТЬ ВЫПЛАТ ПАРТНЕРАМ:**\n"
    for partner, amount in partner_payouts.items():
        payout_sheet += f"👤 `{partner}` ➡️ **{amount:.2f} руб.**\n"
        partner_tg_id = get_tg_id_by_code(partner)
        if partner_tg_id and amount > 0:
            wallet_addr = get_user_wallet(partner_tg_id)
            try: await bot.send_message(chat_id=partner_tg_id, text=f"💰 **Подведены итоги месяца!**\n\nВам начислено вознаграждение: **{amount:.2f} руб.**\nВыплата отправлена на ваш TON-кошелек:\n`{wallet_addr}`")
            except Exception: pass

    final_report = f"📊 **ОТЧЕТ И РАССЫЛКА (СХЕМА 60/30/10)**\n\n{details_log}--------------------\n{payout_sheet}\n--------------------\n📥 Всего вошло: **{total_received_money:.2f} руб.**\n👑 Ваша чистая доля (60%): **{total_clean_profit:.2f} руб.**"
    await message.answer(final_report, parse_mode="Markdown")

async def main(): await dp.start_polling(bot)
if __name__ == "__main__": asyncio.run(main())
