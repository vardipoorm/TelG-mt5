import MetaTrader5 as mt5
import time
import logging
import pytz
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
from datetime import datetime, timedelta # تغییر ضروری: timedelta اضافه شد
from telegram.ext import Updater, CommandHandler # تغییر ضروری: کتابخانه‌های شنونده اضافه شدند

# تا اینجا تصویر خوب کار میکنه

# ====================== ساکت کردن گزارشگرهای پیش‌فرض تلگرام ======================
# این بخش گزارش‌های خطای شبکه‌ای پیش‌فرض کتابخانه تلگرام و وابستگی‌های آن را غیرفعال می‌کند
# تا فقط مدیر خطای شخصی ما (handle_error) پیام‌ها را چاپ کند.
logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)
logging.getLogger('telegram.vendor.ptb_urllib3.urllib3.connectionpool').setLevel(logging.CRITICAL)
logging.getLogger('telegram.ext.updater').setLevel(logging.CRITICAL)

# =====================تابع برگرداندن منطقه زمانی بروکر =====================
def determine_broker_timezone():
    """
    اختلاف زمانی سرور بروکر با UTC را محاسبه کرده و رشته منطقه زمانی صحیح را برمی‌گرداند.
    """
    print("Determining broker timezone...")
    if not mt5.initialize(path=MT5_PATH):
        print("Could not connect to MT5 to determine timezone.")
        return None

    server_tick = mt5.symbol_info_tick("BTCUSD")
    if not server_tick or server_tick.time == 0:
        print("Could not get server time from tick.")
        # mt5.shutdown()
        return None
    
    # زمان سرور و زمان جهانی را به صورت "آگاه از منطقه زمانی" ایجاد می‌کنیم
    server_time = datetime.fromtimestamp(server_tick.time, tz=pytz.utc)
    # print(f"Server time (UTC): {server_time}")
    utc_now = datetime.now(pytz.utc)
    # print(f"Current UTC time: {utc_now}")

    # اختلاف را به ساعت گرد می‌کنیم
    time_difference_hours = (server_time - utc_now).total_seconds() / 3600.0
    # print(f"Detected timezone difference (hours): {time_difference_hours}")
    offset = round(time_difference_hours) # به نزدیک‌ترین ساعت کامل گرد می‌کنیم
    
    # print(f"Detected timezone offset: UTC{offset:+}")

    # ساخت رشته صحیح Etc/GMT (علامت برعکس است)
    offset_sign = "+" if offset <= 0 else "-"
    # timezone_str = f"Etc/GMT{offset_sign}{abs(offset)}"
    timezone_str = "Etc/GMT+0"
    # mt5.shutdown()
    print(f"Timezone automatically set to: {timezone_str}")
    return timezone_str

# ========================= تنظیمات اصلی =========================
TOKEN = ""

CHAT_ID = 

CHECK_INTERVAL = 5 # فاصله زمانی بین هر چک در حالت عادی

# ---  تنظیمات اتصال مجدد به متاتریدر ---
RECONNECT_DELAY = 10  # هر چند ثانیه برای اتصال مجدد تلاش کند
OVERALL_TIMEOUT = 6000 # مهلت زمانی نهایی به ثانیه (600 ثانیه = 10 دقیقه)

# --- تنظیمات تلاش مجدد برای ارسال تلگرام ---
RETRY_COUNT = 2000
RETRY_DELAY = 2

# --- مسیر متاتریدر خاص ---
MT5_PATH = ***REMOVED***" "
BROKER_TIMEZONE = None

# ====================== اتصال به تلگرام ======================
bot = Bot(token=TOKEN)
updater = None

def send_telegram(text):
    # ۱. تلاش اول انجام می‌شود
    try:
        bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
        return True # اگر موفق بود، کار تمام است

    except Exception as e:
        # ۲. اگر تلاش اول ناموفق بود، فقط یک بار هشدار ارسال می‌شود
        print(f"!!! Telegram Send Error retrying... (1/{RETRY_COUNT})")#: {e}")
        try:
            bot.send_message(chat_id=CHAT_ID, text="⚠️ Network unstable.", parse_mode='Markdown')
        except Exception as e_warn:
            print(f"⚠️Could not send the warning message: {e_warn}")

    # ۳. حلقه تلاش‌های مجدد شروع می‌شود (چون تلاش اول ناموفق بود)
    for i in range(1, RETRY_COUNT): 
        time.sleep(RETRY_DELAY)
        #print(f"!!! Telegram Send Error retrying... ({i+1}/{RETRY_COUNT})")
        try:
            # تلاش برای ارسال پیام اصلی
            bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
            return True
        except Exception as e:
            print(f"!!! Telegram Send Error retrying... ({i+1}/{RETRY_COUNT})")

    # اگر همه تلاش‌ها ناموفق بود
    print("❌Could not send message to Telegram after all retries.")
    #send_telegram("❌ Failed to send a message after multiple retries.")
    bot.send_message(chat_id=CHAT_ID, text="❌ Failed to send a message after multiple retries.", parse_mode='Markdown')
    return False
#----------------- تابع گزارش خطای شبکه listening -------------------
def handle_error(update, context):
    """خطاهای مربوط به شنونده تلگرام را مدیریت کرده و یک پیام ساده چاپ می‌کند."""
    # ما فقط برای خطاهای مربوط به شبکه پیام ساده چاپ می‌کنیم تا خطاهای مهم دیگر پنهان نشوند
    if "urllib3 HTTPError" in str(context.error) or "SSLEOFError" in str(context.error):
        print("Network error occurred while listening for updates...")
    else:
        # برای خطاهای دیگر، جزئیات را چاپ می‌کنیم تا در صورت نیاز بتوانید آنها را رفع کنید
        print(f"listener unhandled error: {context.error}")

# ====================== توابع گزارش‌گیری ======================
def generate_and_send_report(update, context, start_time, end_time, title):
    """موتور اصلی برای ساخت و ارسال تمام گزارش‌ها"""
    terminal_info = mt5.terminal_info()
    if not terminal_info or not terminal_info.connected:
        update.message.reply_text("اسکریپت به متاتریدر متصل نیست. لطفاً چند لحظه دیگر دوباره تلاش کنید.")
        return

    deals = mt5.history_deals_get(start_time, end_time)

    if not deals:
        update.message.reply_text(f"در بازه زمانی گزارش ({title}) هیچ معامله‌ای یافت نشد.")
        return

    report_lines, total_profit, closed_trades_count, win_count = [], 0.0, 0, 0
    trade_counter = 1
    commison = 0.0
    swap = 0.0
    for deal in deals:
        total_profit += deal.commission + deal.swap
        commison += deal.commission
        swap += deal.swap
        if deal.entry == mt5.DEAL_ENTRY_OUT:
            closed_trades_count += 1
            total_profit += deal.profit
            if deal.profit >= 0:
                win_count += 1
            utc_time = datetime.fromtimestamp(deal.time, tz=pytz.utc)
            broker_tz = pytz.timezone(BROKER_TIMEZONE)
            broker_dt_object = utc_time.astimezone(broker_tz)
            trade_date = broker_dt_object.strftime('%y/%m/%d %H:%M:%S')
            line = f"{trade_counter:02d}-{deal.symbol}|{deal.volume:.2f}|{deal.profit:>8,.2f}|{trade_date}"
            report_lines.append(f"`{line}`")
            trade_counter += 1

    if not report_lines:
        update.message.reply_text(f"در بازه زمانی گزارش ({title}) هیچ پوزیشنی بسته نشده است.")
        return

    win_rate = (win_count / closed_trades_count * 100) if closed_trades_count > 0 else 0
    total_profit_sign = "✅" if total_profit >= 0 else "🔻"

# --- بخش جدید: محاسبه دقیق سود و رشد ---
    account_info = mt5.account_info()
    profit_line = ""
    growth_line = ""

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
        if abs((end_time - get_server_time()).total_seconds()) < 5:
            # این یک گزارش تا لحظه ی حال است، از فرمول ساده استفاده کن
            starting_balance_period = account_info.balance - total_profit
            # --- بخش جدید: گرفتن اطلاعات بالانس و اکوییتی ---
            account_info = mt5.account_info()
            balance_equity_line = f"**موجودی ابتدای بازه:**`{starting_balance_period:,.2f}`\n**موجودی(حال):**`{account_info.balance:>8.2f}`**|اکوییتی(حال):**`{account_info.equity:,.2f}`\n" if account_info else ""
            display_end_time = end_time

        else:
            # این یک گزارش تاریخ خاص است، از فرمول پیچیده‌تر استفاده کن
            # ابتدا سود معاملاتی که بعد از بازه گزارش انجام شده را پیدا می‌کنیم
            deals_after_period = mt5.history_deals_get(end_time, get_server_time())
            profit_after_period = 0.0
            if deals_after_period:
                for d in deals_after_period:
                    if d.entry in (mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_OUT):
                        profit_after_period += d.profit + d.commission + d.swap
            
            # بالانس در انتهای بازه = بالانس فعلی - سود معاملات بعدی
            balance_at_period_end = account_info.balance - profit_after_period
            
            # بالانس ابتدای بازه = بالانس انتهای بازه - سود خود بازه
            starting_balance_period = balance_at_period_end - total_profit
            
            # --- گرفتن اطلاعات بالانس تاریخی ---
            balance_equity_line = f"**موجودی ابتدای بازه:** `{starting_balance_period:,.2f}`\n**موجودی انتهای بازه:**`{balance_at_period_end:,.2f}`\n" if balance_at_period_end and starting_balance_period else ""
            # برای گزارش‌های تاریخی، یک روز از تاریخ پایان کم می‌کنیم تا بازه درست نمایش داده شود
            display_end_time = end_time - timedelta(days=1)    

        profit_line = f"**سود اکانت(حال):**`{true_total_account_profit:>8.2f}$`|**سود بازه:** `{total_profit:,.2f}$`\n"

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
            period_growth_percentage = (total_profit / starting_balance_period) * 100
        period_growth_sign = "+" if period_growth_percentage >= 0 else ""
        period_growth_str = f"{period_growth_sign}{period_growth_percentage:.2f}%"

        growth_line = f"**درصد رشد اکانت(حال):**`{total_growth_str}`|**درصد رشد بازه:**`{period_growth_str}`\n"

        summary = (
            f"**📊 گزارش {title}**\n"
            f"_{start_time.strftime('%Y/%m/%d')} - {display_end_time.strftime('%Y/%m/%d')}_\n\n"
            f"{balance_equity_line}"
            f"{profit_line}"
            f"{growth_line}"
            f"کمیسیون بازه:`{commison:.2f}`|سواپ بازه:`{swap:.2f}`\n"
            f"**وین ریت بازه:**`{win_rate:.2f}%` ({win_count}/{closed_trades_count})\n"
            f"**ت. پوزیشن‌های بازه:**`{closed_trades_count}`\n"
            f"-----------------------------------"
        )
    
    update.message.reply_text(summary, parse_mode='Markdown')
    time.sleep(1) 

    # --- بخش جدید: اضافه کردن هدر ---
    update.message.reply_text("#N| Symbol | lot   |          Profit | Date")
    
    CHUNK_SIZE = 40
    for i in range(0, len(report_lines), CHUNK_SIZE):
        chunk = report_lines[i:i + CHUNK_SIZE]
        message_part = "\n".join(chunk)
        update.message.reply_text(message_part, parse_mode='Markdown')
        time.sleep(1)

    # فراخوانی تابع برای ساخت و ارسال نمودار
    create_and_send_growth_chart(update, context, deals, starting_balance_period, title)   
        
    update.message.reply_text("End report.\nmonitoring continue...")

# ====================== رسم نمودار رشد ======================
def create_and_send_growth_chart(update, context, deals, starting_balance, title):
    """نمودار رشد حساب را ساخته و به تلگرام ارسال می‌کند."""
    print("Creating growth chart...")
    
    # # ۱. آماده‌سازی داده‌ها
    # dates = []
    # cumulative_profit = []
    # current_equity = starting_balance

    # # مرتب کردن معاملات بر اساس زمان
    sorted_deals = sorted([d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT], key=lambda x: x.time)

    # if not sorted_deals:
    #     print("No closing deals to chart.")
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

    if not sorted_deals:
        print("No closing deals to chart.")
        return

    # اضافه کردن نقطه شروع نمودار (معامله شماره صفر)
    trade_numbers.append(0)
    cumulative_profit.append(starting_balance)

    # محاسبه سود تجمعی برای هر معامله
    for i, deal in enumerate(sorted_deals):
        current_equity += deal.profit + deal.commission + deal.swap
        trade_numbers.append(i + 1) # شماره معامله (۱، ۲، ۳، ...)
        cumulative_profit.append(current_equity)

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
    plt.scatter(x_original, y_original, color=line_color, s=4) # s=5 for marker size

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
    print("Sending chart to Telegram...")
    update.message.reply_photo(photo=buf, caption=f"نمودار رشد: {title}")
    
    # بستن نمودار برای آزاد کردن حافظه
    plt.close()
    buf.close()
    print("RAM freed.")
    print("Monitoring continue...")
# ============================================== گزارش روزانه ===========================================================
def _24H_report(update, context):
    update.message.reply_text("در حال تهیه گزارش 24 ساعته گذشته...")
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=1), datetime.min.time())
    start_time = make_aware(naive_start_time)
    generate_and_send_report(update, context, start_time, end_time, "۲۴ ساعت گذشته")

def _3days_report(update, context):
    update.message.reply_text("در حال تهیه گزارش ۳ روز گذشته...")
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=2), datetime.min.time())
    start_time = make_aware(naive_start_time)
    generate_and_send_report(update, context, start_time, end_time, "۳ روز گذشته")
    
def _7day_report(update, context):
    """گزارش ۷ روز گذشته را با استفاده از موتور گزارش‌ساز مرکزی تهیه می‌کند."""
    update.message.reply_text("در حال تهیه گزارش ۷ روز گذشته...")
    
    # محاسبه بازه زمانی ۷ روز گذشته
    end_time = get_server_time()
    # end_time = datetime.now() # یا get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=7), datetime.min.time())
    start_time = make_aware(naive_start_time)
    
    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۷ روز گذشته")

def _14day_report(update, context):
    """گزارش ۱۴ روز گذشته را با استفاده از موتور گزارش‌ساز مرکزی تهیه می‌کند."""
    update.message.reply_text("در حال تهیه گزارش ۱۴ روز گذشته...")

    # محاسبه بازه زمانی ۱۴ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=14), datetime.min.time())
    start_time = make_aware(naive_start_time)

    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۱۴ روز گذشته")

def _30day_report(update, context):
    """گزارش ۳۰ روز گذشته را با استفاده از موتور گزارش‌ساز مرکزی تهیه می‌کند."""
    update.message.reply_text("در حال تهیه گزارش ۳۰ روز گذشته...")

    # محاسبه بازه زمانی ۳۰ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=30), datetime.min.time())
    start_time = make_aware(naive_start_time)

    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۳۰ روز گذشته")

def _60day_report(update, context):
    """گزارش ۶۰ روز گذشته را با استفاده از موتور گزارش‌ساز مرکزی تهیه می‌کند."""
    update.message.reply_text("در حال تهیه گزارش ۶۰ روز گذشته...")

    # محاسبه بازه زمانی ۶۰ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=60), datetime.min.time())
    start_time = make_aware(naive_start_time)

    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۶۰ روز گذشته")

def _90day_report(update, context):
    """گزارش ۹۰ روز گذشته را با استفاده از موتور گزارش‌ساز مرکزی تهیه می‌کند."""
    update.message.reply_text("در حال تهیه گزارش ۹۰ روز گذشته...")

    # محاسبه بازه زمانی ۹۰ روز گذشته
    end_time = get_server_time()
    naive_start_time = datetime.combine(end_time.date() - timedelta(days=90), datetime.min.time())
    start_time = make_aware(naive_start_time)

    # فراخوانی موتور اصلی گزارش‌ساز
    generate_and_send_report(update, context, start_time, end_time, "۹۰ روز گذشته") 
    
#--------------------توابع گزارش‌گیری بر اساس هفته و ماه--------------------
def today_report(update, context):
    update.message.reply_text("در حال تهیه گزارش امروز...")
    server_now = get_server_time()
    naive_start_time = datetime.combine(server_now.date(), datetime.min.time())
    start_time = make_aware(naive_start_time)
    end_time = server_now
    # print(f"Start time: {start_time}, End time: {end_time}")
    generate_and_send_report(update, context, start_time, end_time, "امروز")
    
def last_week_report(update, context):
    update.message.reply_text("در حال تهیه گزارش هفته گذشته...")
    today = get_server_time().date()
    # پیدا کردن شنبه هفته جاری
    last_saturday = today + relativedelta(weekday=SA(-1))
    
    naive_end_time = datetime.combine(last_saturday, datetime.min.time())
    end_time = make_aware(naive_end_time)
    start_time = end_time - timedelta(days=7)
    generate_and_send_report(update, context, start_time, end_time, "هفته گذشته")

def last_2_weeks_report(update, context):
    update.message.reply_text("در حال تهیه گزارش ۲ هفته گذشته...")
    today = get_server_time().date()
    last_saturday = today + relativedelta(weekday=SA(-1))
    naive_end_time = datetime.combine(last_saturday, datetime.min.time())
    end_time = make_aware(naive_end_time)
    start_time = end_time - timedelta(days=14)
    generate_and_send_report(update, context, start_time, end_time, "دو هفته گذشته")

def last_month_report(update, context):
    update.message.reply_text("در حال تهیه گزارش ماه گذشته...")
    today = get_server_time().date()
    naive_end_time = datetime(today.year, today.month, 1)
    end_time = make_aware(naive_end_time)
    start_time = end_time - relativedelta(months=1)
    generate_and_send_report(update, context, start_time, end_time, "ماه گذشته")

def last_2_months_report(update, context):
    update.message.reply_text("در حال تهیه گزارش ۲ ماه گذشته...")
    today = get_server_time().date()
    naive_end_time = datetime(today.year, today.month, 1)
    end_time = make_aware(naive_end_time)
    start_time = end_time - relativedelta(months=2)
    generate_and_send_report(update, context, start_time, end_time, "دو ماه گذشته")

def last_3_months_report(update, context):
    update.message.reply_text("در حال تهیه گزارش ۳ ماه گذشته...")
    today = get_server_time().date()
    naive_end_time = datetime(today.year, today.month, 1)
    end_time = make_aware(naive_end_time)
    start_time = end_time - relativedelta(months=3)
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
def format_position_closed(deal, original_order_comment):
    """قالب پیام برای بسته شدن پوزیشن با کامنت اصلی"""
    side = "🔴 Sell" if deal.type == mt5.DEAL_TYPE_BUY else "🔵 Buy"
    result = "ℹ️ Manually Closed"
    if deal.reason == 3 or '[tp' in deal.comment.lower(): result = "✅ TP"
    elif deal.reason == 4 or '[sl' in deal.comment.lower(): result = "❌ SL"
    
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
    broker_account_line = f"`{account_info.company}|Acc: {account_info.login}`\n" if account_info else ""
    
    return (
        f"**⚔️ Position Closed**\n\n"
        f"{broker_account_line}"
        f"{balance_equity_line}"
        f"`Symbol : `{deal.symbol}\n"
        f"`Side   : {side}`\n"
        f"`Result : {result}`\n"
        f"`Profit : {deal.profit:,.2f} $`\n"
        f"`Lots   : {deal.volume}`\n"
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

# ====================== تابع اصلی مانیتورینگ ======================
def main():
        # این حلقه تا زمانی که اینترنت آماده شود، ادامه دارد
    while True:
        # تلاش برای راه‌اندازی شنونده تلگرام
        try:
            global updater
            updater = Updater(TOKEN, use_context=True)
            dispatcher = updater.dispatcher
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
            print("Bot is listening for commands in the background...")
            #print("Telegram connection successful. Starting main operations.")
            break

        except Exception as e:
            # اگر اینترنت وصل نبود، خطا را چاپ کرده و حلقه را تکرار می‌کنیم
            print(f"--initial listener run fail Retrying in 10 seconds...")
            time.sleep(10)
            continue
    
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
                            position_deals = mt5.history_deals_get(position=deal.position_id)
                            if position_deals:
                                for opening_deal in position_deals:
                                    if opening_deal.entry == mt5.DEAL_ENTRY_IN:
                                        opening_order = mt5.history_orders_get(ticket=opening_deal.order)
                                        if opening_order:
                                            original_comment = opening_order[0].comment
                                        break
                            msg = format_position_closed(deal, original_comment)
                            send_telegram(msg)
                        
                        processed_deals.add(deal.ticket)
                
                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                print(f"Connection to MT5 lost during monitoring: {e}")
                send_telegram("⚠️ Connection to MT5 lost. Attempting to reconnect...")
                is_connected = False
                disconnect_time = time.time()
                mt5.shutdown()
                time.sleep(RECONNECT_DELAY)
                continue
        else:
            # --- حالت قطع شده: تلاش برای اتصال مجدد ---
            print("Attempting to connect to MetaTrader 5...")
            
            if disconnect_time and (time.time() - disconnect_time > OVERALL_TIMEOUT):
                print(f"Could not reconnect within {int(OVERALL_TIMEOUT/60)} minutes. Shutting down for good.")
                send_telegram(f"❌ Could not reconnect to MT5 for {int(OVERALL_TIMEOUT/60)} minutes. Bot is shutting down.")
                break 

            if mt5.initialize(path=MT5_PATH):
                if disconnect_time:
                    print("Reconnected to MT5 successfully!")
                    send_telegram("✅ Reconnected to MT5. Monitoring resumed.")
                else: # در غیر این صورت این اولین اتصال است
                    print("connected to MT5 successfully!")
                    print("Monitoring...")
                    send_telegram("✅ *Bot is running*\nMonitoring...")

                is_connected = True
                disconnect_time = None
                # تغییر: ریست کردن زمان پس از اتصال بر اساس زمان سرور
                last_check_time = get_server_time()
                
                # این بخش دیگر ضروری نیست چون ما تاریخچه را چک می‌کنیم نه پوزیشن‌های باز را
                positions_result = mt5.positions_get()
                last_known_positions = {p.ticket: p for p in positions_result} if positions_result else {}
                # print(f"Ignoring {len(last_known_positions)} existing position(s).")
                # send_telegram(f"{len(last_known_positions)} existing position(s).")
                # --- بخش جدید: ساخت و ارسال لیست پوزیشن‌های باز ---
                print(f"Ignoring {len(last_known_positions)} existing position(s).")
                
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
                        #header = f"Symbol  |Side  |Lots   |Profit"
                        line = f"{position.symbol:<8}|{side:>5} |{lot:>6.2f} |{profit:>8.2f}$"
                        # position_lines.append(header)
                        position_lines.append(line)
                    
                    # ترکیب همه خطوط در یک پیام واحد و ارسال آن
                    full_message = "\n".join(position_lines)
                    send_telegram(full_message)
                else:
                    # اگر هیچ پوزیشنی باز نبود، فقط یک پیام ساده بفرست
                    send_telegram("0 existing position(s).")
            else:
                print(f"Connection failed. Retrying in {RECONNECT_DELAY} seconds...")
                time.sleep(RECONNECT_DELAY)
                #mt5.initialize(path=MT5_PATH)#فقط به خاطر اینکه متاتریدر اگه اجرا نبود اجرا بشه
    
    print("Script has been shut down.")
    updater.stop()
    
# ====================== اجرای اسکریپت ======================
if __name__ == "__main__":
    # مرحله اول: تشخیص خودکار منطقه زمانی قبل از هر کاری
    BROKER_TIMEZONE = determine_broker_timezone()

    # اگر تشخیص منطقه زمانی ناموفق بود، برنامه را متوقف کن
    if BROKER_TIMEZONE is None:
        print("Could not run the script because timezone detection failed.")
    else:
        # اگر موفق بود، برنامه اصلی را اجرا کن
        try:
            main()
        except KeyboardInterrupt:
            send_telegram("ℹ️ *Script Stopped Manually*")
            print("\nScript stopped by user.")
        except Exception as e:
            send_telegram(f"❌ *CRITICAL ERROR*\nBot has crashed!\nError: {e}")
            print(f"Critical Error: {e}")
        finally:
            if updater and updater.running:
                # تغییر ۳: در نهایت، چه با خطا و چه با Ctrl+C، شنونده را متوقف می‌کنیم
                print("{please wait}Stopping the bot updater...")
                updater.stop()
                print("Updater stopped.")
            if mt5.terminal_info():
                mt5.shutdown()
            print("Script exited gracefully.")    
    

