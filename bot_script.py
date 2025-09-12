import MetaTrader5 as mt5
import time
import logging
import colorlog
import pytz
import sqlite3
from flask import Flask, request
import threading
import arabic_reshaper
from bidi.algorithm import get_display
import numpy as np
from scipy.interpolate import make_interp_spline
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
import io
from datetime import date
from dateutil.relativedelta import relativedelta, SA # برای پیدا کردن شنبه
from telegram import Bot
from telegram.error import BadRequest, NetworkError
from datetime import datetime, timedelta # تغییر ضروری: timedelta اضافه شد
from telegram.ext import Updater, CommandHandler # تغییر ضروری: کتابخانه‌های شنونده اضافه شدند
from telegram.ext import ConversationHandler, MessageHandler, Filters # کتابخانه تاریخ دستی
# این کد تا قبل از تغییر محاسبه سود بازه برای واریز و برداشت ها اوکیه

# ====================== ساکت کردن گزارشگرهای پیش‌فرض تلگرام ======================
# این بخش گزارش‌های خطای شبکه‌ای پیش‌فرض کتابخانه تلگرام و وابستگی‌های آن را غیرفعال می‌کند
# تا فقط مدیر خطای شخصی ما (handle_error) پیام‌ها را چاپ کند.
# ====== تنظیمات لاگ رنگی و پیشرفته ======
# تعریف کدهای رنگی ANSI برای تاریخ
ORANGE = '\033[33m'
RESET = '\033[0m'

# ۱. گرفتن گزارشگر اصلی
log = logging.getLogger()
log.setLevel(logging.INFO)

# ۲. ساخت یک فرمت‌دهنده رنگی
# %(log_color)s: رنگ را بر اساس سطح خطا تنظیم می‌کند
# ما تاریخ را به صورت دستی با کدهای ANSI رنگی می‌کنیم
formatter = colorlog.ColoredFormatter(
    f'{ORANGE}%(asctime)s{RESET}%(log_color)s[%(levelname)s]{RESET}%(message)s',
    log_colors={
        'DEBUG':    'cyan',
        'INFO':     'green',
        'WARNING':  'yellow',
        'ERROR':    'red',
        'CRITICAL': 'bold_red',
    }
)

# ۳. ساخت یک کنترل‌کننده و اعمال فرمت‌دهنده روی آن
handler = colorlog.StreamHandler()
handler.setFormatter(formatter)

# ۴. اضافه کردن کنترل‌کننده به گزارشگر اصلی
log.addHandler(handler)
logging.getLogger('telegram.vendor.ptb_urllib3.urllib3.connectionpool').setLevel(logging.CRITICAL)
logging.getLogger('telegram.ext.updater').setLevel(logging.CRITICAL)

# =====================تابع برگرداندن منطقه زمانی بروکر =====================
def determine_broker_timezone():
    """
    اختلاف زمانی سرور بروکر با UTC را محاسبه کرده و رشته منطقه زمانی صحیح را برمی‌گرداند.
    """
    logging.info("Determining broker timezone...")
    if not mt5.initialize(path=MT5_PATH):
        logging.error("Could not connect to MT5 to determine timezone.")
        return None

    server_tick = mt5.symbol_info_tick("BTCUSD")
    if not server_tick or server_tick.time == 0:
        logging.error("Could not get server time from tick.")
        # mt5.shutdown()
        return None
    
    # زمان سرور و زمان جهانی را به صورت "آگاه از منطقه زمانی" ایجاد می‌کنیم
    server_time = datetime.fromtimestamp(server_tick.time, tz=pytz.utc)
    # logging.info(f"Server time (UTC): {server_time}")
    utc_now = datetime.now(pytz.utc)
    # logging.info(f"Current UTC time: {utc_now}")

    # اختلاف را به ساعت گرد می‌کنیم
    time_difference_hours = (server_time - utc_now).total_seconds() / 3600.0
    # logging.info(f"Detected timezone difference (hours): {time_difference_hours}")
    offset = round(time_difference_hours) # به نزدیک‌ترین ساعت کامل گرد می‌کنیم
    
    # logging.info(f"Detected timezone offset: UTC{offset:+}")

    # ساخت رشته صحیح Etc/GMT (علامت برعکس است)
    offset_sign = "+" if offset <= 0 else "-"
    # timezone_str = f"Etc/GMT{offset_sign}{abs(offset)}"
    timezone_str = "Etc/GMT+0"
    # mt5.shutdown()
    logging.info(f"Timezone: {timezone_str}")
    return timezone_str

# ========================= تنظیمات اصلی =========================
TOKEN = 

CHAT_ID = 

# --- مراحل مکالمه برای گزارش سفارشی ---
START_DATE, END_DATE = range(2)
# +++ کد جدید +++
# لیستی برای ذخیره شناسه‌های پیام‌های هشدار جهت حذف در آینده
KEYWORDS_TO_KEEP = [
    "Position Closed", 
    "Order Filled",
    "GMM-Glory",
    "Position Closed (Partial)",
    "Position Closed (Complete)"
]
# لیست شناسه‌های تمام پیام‌هایی که می‌توانند حذف شوند (چون کلمات کلیدی بالا را ندارند)
alert_message_ids = []

CHECK_INTERVAL = 5 # فاصله زمانی بین هر چک در حالت عادی

# ---  تنظیمات اتصال مجدد به متاتریدر ---
RECONNECT_DELAY = 10  # هر چند ثانیه برای اتصال مجدد تلاش کند
OVERALL_TIMEOUT = 6000 # مهلت زمانی نهایی به ثانیه (600 ثانیه = 10 دقیقه)

# --- تنظیمات تلاش مجدد برای ارسال تلگرام ---
RETRY_COUNT = 2000
RETRY_DELAY = 2

# --- مسیر متاتریدر خاص ---
MT5_PATH = "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
BROKER_TIMEZONE = None
# --- آستانه برای محاسبه وین ریت واقعی ---
WINRATE_THRESHOLD_PERCENT = 0.05 # 0.05% of starting balance

# ========================= تنظیمات پایگاه داده =========================
DB_NAME = "bot_data.db" # نام فایل پایگاه داده شما

def setup_database():
    """پایگاه داده و جدول مورد نیاز را در صورت عدم وجود ایجاد می‌کند."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # یک جدول برای نگهداری شناسه‌های پیام ایجاد می‌کنیم
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages_to_delete (
            message_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("Database setup complete.")

def load_ids_from_db():
    """شناسه‌های پیام را از پایگاه داده خوانده و در یک لیست برمی‌گرداند."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT message_id FROM messages_to_delete")
    # نتیجه به صورت لیستی از تاپل‌هاست، آن‌ها را به یک لیست ساده از اعداد تبدیل می‌کنیم
    ids = [item[0] for item in cursor.fetchall()]
    conn.close()
    logging.info(f"Loaded {len(ids)} message IDs from database.")
    return ids

def add_id_to_db(message_id):
    """یک شناسه پیام جدید را به پایگاه داده اضافه می‌کند."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # از INSERT OR IGNORE استفاده می‌کنیم تا اگر شناسه تکراری بود، خطایی رخ ندهد
    cursor.execute("INSERT OR IGNORE INTO messages_to_delete (message_id) VALUES (?)", (message_id,))
    conn.commit()
    conn.close()

def remove_id_from_db(message_id):
    """یک شناسه پیام را از پایگاه داده حذف می‌کند."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages_to_delete WHERE message_id = ?", (message_id,))
    conn.commit()
    conn.close()

# ====================== اتصال به تلگرام ======================
bot = Bot(token=TOKEN)
updater = None

def send_telegram(text):
    # ۱. تلاش اول انجام می‌شود
    try:
        sent_message = bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
        return sent_message # --- تغییر: به جای True، کل شیء پیام را برگردان ---

    except Exception as e:
        # ۲. اگر تلاش اول ناموفق بود، فقط یک بار هشدار ارسال می‌شود
        logging.error(f"Telegram Send Error retrying... (1/{RETRY_COUNT})")#: {e}")
        # try:
        #     bot.send_message(chat_id=CHAT_ID, text="⚠️ Network unstable.", parse_mode='Markdown')
        # except Exception as e_warn:
        #     logging.error(f"⚠️Could not send the warning message: {e_warn}")

    # ۳. حلقه تلاش‌های مجدد شروع می‌شود (چون تلاش اول ناموفق بود)
    for i in range(1, RETRY_COUNT): 
        time.sleep(RETRY_DELAY)
        #logging.error(f"Telegram Send Error retrying... ({i+1}/{RETRY_COUNT})")
        try:
            # تلاش برای ارسال پیام اصلی
            sent_message = bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
            return sent_message # --- تغییر: در حلقه هم شیء پیام را برگردان ---
        except Exception as e:
            if i > 10:
                bot.send_message(chat_id=CHAT_ID, text="⚠️ Network unstable.", parse_mode='Markdown')
            logging.error(f"Telegram Send Error retrying... ({i+1}/{RETRY_COUNT})")#: {e}")

    # اگر همه تلاش‌ها ناموفق بود
    logging.critical("❌Could not send message to Telegram after all retries.")
    #send_telegram("❌ Failed to send a message after multiple retries.")
    bot.send_message(chat_id=CHAT_ID, text="❌ Failed to send a message after multiple retries.", parse_mode='Markdown')
    return None # --- تغییر: در صورت شکست نهایی، None برگردان ---
#----------------- تابع گزارش خطای شبکه listening -------------------
def handle_error(update, context):
    """خطاهای مربوط به شنونده تلگرام را مدیریت کرده و یک پیام ساده چاپ می‌کند."""
    # ما فقط برای خطاهای مربوط به شبکه پیام ساده چاپ می‌کنیم تا خطاهای مهم دیگر پنهان نشوند
    if "urllib3 HTTPError" in str(context.error) or "SSLEOFError" in str(context.error):
        logging.warning("Listener Network error")
    else:
        # برای خطاهای دیگر، جزئیات را چاپ می‌کنیم تا در صورت نیاز بتوانید آنها را رفع کنید
        logging.critical(f"listener unhandled error: {context.error}")

#-------------------- تابع های گزارش تاریخ دستی ----------------------------------------------    
def custom_report_start(update, context):
    sent_messages_info = []
    """مکالمه را برای دریافت گزارش سفارشی شروع می‌کند."""
        # ۱. متن پیام را در یک متغیر با نام مشخص قرار دهید
    prompt_text = "لطفاً تاریخ شروع را در فرمت YYYY/MM/DD وارد کنید (مثال: 2025/08/01).\nبرای لغو، /cancel را ارسال کنید."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    process_messages_for_clearing(sent_messages_info)
    return START_DATE # به مرحله بعدی (دریافت تاریخ شروع) برو

def received_start_date(update, context):
    sent_messages_info = []
    """تاریخ شروع را دریافت کرده و منتظر تاریخ پایان می‌ماند."""
    try:
        # تاریخ دریافت شده را در حافظه موقت مکالمه ذخیره می‌کنیم
        naive_start_time = datetime.strptime(update.message.text, '%Y/%m/%d')
        context.user_data['start_date'] = make_aware(naive_start_time)
        prompt_text = "عالی! حالا لطفاً تاریخ پایان را در همان فرمت وارد کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return END_DATE # به مرحله بعدی (دریافت تاریخ پایان) برو
    except ValueError:
        prompt_text = "فرمت تاریخ اشتباه است. لطفاً دوباره در فرمت YYYY/MM/DD وارد کنید یا برای لغو /cancel را ارسال کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return START_DATE # در همین مرحله باقی بمان
    
def received_end_date(update, context):
    sent_messages_info = []
    """تاریخ پایان را دریافت کرده، گزارش را ساخته و مکالمه را تمام می‌کند."""
    try:
        naive_end_date_input = datetime.strptime(update.message.text, '%Y/%m/%d')
        # برای اینکه معاملات روز پایان را هم شامل شود، آن را به انتهای روز منتقل می‌کنیم
        naive_end_time = naive_end_date_input.replace(hour=23, minute=59, second=59)
        end_time = make_aware(naive_end_time)
        # --- بخش جدید: چک می‌کنیم که تاریخ پایان از تاریخ شروع بزرگتر باشد ---
        start_time = context.user_data['start_date']
        if end_time < start_time:
            prompt_text = "خطا: تاریخ پایان نمی‌تواند قبل از تاریخ شروع باشد. لطفاً تاریخ پایان را دوباره وارد کنید یا برای لغو /cancel را ارسال کنید."
            sent_msg = update.message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
            process_messages_for_clearing(sent_messages_info)
            return END_DATE # در همین مرحله باقی بمان تا کاربر تاریخ جدید را وارد کند
        start_time = context.user_data['start_date']
        
        # فراخوانی موتور اصلی گزارش‌ساز با تاریخ‌های سفارشی
        generate_and_send_report(update, context, start_time, end_time, "سفارشی")
        
        # پاک کردن حافظه موقت و پایان مکالمه
        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        prompt_text = "فرمت تاریخ اشتباه است. لطفاً دوباره در فرمت YYYY/MM/DD وارد کنید یا برای لغو /cancel را ارسال کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return END_DATE # در همین مرحله باقی بمان
    
def cancel_conversation(update, context):
    """مکالمه را لغو می‌کند."""
    sent_messages_info = []
    prompt_text = "لغو شد."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    process_messages_for_clearing(sent_messages_info)
    context.user_data.clear()
    return ConversationHandler.END 

# ====================== بخش وب‌سرور برای دریافت هشدارها ======================
app = Flask(__name__)

@app.route('/alert', methods=['POST'])
def handle_alert():
    """این تابع هر زمان که یک هشدار از متاتریدر دریافت شود، اجرا می‌شود."""
    alert_message = request.data.decode('utf-8')
    
    # چاپ پیام در کنسول برای اطمینان از دریافت
    # logging.info(f"{alert_message}")

    # ارسال پیام به تلگرام
    # ما این کار را در یک ترد جداگانه انجام می‌دهیم تا سرور بلافاصله پاسخ دهد
    threading.Thread(target=send_alert_and_log, args=(alert_message,)).start()

    return "OK", 200

def run_flask_server():
    # --- این دو خط، لاگ پیش‌فرض وب‌سرور را غیرفعال می‌کنند ---
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    """این تابع وب‌سرور را راه‌اندازی می‌کند."""
    # پارامتر use_reloader=False برای جلوگیری از خطا در تردها ضروری است
    app.run(host='127.0.0.1', port=5000, use_reloader=False)

def send_alert_and_log(message):
    """پیام هشدار را به تلگرام ارسال کرده و نتیجه را در یک خط در کنسول چاپ می‌کند."""
    sent_message = send_telegram(message)
    # --- تغییر: اگر پیام با موفقیت ارسال شد، شناسه آن را ذخیره کن ---
    if sent_message:
        # بررسی می‌کنیم که آیا هیچ‌کدام از کلمات کلیدی در متن پیام وجود دارد یا نه
        is_important = any(keyword in message for keyword in KEYWORDS_TO_KEEP)
        
        # اگر پیام مهم نبود (کلمات کلیدی را نداشت)، آن را برای حذف علامت‌گذاری کن
        if not is_important:
            # messages_to_clear.append(sent_message.message_id)
            alert_message_ids.append(sent_message.message_id)
            add_id_to_db(sent_message.message_id) # +++ این خط را اضافه کنید +++
            # logging.info(f"(Payload marked for clearing, ID: {sent_message.message_id})")
        # else:
        #     logging.info(f"(Payload is important, ID: {sent_message.message_id})")
        logging.info(f"(Send payload ok)")
    else:
        logging.error(f"(Send payload error)")
    # status = "(Send payload ok)" if sent_message else f"(Send payload error)"
    # logging.info(f"{message.strip()}{status}")
    # logging.info(f"{status}")

# ====================== توابع گزارش‌گیری ======================
def generate_and_send_report(update, context, start_time, end_time, title):
    sent_messages_info = [] # <--- لیست محلی برای این تابع
    
    """موتور اصلی برای ساخت و ارسال تمام گزارش‌ها"""
    terminal_info = mt5.terminal_info()
    if not terminal_info or not terminal_info.connected:
        prompt_text = "اسکریپت به متاتریدر متصل نیست. لطفاً چند لحظه دیگر دوباره تلاش کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return
    # به جای 0، از یک تاریخ معقول در گذشته (مثلاً ۵ سال قبل) شروع می‌کنیم
    # این کار از محدودیت‌های احتمالی بروکر جلوگیری می‌کند
    # naive_start_time = datetime.combine(end_time.date() - timedelta(days=7), datetime.min.time())
    # start_time = make_aware(naive_start_time)
    start_date_for_history = end_time - timedelta(days=365 * 5)  # 5 years back
    # در ابتدا کل تاریخچه رو میگیریم بعدا فیلتر میکنیم که توی بازه نشون بده فقط
    deals = mt5.history_deals_get(start_date_for_history, end_time)

    if not deals:
        prompt_text = f"در بازه زمانی گزارش ({title}) هیچ معامله‌ای یافت نشد."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return

    report_lines, total_profit, closed_trades_count, win_count = [], 0.0, 0, 0
    # متغیرهای جدید برای آمار سود و ضرر
    max_profit = 0.0
    max_loss = 0.0
    total_profit_sum = 0.0
    total_loss_sum = 0.0
    profit_trades_count = 0
    loss_trades_count = 0
    actual_date_report = ""
    
    real_win_count = 0
    real_loss_count = 0
    breakeven_count = 0
    total_balance_change_period = 0.0 
    trade_counter = 1
    commission = 0.0
    swap = 0.0
    positions = {}
    # این حلقه برای گرفتن معاملات کل تاریخچه از 5 سال گذشته تا الان هست بجز اون شرط زمان که داخلش هست
    for deal in deals:
        # --- این خط را اضافه کنید تا تراکنش‌های غیرمعاملاتی نادیده گرفته شوند ---
        if deal.position_id == 0:
            continue # این دیل را نادیده بگیر و برو سراغ دیل بعدی
        # --- این بلوک کد جدید را اضافه کنید ---
        # این بلاک برای محاسبه ی این متغییر ها در بازه ی گزارش است
        deal_time = datetime.fromtimestamp(deal.time, tz=pytz.utc)
        if start_time <= deal_time <= end_time:
            total_balance_change_period += deal.profit + deal.commission + deal.swap
            commission += deal.commission
            swap += deal.swap
        # --- پایان بلوک جدید ---
        total_profit += deal.commission + deal.swap # سود کل معاملات تاریخچه نه بازه
        # --- بخش جدید برای تجمیع اطلاعات پوزیشن ---
        position_id = deal.position_id
        if position_id not in positions:
            positions[position_id] = {
                'profit': 0.0,
                'volume': 0.0,
                'symbol': deal.symbol,
                'close_time': 0,
                'trade_volume': 0.0
            }
        
        positions[position_id]['profit'] += deal.profit# + deal.commission + deal.swap
        
        if deal.entry == mt5.DEAL_ENTRY_IN:
            positions[position_id]['volume'] += deal.volume
            # فقط حجم اولین معامله ورودی را به عنوان حجم کل پوزیشن در نظر می‌گیریم
            if positions[position_id]['trade_volume'] == 0:
                positions[position_id]['trade_volume'] = deal.volume
        elif deal.entry == mt5.DEAL_ENTRY_OUT:
            positions[position_id]['volume'] -= deal.volume
            positions[position_id]['close_time'] = deal.time # زمان آخرین خروج ثبت می‌شود
        # --- پایان بخش جدید ---
        if deal.entry == mt5.DEAL_ENTRY_OUT:
            # closed_trades_count += 1
            total_profit += deal.profit
            # if deal.profit >= 0:
            #     win_count += 1
    # --- بخش جدید برای ساخت لیست گزارش از پوزیشن‌های نهایی ---
    trade_counter = 1
    
    # --- بخش جدید: فیلتر کردن پوزیشن‌ها بر اساس بازه زمانی اصلی گزارش ---
    final_positions = {}
    for pos_id, pos_data in positions.items():
        # شرط ۱: پوزیشن باید کاملا بسته شده باشد
        is_closed = abs(pos_data['volume']) < 0.01
        
        if is_closed and pos_data['close_time'] > 0:
            # زمان را به صورت آگاه از منطقه زمانی (UTC) ایجاد می‌کنیم
            close_datetime = datetime.fromtimestamp(pos_data['close_time'], tz=pytz.utc)
            # logging.info("position id: ", pos_id," close time: ", close_datetime)
            # شرط ۲: زمان بسته شدن پوزیشن باید در بازه گزارش اصلی باشد
            if start_time <= close_datetime <= end_time:
                final_positions[pos_id] = pos_data
    # --- پایان بخش جدید ---
    active_trading_days_set = set()
    # مرتب‌سازی پوزیشن‌ها بر اساس زمان بسته شدن برای نمایش به ترتیب
    sorted_positions = sorted(final_positions.items(), key=lambda item: item[1]['close_time'])

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
    # این بلوک کد را برای پیدا کردن تاریخ اولین ترید اضافه کنید
    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
    first_trade_date_str = "---" # مقدار پیش‌فرض
    if sorted_positions:
        # زمان اولین پوزیشن بسته شده در بازه را می‌گیریم
        first_trade_timestamp = sorted_positions[0][1]['close_time']
        first_trade_dt_utc = datetime.fromtimestamp(first_trade_timestamp, tz=pytz.utc)

        # به منطقه زمانی بروکر تبدیل می‌کنیم
        broker_tz = pytz.timezone(BROKER_TIMEZONE)
        first_trade_dt_broker = first_trade_dt_utc.astimezone(broker_tz)
        first_trade_date_str = first_trade_dt_broker.strftime('%Y/%m/%d')
    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    for position_id, pos_data in sorted_positions:
        # یک پوزیشن زمانی کاملا بسته شده که حجم باقی‌مانده آن نزدیک به صفر باشد
        if abs(pos_data['volume']) < 0.01 and pos_data['close_time'] > 0:
            utc_time = datetime.fromtimestamp(pos_data['close_time'], tz=pytz.utc)
            broker_tz = pytz.timezone(BROKER_TIMEZONE)
            broker_dt_object = utc_time.astimezone(broker_tz)
            trade_date = broker_dt_object.strftime('%y/%m/%d %H:%M:%S')
            active_trading_days_set.add(broker_dt_object.date())
  
            # شمارنده وین ریت قدیمی همچنان برای مقایسه محاسبه می‌شود
            closed_trades_count += 1
            # ... کد قبلی شما
            if pos_data['profit'] >= 0:
                win_count += 1
                profit_trades_count += 1
                total_profit_sum += pos_data['profit']
                if pos_data['profit'] > max_profit:
                    max_profit = pos_data['profit']
            else: # اگر معامله با ضرر بسته شده بود
                loss_trades_count += 1
                total_loss_sum += pos_data['profit'] # ضررها منفی هستند،そのままجمع می‌کنیم
                if pos_data['profit'] < max_loss:
                    max_loss = pos_data['profit']

            line = f"{trade_counter:02d}-{pos_data['symbol']}|{pos_data['trade_volume']:.2f}|{pos_data['profit']:>8,.2f}|{trade_date}"
            # line = f"{trade_counter:02d}-{pos_data['symbol']}|{pos_data['trade_volume']:.2f}|{pos_data['profit']:>8,.2f}|{trade_date}"
            report_lines.append(f"`{line}`")
            trade_counter += 1
    # --- پایان بخش جدید ---
    
    avg_profit = total_profit_sum / profit_trades_count if profit_trades_count > 0 else 0.0
    avg_loss = total_loss_sum / loss_trades_count if loss_trades_count > 0 else 0.0
    
        
    if not report_lines:
        prompt_text = f"در بازه زمانی گزارش ({title}) هیچ پوزیشنی بسته نشده است."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return

    win_rate = (win_count / closed_trades_count * 100) if closed_trades_count > 0 else 0
    total_profit_sign = "✅" if total_profit >= 0 else "🔻"

# --- بخش جدید: محاسبه دقیق سود و رشد ---
    account_info = mt5.account_info()
    profit_line = ""
    growth_line = ""
    Not_available = "" # اگر توی گزارش مقداری نبود این کاراکتر

    if account_info:
        # --- محاسبه دقیق سود کل اکانت از ابتدا ---
        # --- بخش جدید: محاسبه دقیق سود کل اکانت بر اساس واریزی‌ها ---
        # --- بخش جدید: محاسبه دقیق سود کل اکانت بر اساس شماره سفارش (order) ---
        all_deals = mt5.history_deals_get(0, get_server_time())
        total_balance_operations = 0.0
        if all_deals:
            for d in all_deals:
                # تراکنش‌های واریز/برداشت معمولا شماره سفارش (order) صفر دارند
                if d.order == 0:
                    total_balance_operations += d.profit
        
        # سود کل اکانت = بالانس فعلی - مجموع واریزی‌ها و برداشتی‌ها
        true_total_account_profit = account_info.balance - total_balance_operations
                                
        # --- محاسبه سود بازه زمانی ---
        # این بخش بدون تغییر است و از قبل درست بود
        # starting_balance_period = account_info.balance - total_profit
        
        # --- بخش جدید: محاسبه هوشمند بالانس ابتدای بازه ---
        # چک می‌کنیم که آیا گزارش تا لحظه حال است یا یک گزارش تاریخی است
        # (با یک بازه خطای ۵ ثانیه‌ای)
        current_balance = ""
        current_equity = ""
        historical_end_balance = ""
        
        # --- بخش جدید: تشخیص هوشمند نوع گزارش (لحظه‌ای یا تاریخی) ---
        is_live_report = False # پیش‌فرض را روی تاریخی می‌گذاریم

        # شرط اول: آیا اختلاف زمانی بسیار کم است؟ (برای گزارش‌های روزانه)
        if abs((end_time - get_server_time()).total_seconds()) < 10:
            is_live_report = True
        # شرط دوم: آیا تاریخ پایان گزارش، همان تاریخ امروز است؟ (برای تاریخ دستی)
        elif end_time.date() == get_server_time().date():
            is_live_report = True

        # حالا بر اساس نتیجه، بالانس ابتدای بازه را محاسبه می‌کنیم
        if is_live_report:
            logging.info("Generating real-time report...")
            # این یک گزارش تا لحظه ی حال است، از فرمول ساده استفاده کن
            starting_balance_period = account_info.balance - total_balance_change_period
            actual_trading_days_count = len(active_trading_days_set)
            actual_date_report = f"اولین ترید(روز معاملاتی): ‎{first_trade_date_str}‏ ({str(actual_trading_days_count)})\n" if actual_trading_days_count > 1 else ""
            
            for position_id, pos_data in sorted_positions:
                # یک پوزیشن زمانی کاملا بسته شده که حجم باقی‌مانده آن نزدیک به صفر باشد
                if abs(pos_data['volume']) < 0.01 and pos_data['close_time'] > 0:
                    # --- بخش جدید: محاسبه هوشمند برد، باخت و سر به سر ---
                    threshold_amount = starting_balance_period * (WINRATE_THRESHOLD_PERCENT / 100.0)
                    
                    if pos_data['profit'] > threshold_amount:
                        real_win_count += 1
                    elif pos_data['profit'] < -threshold_amount:
                        real_loss_count += 1
                    else:
                        breakeven_count += 1
        
            # --- بخش جدید: گرفتن اطلاعات بالانس و اکوییتی ---
            account_info = mt5.account_info()
            balance_equity_line = f"**موجودی ابتدای بازه:**`‎{starting_balance_period:,.2f}`‏\n**موجودی(حال):**‎`{account_info.balance:>8.2f}`**|اکوییتی(حال):**`{account_info.equity:,.2f}`\n" if account_info else ""
            current_balance = f"{account_info.balance:,.2f}"
            current_equity = f"{account_info.equity:,.2f}" if account_info else Not_available
            display_end_time = end_time
            
        else:
            logging.info("Generating historical report...")
            # این یک گزارش تاریخ خاص است، از فرمول پیچیده‌تر استفاده کن
            # ابتدا سود معاملاتی که بعد از بازه گزارش انجام شده را پیدا می‌کنیم
            deals_after_period = mt5.history_deals_get(end_time, get_server_time())
            profit_after_period = 0.0
            if deals_after_period:
                for d in deals_after_period:
                    if d.entry in (mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_OUT):
                        profit_after_period += d.profit + d.commission + d.swap
            # logging.info(f"Profit from deals after the period: {profit_after_period}")
            # بالانس در انتهای بازه = بالانس فعلی - سود معاملات بعدی
            balance_at_period_end = account_info.balance - profit_after_period
            # logging.info(f"Current balance: {account_info.balance}")
            # logging.info(f"Balance at period end: {balance_at_period_end}")
            # بالانس ابتدای بازه = بالانس انتهای بازه - سود خود بازه
            starting_balance_period = balance_at_period_end - total_balance_change_period

            for position_id, pos_data in sorted_positions:
                # یک پوزیشن زمانی کاملا بسته شده که حجم باقی‌مانده آن نزدیک به صفر باشد
                if abs(pos_data['volume']) < 0.01 and pos_data['close_time'] > 0:
                    # --- بخش جدید: محاسبه هوشمند برد، باخت و سر به سر ---
                    threshold_amount = starting_balance_period * (WINRATE_THRESHOLD_PERCENT / 100.0)
                    
                    if pos_data['profit'] > threshold_amount:
                        real_win_count += 1
                    elif pos_data['profit'] < -threshold_amount:
                        real_loss_count += 1
                    else:
                        breakeven_count += 1

            # --- گرفتن اطلاعات بالانس تاریخی ---
            balance_equity_line = f"**موجودی ابتدای بازه:** `‎{starting_balance_period:,.2f}‏`\n**موجودی انتهای بازه:**`‎{balance_at_period_end:,.2f}`\n" if balance_at_period_end and starting_balance_period else ""
            historical_end_balance = f"{balance_at_period_end:,.2f}" if balance_at_period_end and starting_balance_period else Not_available
            # این شرط تضمین می‌کند که یک روز از تاریخ پایان فقط و فقط زمانی کم شود که گزارش شما یک گزارش تاریخی باشد و زمان پایان آن دقیقاً ساعت ۰۰:۰۰ بامداد باشد.
            # این کار باعث می‌شود که گزارش سفارشی شما (که زمان پایان آن ۲۳:۵۹ است) به درستی و بدون تغییر نمایش داده شود.
            if end_time.time() == datetime.min.time():
                display_end_time = end_time - timedelta(days=1)
            else:
                display_end_time = end_time
            # برای گزارش‌های تاریخی، یک روز از تاریخ پایان کم می‌کنیم تا بازه درست نمایش داده شود
            # display_end_time = end_time - timedelta(days=1)    

        profit_line = f"**سود اکانت(حال):**‎`{true_total_account_profit:>8.2f}$`‏|**سود بازه:** ‎`{total_balance_change_period:,.2f}$`\n"

        # --- محاسبه درصد رشد کل اکانت ---
        initial_deposit = account_info.balance - true_total_account_profit
        total_growth_percentage = 0.0
        if initial_deposit != 0:
            total_growth_percentage = (true_total_account_profit / initial_deposit) * 100
        total_growth_sign = "+" if total_growth_percentage >= 0 else ""
        total_growth_str = f"{total_growth_sign}{total_growth_percentage:.2f}%"

        # --- محاسبه درصد رشد بازه زمانی ---
        period_growth_percentage = 0.0
        if starting_balance_period != 0:
            period_growth_percentage = (total_balance_change_period / starting_balance_period) * 100
        period_growth_sign = "+" if period_growth_percentage >= 0 else ""
        period_growth_str = f"{period_growth_sign}{period_growth_percentage:.2f}%"

        growth_line = f"**درصد رشد اکانت(حال):**‎`{total_growth_str}`‏|**درصد رشد بازه:**‎`{period_growth_str}`\n"
        broker_account_line = f"`{account_info.company} | {account_info.login}`\n" if account_info else ""
        
        summary_old = (
        f"**📊 گزارش {title}**\n"
        f"_{start_time.strftime('%Y/%m/%d')} - {display_end_time.strftime('%Y/%m/%d')}_\n\n"
        f"{actual_date_report}"
        f"{balance_equity_line}"
        f"{profit_line}"
        f"{growth_line}"
        f"کمیسیون بازه:`‎{commission:.2f}`‏|سواپ بازه:‎`{swap:.2f}`\n"
        f"**نرخ برد بازه:**‎`{win_rate:.2f}%` ‏({win_count}/{closed_trades_count})\n"
        f"**نرخ برد واقعی:**`‎{((real_win_count / (real_win_count + real_loss_count) * 100) if (real_win_count + real_loss_count) > 0 else 0):.2f}%` ‏({real_win_count}/{real_win_count + real_loss_count})\n"
        f"**معاملات سر به سر:** `{breakeven_count}`\n"
        f"بیشترین س،ض: ‎{max_profit:,.2f}‏|‎{max_loss:,.2f}$\n"
        f"میانگین س،ض: ‎{avg_profit:,.2f}‏|‎{avg_loss:,.2f}$\n"
        f"**ت. پوزیشن‌های بازه:**`{closed_trades_count}`\n"
        f"{broker_account_line}"
        f"-----------------------------------"
        )

        # داده‌های جدول
        rows = [
            ["شاخص", "بازه", "اکنون"],
            ["موجودی", f"{starting_balance_period:,.2f}", Not_available],
            ["موجودی پایان", historical_end_balance+current_balance, Not_available],
            ["اکوئیتی", Not_available, current_equity],# تا اینجا فکر کنم درسته
            ["سود خالص", f"{total_balance_change_period:,.2f}$", f"{true_total_account_profit:,.2f}$"],
            ["رشد", f"{period_growth_str}", f"{total_growth_str}"],
            ["نرخ برد", f"({win_count}/{closed_trades_count})%{win_rate:.2f}", Not_available],
            ["نرخ برد واقعی", f"({real_win_count}/{real_win_count + real_loss_count})%{((real_win_count / (real_win_count + real_loss_count) * 100) if (real_win_count + real_loss_count) > 0 else 0):.2f}", Not_available],
            ["سر به سر", f"{breakeven_count}", Not_available],
            ["بیشترین س،ض$", f"{max_loss:.2f},{max_profit:.2f}", Not_available],
            ["میانگین س،ض$", f"{avg_loss:.2f},{avg_profit:.2f}", Not_available],
            ["تعداد معامله", f"{closed_trades_count}", Not_available],
            ["کمیسیون", f"{commission:.2f}", Not_available],
            ["سواپ", f"{swap:.2f}", Not_available],
        ]

        # طول بیشترین رشته برای ستون‌های عددی (ستون 2 و 3)
        col_widths = [
            max(len(str(row[0])) for row in rows),  # ستون اول فارسی
            max(len(str(row[1])) for row in rows),  # ستون عددی وسط
            max(len(str(row[2])) for row in rows),  # ستون عددی آخر
        ]
        # col_widths = [max(len(str(row[i])) for row in rows) for i in range(3)]

        # logging.info(col_widths)
        def format_number(val: str, width: int):
            if val == Not_available:
                return val.rjust(width)

            sign = ""
            suffix = ""

            # گرفتن علامت مثبت/منفی
            if val.startswith(("+", "-")):
                sign, val = val[0], val[1:]

            # گرفتن پسوند مثل % یا $
            if val.endswith("%") or val.endswith("$"):
                suffix, val = val[-1], val[:-1]

            # بازسازی با علامت چپ و پسوند راست
            formatted = f"{suffix}{val}{sign}"

            # راست‌چین کردن
            return formatted.rjust(width)
                
        # def format_row(row):
        #     # ستون اول: بدون padding، ستون 2 و 3 راست‌چین
        #     return f"`{str(row[0]).ljust(col_widths[0]-1)}|{str(row[1]).rjust(col_widths[1])}|{str(row[2]).rjust(col_widths[2])}`"
        def format_row(row):
            col1 = str(row[0]).ljust(col_widths[0])
            col2 = format_number(str(row[1]), col_widths[1])
            col3 = format_number(str(row[2]), col_widths[2])
            return f"`{col1}|{col2}|{col3}`"
        
        def make_title_line(title, total_width, sep_char="-"):
            # طول متن عنوان با فاصله‌های قبل و بعد
            title_text = f" {title} "
            title_len = len(title_text)

            # تعداد خط تیره‌های باقی‌مونده
            dashes = total_width - title_len
            left = dashes // 2
            right = dashes - left

            return "`" + (sep_char * left) + title_text + (sep_char * right) + "`"

        # ساخت جدول
        lines = []
        total_width = sum(col_widths) + 2  # 6 برای ' | ' بین ستون‌ها
        lines.append(make_title_line(f"گزارش {title}", total_width, "-"))
        lines.append(f"`بازه  : ‎{start_time.strftime('%Y/%m/%d')}-{display_end_time.strftime('%Y/%m/%d')}`")
        lines.append(f"`حساب  : ‎{account_info.company} {account_info.login}`")
        sep = "`" + "‏-" * total_width + "`"
        # sep_char = "ـ"  # Tatweel
        # sep = "`‏-`" * (sum(col_widths) + 0)
        lines.append(sep)
        lines.append(format_row(rows[0]))
        lines.append(sep)
        for row in rows[1:]:
            lines.append(format_row(row))
        lines.append(sep)

        # ارسال به تلگرام بدون monospace
        summary = "\n".join(lines)
        # sent_msg = update.message.reply_text(summary)


    sent_msg = update.message.reply_text(summary_old, parse_mode='Markdown')
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': summary_old})
    sent_msg = update.message.reply_text(summary, parse_mode='Markdown')
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': summary})
    time.sleep(1) 

    # --- بخش جدید: اضافه کردن هدر ---
    prompt_text = f"#N| Symbol | lot   |          Profit | Date"
    sent_msg = update.message.reply_text(prompt_text, parse_mode='Markdown')
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    CHUNK_SIZE = 40
    for i in range(0, len(report_lines), CHUNK_SIZE):
        chunk = report_lines[i:i + CHUNK_SIZE]
        message_part = "\n".join(chunk)
        sent_msg = update.message.reply_text(message_part, parse_mode='Markdown')
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': message_part})
        time.sleep(1)

    # ساخت لیست تمیز از پوزیشن‌های نهایی برای ارسال به تابع نمودار
    fully_closed_positions = [pos_data for position_id, pos_data in sorted_positions]
    # فراخوانی تابع برای ساخت و ارسال نمودار
    create_and_send_growth_chart(update, context, fully_closed_positions, starting_balance_period, title)   
    prompt_text = "End report.\nmonitoring continue..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    process_messages_for_clearing(sent_messages_info)
    
# ====================== رسم نمودار رشد ======================
def create_and_send_growth_chart(update, context, fully_closed_positions, starting_balance, title):
    sent_messages_info = []
    """نمودار رشد حساب را ساخته و به تلگرام ارسال می‌کند."""
    logging.info("Creating growth chart...")
    
    # # ۱. آماده‌سازی داده‌ها
    # dates = []
    # cumulative_profit = []
    # current_equity = starting_balance

    # # مرتب کردن معاملات بر اساس زمان
    closed_deals = fully_closed_positions#sorted([d for d in fully_closed_positions if d.entry == mt5.DEAL_ENTRY_OUT], key=lambda x: x.time)

    # if not sorted_deals:
    #     logging.warning("No closing deals to chart.")
    #     return

    # # اضافه کردن نقطه شروع نمودار
    # dates.append(datetime.fromtimestamp(sorted_deals[0].time - 1))
    # cumulative_profit.append(starting_balance)

    # # محاسبه سود تجمعی برای هر معامله
    # for deal in sorted_deals:
    #     current_equity += deal.profit + deal.commission + deal.swap
    #     dates.append(datetime.fromtimestamp(deal.time))
    #     cumulative_profit.append(current_equity)
        
# ۱. آماده‌سازی داده‌ها برای محور افقی بر اساس تعداد معاملات
    trade_numbers = []
    cumulative_profit = []
    current_equity = starting_balance

    if not closed_deals:
        logging.warning("No closing deals to chart.")
        prompt_text = "در بازه زمانی گزارش هیچ پوزیشنی بسته نشده است."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return

    # اضافه کردن نقطه شروع نمودار (معامله شماره صفر)
    trade_numbers.append(0)
    cumulative_profit.append(starting_balance)

    # محاسبه سود تجمعی برای هر معامله
    for i, position_data in enumerate(closed_deals):
        # --- این خط را برای دیباگ اضافه کنید ---
        # logging.info(f"Trade #{i+1}: current_equity_before={current_equity}, profit_to_add={position_data['profit']}")
        current_equity += position_data['profit']# + position_data['commission'] + position_data['swap']
        trade_numbers.append(i + 1) # شماره معامله (۱، ۲، ۳، ...)
        cumulative_profit.append(current_equity)
    # --- این شرط حیاتی را اضافه کنید ---
    if len(trade_numbers) < 4:
        logging.warning("Not enough data to create a chart.")
        prompt_text = "تعداد معاملات برای رسم نمودار کافی نیست."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
        process_messages_for_clearing(sent_messages_info)
        # در صورت تمایل می‌توانید یک پیام مناسب به کاربر تلگرام بفرستید
        # sent_msg = update.message.reply_text("تعداد معاملات برای رسم نمودار کافی نیست.")
        return # از ادامه تابع و بروز خطا جلوگیری می‌کند
    
    # ۲. رسم نمودار با خطوط منحنی و نرم
    # تبدیل لیست‌های عادی به آرایه‌های NumPy برای محاسبات
    x_original = np.array(trade_numbers)
    y_original = np.array(cumulative_profit)

    # ایجاد نقاط بسیار بیشتر برای رسم یک منحنی نرم
    x_smooth = np.linspace(x_original.min(), x_original.max(), 400)

    # ساخت مدل ریاضی (spline) و محاسبه مقادیر y برای نقاط جدید
    spl = make_interp_spline(x_original, y_original, k=3) # k=3 for a cubic spline
    y_smooth = spl(x_smooth)

    # ۲. رسم نمودار با کیفیت و اندازه بزرگ
    plt.figure(figsize=(12, 7), dpi=150) # اندازه ۱۲x۷ اینچ با کیفیت ۱۵۰ DPI
    
    # اگر سود نهایی مثبت بود، نمودار سبز باشد، در غیر این صورت قرمز
    line_color = 'green' if current_equity >= starting_balance else 'red'
    
    # رسم منحنی نرم
    plt.plot(x_smooth, y_smooth, color=line_color, linewidth=0.4)
    
    # اضافه کردن نقاط معاملات اصلی روی منحنی
    plt.scatter(x_original, y_original, color=line_color, s=1) # s=1 for marker size

    # تغییر: رسم بر اساس شماره معامله به جای تاریخ
    # plt.plot(trade_numbers, cumulative_profit, linestyle='-', color=line_color, marker='.', markersize=1, linewidth=0.4, label='Cumulative Profit')
    
    # این خط دیگر نیازی نیست و باید حذف یا کامنت شود
    # plt.xticks(rotation=45)  
    # --- بخش جدید: آماده‌سازی متن فارسی برای نمایش صحیح ---
    # ابتدا متن فارسی را برای اتصال حروف، آماده می‌کنیم
    reshaped_title_text = arabic_reshaper.reshape(f'نمودار رشد: {title}')
    # سپس، متن آماده شده را از راست به چپ مرتب می‌کنیم
    bidi_title_text = get_display(reshaped_title_text)
    
    # زیباسازی نمودار
    plt.title(bidi_title_text, fontsize=16, fontname='Tahoma')
    plt.xlabel('trade number', fontsize=6)
    plt.ylabel('balance', fontsize=6)
    plt.grid(True, linestyle='--', alpha=0.2)
    
    # --- بخش جدید: تنظیم هوشمند محورها ---
    ax = plt.gca() # گرفتن محورهای فعلی نمودار
    
    # محور افقی (تعداد معاملات) را طوری تنظیم کن که حداکثر 100 عدد صحیح نمایش دهد
    ax.xaxis.set_major_locator(MaxNLocator(nbins=100, integer=True))
    plt.xticks(fontname='calibri', fontsize=6)
    # --- تغییر کلیدی: تنظیم نقطه شروع محور افقی ---
    plt.xlim(left=0) # محور افقی را مجبور کن که از صفر شروع شود

    # محور عمودی (ارزش حساب) را طوری تنظیم کن که حداکثر 50 عدد خوانا نمایش دهد
    ax.yaxis.set_major_locator(MaxNLocator(nbins=50))
    plt.yticks(fontname='calibri', fontsize=6)
    
    # --- تغییر کلیدی: تنظیم دستی پرش اعداد در محور افقی ---
    # تنظیم اعداد محور افقی با فونت مشخص
    # plt.xticks(range(0, len(trade_numbers), 1), fontname='calibri', fontsize=6)
    # --- تغییر کلیدی: تنظیم دستی پرش اعداد در محور عمودی ---
    # تنظیم اعداد محور عمودی با فونت مشخص
    # plt.yticks(range(int(min(cumulative_profit)), int(max(cumulative_profit)) + 100, 20), fontname='calibri', fontsize=6)
    plt.tight_layout() # برای جلوگیری از بریده شدن برچسب‌ها

    # ۳. ذخیره نمودار در حافظه RAM
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    
    # ۴. ارسال تصویر به تلگرام
    logging.info("Sending chart to Telegram...")
    send_msg = update.message.reply_photo(photo=buf, caption=f"نمودار رشد: {title}")
    if send_msg:
        sent_messages_info.append({'id': send_msg.message_id, 'text': f"نمودار رشد: {title}"})
    # بستن نمودار برای آزاد کردن حافظه
    plt.close()
    buf.close()
    logging.info("RAM freed.")
    logging.info("Monitoring continue...")
    process_messages_for_clearing(sent_messages_info)

# ============================================== گزارش روزانه ===========================================================
def _24H_report(update, context):
        # ۱. متن پیام را در یک متغیر با نام مشخص قرار دهید
    sent_messages_info = []        
    prompt_text = "در حال تهیه گزارش 24 ساعته گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    end_time = get_server_time()
    start_time = end_time - timedelta(hours=24)
    process_messages_for_clearing(sent_messages_info)
    generate_and_send_report(update, context, start_time, end_time, "۲۴ ساعت گذشته")

def _3days_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۳ روز گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=3), datetime.min.time())
    start_time = make_aware(naive_start_time)
    process_messages_for_clearing(sent_messages_info)
    generate_and_send_report(update, context, start_time, end_time, "۳ روز گذشته")
    
def _7day_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۷ روز گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    # محاسبه بازه زمانی ۷ روز گذشته
    end_time = get_server_time()
    # end_time = datetime.now() # یا get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=7), datetime.min.time())
    start_time = make_aware(naive_start_time)
    process_messages_for_clearing(sent_messages_info)
    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۷ روز گذشته")

def _14day_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۱۴ روز گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    # محاسبه بازه زمانی ۱۴ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=14), datetime.min.time())
    start_time = make_aware(naive_start_time)
    process_messages_for_clearing(sent_messages_info)
    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۱۴ روز گذشته")

def _30day_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۳۰ روز گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    # محاسبه بازه زمانی ۳۰ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=30), datetime.min.time())
    start_time = make_aware(naive_start_time)
    process_messages_for_clearing(sent_messages_info)
    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۳۰ روز گذشته")

def _60day_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۶۰ روز گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    # محاسبه بازه زمانی ۶۰ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=60), datetime.min.time())
    start_time = make_aware(naive_start_time)
    process_messages_for_clearing(sent_messages_info)
    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۶۰ روز گذشته")

def _90day_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۹۰ روز گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    # محاسبه بازه زمانی ۹۰ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=90), datetime.min.time())
    start_time = make_aware(naive_start_time)
    process_messages_for_clearing(sent_messages_info)
    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۹۰ روز گذشته") 
    
#--------------------توابع گزارش‌گیری بر اساس هفته و ماه--------------------
def today_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش امروز..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})

    server_now = get_server_time()
    naive_start_time = datetime.combine(server_now.date(), datetime.min.time())
    start_time = make_aware(naive_start_time)
    end_time = server_now
    process_messages_for_clearing(sent_messages_info)
    # logging.info(f"Start time: {start_time}, End time: {end_time}")
    generate_and_send_report(update, context, start_time, end_time, "امروز")
    
def last_week_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش هفته گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    today = get_server_time().date()
    # پیدا کردن شنبه هفته جاری
    last_saturday = today + relativedelta(weekday=SA(-1))
    
    naive_end_time = datetime.combine(last_saturday, datetime.min.time())
    end_time = make_aware(naive_end_time)
    start_time = end_time - timedelta(days=7)
    process_messages_for_clearing(sent_messages_info)
    generate_and_send_report(update, context, start_time, end_time, "هفته گذشته")

def last_2_weeks_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۲ هفته گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    today = get_server_time().date()
    last_saturday = today + relativedelta(weekday=SA(-1))
    naive_end_time = datetime.combine(last_saturday, datetime.min.time())
    end_time = make_aware(naive_end_time)
    start_time = end_time - timedelta(days=14)
    process_messages_for_clearing(sent_messages_info)
    generate_and_send_report(update, context, start_time, end_time, "دو هفته گذشته")

def last_month_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ماه گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    today = get_server_time().date()
    naive_end_time = datetime(today.year, today.month, 1)
    end_time = make_aware(naive_end_time)
    start_time = end_time - relativedelta(months=1)
    process_messages_for_clearing(sent_messages_info)    
    generate_and_send_report(update, context, start_time, end_time, "ماه گذشته")

def last_2_months_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۲ ماه گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    today = get_server_time().date()
    naive_end_time = datetime(today.year, today.month, 1)
    end_time = make_aware(naive_end_time)
    start_time = end_time - relativedelta(months=2)
    process_messages_for_clearing(sent_messages_info)
    generate_and_send_report(update, context, start_time, end_time, "دو ماه گذشته")

def last_3_months_report(update, context):
    sent_messages_info = []
    prompt_text = "در حال تهیه گزارش ۳ ماه گذشته..."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({'id': sent_msg.message_id, 'text': prompt_text})
    today = get_server_time().date()
    naive_end_time = datetime(today.year, today.month, 1)
    end_time = make_aware(naive_end_time)
    start_time = end_time - relativedelta(months=3)
    process_messages_for_clearing(sent_messages_info)
    generate_and_send_report(update, context, start_time, end_time, "سه ماه گذشته")
     
# ====================== توابع قالب‌بندی پیام‌ها ======================
def format_pending_order_filled(deal, order):
    """قالب پیام برای فعال شدن اردر بر اساس deal و order"""
    side = "🔵 Buy" if deal.type == mt5.DEAL_TYPE_BUY else "🔴 Sell"
    comment_text = f"`Comment : {order.comment}\n\n`" if order.comment else ""
    utc_time = datetime.fromtimestamp(deal.time_msc / 1000, tz=pytz.utc)
    broker_tz = pytz.timezone(BROKER_TIMEZONE)
    broker_dt_object = utc_time.astimezone(broker_tz)
    milliseconds = deal.time_msc % 1000
    broker_time_str = f"{broker_dt_object.strftime('%y/%m/%d..%H:%M:%S')}.{milliseconds:03d}"
    # --- بخش جدید: گرفتن اطلاعات حساب ---
    account_info = mt5.account_info()
    balance_equity_line = f"`Bal|Eq  : {account_info.balance:,.2f}|{account_info.equity:,.2f}`\n" if account_info else ""
    broker_account_line = f"`{account_info.company}|Acc: {account_info.login}`\n" if account_info else ""
    
    return (
        f"**----- Order Filled -----**\n\n"
        f"{broker_account_line}"
        f"{balance_equity_line}"
        f"`Symbol  : `{deal.symbol}\n"
        f"`Type    : {get_order_type_str(order)}`\n"
        f"`Price   : {deal.price}`\n"
        f"`Lots    : {deal.volume}`\n"
        f"`ID      : {deal.position_id}`\n"
        f"{comment_text}"
        f"`{broker_time_str}`"
    )
def format_position_closed(deal, original_order_comment, initial_volume, is_complete_close, total_position_profit, total_position_commission, total_position_swap, total_sign):
    """قالب پیام برای بسته شدن پوزیشن با کامنت اصلی"""
    side = "🔴 Sell" if deal.type == mt5.DEAL_TYPE_BUY else "🔵 Buy"
    result = "ℹ️ Manually Closed"
    # ==== متد 1 ====
    # if deal.reason == 3 or '[tp' in deal.comment.lower(): result = "✅ TP"
    # elif deal.reason == 4 or '[sl' in deal.comment.lower(): result = "❌ SL"
    
    # ==== متد 2 ====
    if '[tp' in deal.comment.lower(): result = "✅ TP"
    elif '[sl' in deal.comment.lower(): result = "❌ SL"
    
    # ==== متد 3 ====
    # # یک معامله فقط زمانی TP است که دلیل آن TP بوده و حجم آن برابر با حجم اولیه باشد (خروج کامل)
    # if (deal.reason == 3 or '[tp' in deal.comment.lower()) and deal.volume == initial_volume:
    #     result = "✅ TP"
    # # یک معامله فقط زمانی SL است که دلیل آن SL بوده و حجم آن برابر با حجم اولیه باشد
    # elif (deal.reason == 4 or '[sl' in deal.comment.lower()) and deal.volume == initial_volume:
    #     result = "❌ SL"
    
    comment_text = f"`Comment: {original_order_comment}`\n\n" if original_order_comment else ""
    utc_time = datetime.fromtimestamp(deal.time_msc / 1000, tz=pytz.utc)
    broker_tz = pytz.timezone(BROKER_TIMEZONE)
    broker_dt_object = utc_time.astimezone(broker_tz)
    milliseconds = deal.time_msc % 1000
    broker_time_str = f"{broker_dt_object.strftime('%y/%m/%d..%H:%M:%S')}.{milliseconds:03d}"
    # --- بخش جدید: گرفتن اطلاعات حساب ---
    account_info = mt5.account_info()
    balance_equity_line = f"`Bal|Eq : {account_info.balance:,.2f}|{account_info.equity:,.2f}`\n" if account_info else ""
    # --- بخش جدید: ساخت خط اطلاعات بروکر و حساب ---
    broker_account_line = f"`{account_info.company} | {account_info.login}`\n" if account_info else ""
    # --- بخش جدید: نمایش هوشمند حجم معامله ---
    commission_pos = 0.0
    swap_pos = 0.0
    if deal.volume < initial_volume and initial_volume > 0:
        if is_complete_close:
            # اگر خروج کامل باشد
            position_close_title = f"**⚔️ Position Closed (Complete)**"
            lots         = f"{deal.volume:.2f}/{initial_volume:.2f}"
            # اگر خروج کامل بود، سود کل را هم نمایش بده
            p_sign       = "+" if deal.profit > 0 else ""
            profit       = f"{p_sign}{deal.profit:,.2f}$ ({total_sign}{total_position_profit:,.2f}$)"
            commission_pos   = f"{total_position_commission:,.2f}$" if total_position_commission else "0$"
            swap_pos         = f"{total_position_swap:,.2f}$" if total_position_swap else "0$"
        else:
            # اگر حجم بسته شده کمتر از حجم اولیه بود (خروج بخشی)
            position_close_title = f"**⚔️ Position Closed (Partial)**"
            lots       = f"{deal.volume:.2f}/{initial_volume:.2f}"
            # در غیر این صورت، فقط سود این بخش را نمایش بده
            p_sign     = "+" if deal.profit > 0 else ""
            profit     = f"{p_sign}{deal.profit:,.2f} $"

    else: # یعنی پارشیال نبوده
        # در غیر این صورت، فقط حجم بسته شده را نمایش بده
        position_close_title = f"**⚔️ Position Closed**"
        lots       = f"{deal.volume:.2f}"
        # در غیر این صورت، فقط سود این بخش را نمایش بده
        p_sign     = "+" if deal.profit > 0 else ""
        profit     = f"{p_sign}{deal.profit:,.2f} $"
        commission_pos   = f"{total_position_commission:,.2f}$" if total_position_commission else "0$"
        swap_pos         = f"{total_position_swap:,.2f}$" if total_position_swap else "0$"

    return (
        f"{position_close_title}\n\n"
        f"{broker_account_line}"
        f"{balance_equity_line}"
        f"`Symbol : `{deal.symbol}\n"
        f"`Side   : {side}`\n"
        f"`Result : {result}`\n"
        f"`Profit : {profit}`\n"
        f"`Comm/sw: {commission_pos}/{swap_pos}`\n"
        f"`Lots   : {lots}`\n"
        f"`ID     : {deal.position_id}`\n"
        f"{comment_text}"
        f"`{broker_time_str}`"
    )
def get_order_type_str(order):
    """یک رشته خوانا از نوع اردر برمی‌گرداند"""
    type_map = {
        mt5.ORDER_TYPE_BUY_LIMIT:  "Buy Limit",
        mt5.ORDER_TYPE_SELL_LIMIT: "Sell Limit",
        mt5.ORDER_TYPE_BUY_STOP:   "Buy Stop",
        mt5.ORDER_TYPE_SELL_STOP:  "Sell Stop",
    }
    return type_map.get(order.type, "Pending")

def get_server_time():
    """زمان سرور بروکر را با منطقه زمانی صحیح برمی‌گرداند."""
    last_tick = mt5.symbol_info_tick("BTCUSD")
    if last_tick and last_tick.time > 0:
        utc_time = datetime.fromtimestamp(last_tick.time, tz=pytz.utc)
        broker_tz = pytz.timezone(BROKER_TIMEZONE)
        return utc_time.astimezone(broker_tz)
    else:
        return None
    
def make_aware(dt):
    """یک زمان ساده را به زمان آگاه از منطقه زمانی بروکر تبدیل می‌کند."""
    broker_tz = pytz.timezone(BROKER_TIMEZONE)
    return broker_tz.localize(dt)

# ====================== تابع پاک کردن هشدارها ======================
def clear_alerts(update, context):
    """
    پیام‌های هشدار ذخیره شده را پاک می‌کند.
    در صورت بروز خطای شبکه، شناسه پیام را برای تلاش مجدد نگه می‌دارد.
    """
    if not alert_message_ids:
        update.message.reply_text("هیچ پیام غیر ضروری برای پاک کردن وجود ندارد.")
        return

    logging.info(f"Attempting to delete {len(alert_message_ids)} messages...")
    update.message.reply_text(f"در حال پاک‌سازی {len(alert_message_ids)} پیام غیر ضروری...")
    
    deleted_count = 0
    failed_permanently_count = 0
    failed_temporarily_count = 0

    # از یک کپی پیمایش می‌کنیم تا بتوانیم لیست اصلی را در حین کار ویرایش کنیم
    for msg_id in list(alert_message_ids):
        try:
            bot.delete_message(chat_id=CHAT_ID, message_id=msg_id)
            # اگر حذف موفق بود، از لیست اصلی هم پاک کن
            alert_message_ids.remove(msg_id)
            remove_id_from_db(msg_id)
            deleted_count += 1
            time.sleep(0.01)  # جلوگیری از محدودیت‌های تلگرام

        except BadRequest as e:
            # این خطاها یعنی پیام قابل حذف نیست (قدیمی، پاک شده و...)
            # پس دیگر برای حذفش تلاش نمی‌کنیم و از لیست پاکش می‌کنیم
            logging.warning(f"Could not delete message ID {msg_id}, Permanent error: {e}")
            alert_message_ids.remove(msg_id)
            remove_id_from_db(msg_id)
            failed_permanently_count += 1

        except NetworkError as e:
            # این خطا یعنی مشکل از شبکه است، پس شناسه را برای تلاش بعدی نگه می‌داریم
            logging.warning(f"Could not delete message ID {msg_id}, Network error: {e}")
            failed_temporarily_count += 1

        except Exception as e:
            # برای خطاهای ناشناخته، برای احتیاط شناسه را نگه می‌داریم
            logging.error(f"Could not delete message ID {msg_id}, Unknown error: {e}")
            failed_temporarily_count += 1

    # ساخت پیام نهایی برای کاربر
    # confirmation_parts = [f"عملیات پاک‌سازی انجام شد."]
    # confirmation_parts.append(f"✅ {deleted_count} پیام با موفقیت حذف شد.")
    confirmation_parts = [f"✅ {deleted_count} پیام با موفقیت حذف شد."]
    if failed_permanently_count > 0:
        confirmation_parts.append(f"❌ {failed_permanently_count} پیام قابل حذف نبود (قدیمی یا ناموجود).")

    if failed_temporarily_count > 0:
        confirmation_parts.append(f"⚠️ {failed_temporarily_count} پیام به دلیل خطای شبکه حذف نشد (در اجرای بعدی دوباره تلاش خواهد شد).")

    confirmation_message = "\n".join(confirmation_parts)
    update.message.reply_text(confirmation_message)
    
    remaining_count = len(alert_message_ids)
    logging.info(f"Deleted:{deleted_count}, F Permn:{failed_permanently_count}, F Tempo:{failed_temporarily_count}, Remaining:{remaining_count}.")

def process_messages_for_clearing(sent_messages_info):
    """
    یک لیست از اطلاعات پیام‌های ارسال شده را گرفته، آن‌ها را بررسی کرده 
    و شناسه‌های پیام‌های غیرمهم را به لیست اصلی حذفی‌ها اضافه می‌کند.
    """
    # logging.info(f"Processing {len(sent_messages_info)} sent messages for clearing...")
    for msg_info in sent_messages_info:
        # شرط را برای هر پیام ذخیره شده اجرا می‌کنیم
        is_important = any(keyword in msg_info['text'] for keyword in KEYWORDS_TO_KEEP)
        
        # اگر پیام مهم نبود، شناسه‌اش را به لیست اصلی حذفی‌ها اضافه کن
        if not is_important:
            alert_message_ids.append(msg_info['id'])
            add_id_to_db(msg_info['id']) # شناسه را در پایگاه داده هم ذخیره می‌کند
            # logging.info(f"Message ID {msg_info['id']} marked for clearing.")

# ====================== تابع اصلی مانیتورینگ ======================
def main():
    # --- تغییر کلیدی: راه‌اندازی وب‌سرور در یک ترد پس‌زمینه ---
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    logging.info("Alert Server is running.")

        # این حلقه تا زمانی که اینترنت آماده شود، ادامه دارد
    while True:
        # تلاش برای راه‌اندازی شنونده تلگرام
        try:
            global updater
            updater = Updater(TOKEN, use_context=True)
            dispatcher = updater.dispatcher
            # --- بخش جدید: ساخت و ثبت مکالمه برای گزارش سفارشی ---
            conv_handler = ConversationHandler(
                entry_points=[CommandHandler('custom_report', custom_report_start)],
                states={
                    START_DATE: [MessageHandler(Filters.text & ~Filters.command, received_start_date)],
                    END_DATE: [MessageHandler(Filters.text & ~Filters.command, received_end_date)],
                },
                fallbacks=[CommandHandler('cancel', cancel_conversation)],
            )
            
            dispatcher.add_handler(conv_handler)
            dispatcher.add_handler(CommandHandler("clear", clear_alerts))
            dispatcher.add_handler(CommandHandler("time", _24H_report))
            dispatcher.add_handler(CommandHandler("3days", _3days_report))
            dispatcher.add_handler(CommandHandler("7day", _7day_report))
            dispatcher.add_handler(CommandHandler("14day", _14day_report))
            dispatcher.add_handler(CommandHandler("30day", _30day_report))
            dispatcher.add_handler(CommandHandler("60day", _60day_report))
            dispatcher.add_handler(CommandHandler("90day", _90day_report))
            dispatcher.add_handler(CommandHandler("today", today_report))
            dispatcher.add_handler(CommandHandler("lastweek", last_week_report))
            dispatcher.add_handler(CommandHandler("last2weeks", last_2_weeks_report))
            dispatcher.add_handler(CommandHandler("lastmonth", last_month_report))
            dispatcher.add_handler(CommandHandler("last2months", last_2_months_report))
            dispatcher.add_handler(CommandHandler("last3months", last_3_months_report))
            dispatcher.add_error_handler(handle_error)      
            # اگر همه چیز موفق بود، از حلقه راه‌اندازی خارج می‌شویم
            updater.start_polling()# راه اندازی شنونده
            logging.info("Listener started successfully.")
            #logging.info("Telegram connection successful. Starting main operations.")
            break

        except Exception as e:
            # اگر اینترنت وصل نبود، خطا را چاپ کرده و حلقه را تکرار می‌کنیم
            logging.error(f"initial listener run fail Retrying in 10 seconds...")
            time.sleep(10)
            continue
        # +++ این دو خط را اضافه کنید +++
    setup_database() # پایگاه داده را آماده می‌کند
    global alert_message_ids
    alert_message_ids = load_ids_from_db() # شناسه‌های قدیمی را بارگذاری می‌کند

    is_connected = False
    disconnect_time = None
    last_check_time = None # در ابتدا خالی است و پس از اولین اتصال مقداردهی می‌شود
    processed_deals = set()

    # برای شروع تمیز، معاملات یک ساعت اخیر را بر اساس زمان سرور نادیده می‌گیریم
    if mt5.initialize(path=MT5_PATH):
#------------------
        server_time_now = get_server_time()
        # اگر زمان سرور در دسترس بود، ادامه بده
        if server_time_now:
            last_check_time = server_time_now
            initial_deals = mt5.history_deals_get(server_time_now - timedelta(hours=1), server_time_now)
            if initial_deals:
                processed_deals.update(d.ticket for d in initial_deals)
        else:
            # اگر زمان سرور در دسترس نبود، بعدا در حلقه اصلی تلاش می‌کنیم
            last_check_time = None
        #mt5.shutdown() # اتصال اولیه را می‌بندیم
    else:
        # اگر اتصال اولیه برقرار نشد، بعدا تلاش می‌کنیم
        last_check_time = None
#------------------
    while True:
        if is_connected:
            try:
                # تغییر: استفاده از زمان سرور برای گرفتن معاملات جدید
                current_time = get_server_time()
                # اگر نتوانستیم زمان سرور را بگیریم، آن را یک قطعی در نظر می‌گیریم
                if current_time is None:
                    raise ConnectionError("Failed to get server time. retry...")
                new_deals = mt5.history_deals_get(last_check_time, current_time)
                last_check_time = current_time

                if new_deals:
                    for deal in new_deals:
                        if deal.ticket in processed_deals:
                            continue

                        if deal.entry == mt5.DEAL_ENTRY_IN:
                            order = mt5.history_orders_get(ticket=deal.order)
                            if order and order[0].type in [2,3,4,5]:
                                msg = format_pending_order_filled(deal, order[0])
                                send_telegram(msg)
                        
                        elif deal.entry == mt5.DEAL_ENTRY_OUT:
                            original_comment = ""
                            initial_volume = 0.0 # متغیر جدید برای حجم اولیه
                            # --- بخش جدید: محاسبه حجم کل بسته شده ---
                            total_closed_volume = 0.0
                            is_complete_close = False
                           
                            # --- بخش جدید: محاسبه سود کل پوزیشن ---
                            total_position_profit = 0.0
                            total_position_commission = 0.0
                            total_position_swap = 0.0
                            position_deals = mt5.history_deals_get(position=deal.position_id)
                            if position_deals:
                                for opening_deal in position_deals:
                                    if opening_deal.entry == mt5.DEAL_ENTRY_IN:
                                        # حجم و کامنت را از معامله ورودی استخراج می‌کنیم
                                        initial_volume = opening_deal.volume
                                        opening_order = mt5.history_orders_get(ticket=opening_deal.order)
                                        # total_position_profit += opening_deal.commission + opening_deal.swap
                                        total_position_commission +=  opening_deal.commission
                                        total_position_swap +=  opening_deal.swap
                                        
                                        if opening_order:
                                            original_comment = opening_order[0].comment
                                            
                                    # جمع زدن حجم تمام معاملات خروجی
                                    if opening_deal.entry == mt5.DEAL_ENTRY_OUT and opening_deal.time_msc <= deal.time_msc:
                                        total_closed_volume += opening_deal.volume
                                        # سود تمام معاملات خروجی را جمع می‌زنیم
                                        total_position_profit += opening_deal.profit# + opening_deal.commission + opening_deal.swap
                                        total_position_commission +=  opening_deal.commission
                                        total_position_swap +=  opening_deal.swap
                                        
                                total_sign = "+" if total_position_profit > 0 else ""
                                # چک می‌کنیم که آیا حجم کل بسته شده با حجم اولیه برابر است
                                # (با یک خطای کوچک برای اعداد اعشاری)
                                if abs(total_closed_volume - initial_volume) < 0.001:
                                    is_complete_close = True
                            # ارسال اطلاعات کامل به تابع قالب‌بندی
                            msg = format_position_closed(deal, original_comment, initial_volume, is_complete_close, total_position_profit, total_position_commission, total_position_swap, total_sign)
                            send_telegram(msg)
                        
                        processed_deals.add(deal.ticket)
                
                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                logging.critical(f"Connection to MT5 lost: {e}")
                send_telegram("⚠️ Connection to MT5 lost. Attempting to reconnect...")
                is_connected = False
                disconnect_time = time.time()
                mt5.shutdown()
                time.sleep(RECONNECT_DELAY)
                continue
        else:
            # --- حالت قطع شده: تلاش برای اتصال مجدد ---
            logging.info("Connecting to MetaTrader 5...")

            if disconnect_time and (time.time() - disconnect_time > OVERALL_TIMEOUT):
                logging.error(f"Could not reconnect within {int(OVERALL_TIMEOUT/60)} minutes. Shutting down for good.")
                send_telegram(f"❌ Could not reconnect to MT5 for {int(OVERALL_TIMEOUT/60)} minutes. Bot is shutting down.")
                break 

            if mt5.initialize(path=MT5_PATH):
                if disconnect_time:
                    logging.info("Reconnected to MT5 successfully!")
                    send_telegram("✅ Reconnected to MT5. Monitoring resumed.")
                else: # در غیر این صورت این اولین اتصال است
                    logging.info("Connected to MT5 successfully!")
                    send_telegram("✅ *Bot is running*\nMonitoring...")

                is_connected = True
                disconnect_time = None
                # تغییر: ریست کردن زمان پس از اتصال بر اساس زمان سرور
                last_check_time = get_server_time()
                
                # این بخش دیگر ضروری نیست چون ما تاریخچه را چک می‌کنیم نه پوزیشن‌های باز را
                positions_result = mt5.positions_get()
                last_known_positions = {p.ticket: p for p in positions_result} if positions_result else {}
                # logging.info(f"Ignoring {len(last_known_positions)} existing position(s).")
                # send_telegram(f"{len(last_known_positions)} existing position(s).")
                # --- بخش جدید: ساخت و ارسال لیست پوزیشن‌های باز ---
                logging.info(f"Ignoring {len(last_known_positions)} existing position(s).")
                logging.info("Monitoring...")
                # اگر پوزیشنی باز بود، لیست آنها را تهیه و ارسال کن
                if last_known_positions:
                    position_lines = []
                    # ساخت هدر پیام
                    header = f"{len(last_known_positions)} position exist"
                    position_lines.append(header)
                    
                    # اضافه کردن جزئیات هر پوزیشن به لیست
                    for ticket, position in last_known_positions.items():
                        side = "Buy" if position.type == mt5.POSITION_TYPE_BUY else "Sell"
                        lot  = position.volume
                        profit = position.profit
                        p_sign = "+" if profit > 0 else ""
                        #header = f"Symbol  |Side  |Lots   |Profit"
                        line = f"{position.symbol:<8}|{side:>5} |{lot:>6.2f} | {p_sign}{profit:,.2f} $"
                        # position_lines.append(header)
                        position_lines.append(line)
                    
                    # ترکیب همه خطوط در یک پیام واحد و ارسال آن
                    full_message = "\n".join(position_lines)
                    send_telegram(full_message)
                else:
                    # اگر هیچ پوزیشنی باز نبود، فقط یک پیام ساده بفرست
                    send_telegram("0 existing position(s).")
            else:
                logging.error(f"Connection failed. Retrying in {RECONNECT_DELAY} seconds...")
                time.sleep(RECONNECT_DELAY)
                #mt5.initialize(path=MT5_PATH)#فقط به خاطر اینکه متاتریدر اگه اجرا نبود اجرا بشه

    logging.info("Script has been shut down.")
    updater.stop()
    
# ====================== اجرای اسکریپت ======================
if __name__ == "__main__":
    
    # --- بخش جدید: حلقه برای اطمینان از تشخیص منطقه زمانی ---
    while True:
        BROKER_TIMEZONE = determine_broker_timezone()
        
        # اگر تشخیص منطقه زمانی موفق بود، از حلقه خارج شو
        if BROKER_TIMEZONE is not None:
            break
        
        # اگر ناموفق بود، ۱۰ ثانیه صبر کرده و دوباره تلاش کن
        logging.info("Retrying timezone detection in 10 seconds...")
        time.sleep(10)
    # +++ حلقه نگهبان برای اجرای بی‌پایان اسکریپت +++
    while True:
        # حالا که منطقه زمانی با موفقیت پیدا شده، برنامه اصلی را اجرا کن           
        try:
            main()
        except KeyboardInterrupt:
            send_telegram("ℹ️ *Script Stopped Manually*")
            logging.info("Script stopped by user.")
            break # <--- این break برای خروج از حلقه نگهبان ضروری است
        
        except Exception as e:
            # اول خطای اصلی را در لاگ کنسول ثبت کن
            # logging.critical(f"Critical unhandled error caught: {e}")
            logging.critical(f"Critical Error: {e}")
            # حالا سعی کن به تلگرام هم خبر بدهی، اما نگذار این تلاش خودش باعث کرش شود
            try:
                send_telegram(f"❌ *CRITICAL ERROR*\nBot has crashed!\nError: {e}")
            except Exception as report_error:
                # اگر حتی ارسال گزارش خطا هم شکست خورد، فقط در لاگ کنسول بنویس
                logging.error(f"Could not send the final crash notification to Telegram: {report_error}")
            
            # کمی صبر می‌کنیم تا از حلقه کرش سریع (crash-loop) جلوگیری شود
            logging.info("Restarting the script...")
            time.sleep(30)
            # سپس حلقه نگهبان دوباره main() را اجرا می‌کند
            
            
    if updater and updater.running:
        # تغییر ۳: در نهایت، چه با خطا و چه با Ctrl+C، شنونده را متوقف می‌کنیم
        logging.info("{wait}Stopping updater...")
        updater.stop()
        logging.info("Updater stopped.")
    if mt5.terminal_info():
        mt5.shutdown()
    logging.info("Script exited gracefully.")


