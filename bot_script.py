import MetaTrader5 as mt5
import time
import logging
import tkinter as tk

# from tkinter import ttk
import os
import colorlog
import pytz
import sqlite3
import asyncio
import aiosmtpd
from aiosmtpd.smtp import SMTP as BaseSMTP
from aiosmtpd.controller import Controller

# import email
from email import policy
from email.parser import BytesParser
from flask import Flask, request
import threading
import arabic_reshaper
from bidi.algorithm import get_display
import numpy as np
from scipy.interpolate import make_interp_spline
import matplotlib
from contextlib import contextmanager

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
import io
import configparser

# from datetime import date
from dateutil.relativedelta import relativedelta, SA  # برای پیدا کردن شنبه
from telegram import Bot
from telegram.error import BadRequest, NetworkError
from datetime import datetime, timedelta  # تغییر ضروری: timedelta اضافه شد
from telegram.ext import (
    Updater,
    CommandHandler,
)  # تغییر ضروری: کتابخانه‌های شنونده اضافه شدند
from telegram.ext import (
    ConversationHandler,
    MessageHandler,
    Filters,
)  # کتابخانه تاریخ دستی
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

# تا اینجا بجز سفارشی دکمه براش درست شده

# ====================== ساکت کردن گزارشگرهای پیش‌فرض تلگرام ======================
# این بخش گزارش‌های خطای شبکه‌ای پیش‌فرض کتابخانه تلگرام و وابستگی‌های آن را غیرفعال می‌کند
# تا فقط مدیر خطای شخصی ما (handle_error) پیام‌ها را چاپ کند.
# ====== تنظیمات لاگ رنگی و پیشرفته ======
# تعریف کدهای رنگی ANSI برای تاریخ
ORANGE = "\033[33m"
RESET = "\033[0m"

# ۱. گرفتن گزارشگر اصلی
log = logging.getLogger()
log.setLevel(logging.INFO)

# ۲. ساخت یک فرمت‌دهنده رنگی
# %(log_color)s: رنگ را بر اساس سطح خطا تنظیم می‌کند
# ما تاریخ را به صورت دستی با کدهای ANSI رنگی می‌کنیم
formatter = colorlog.ColoredFormatter(
    f"{ORANGE}%(asctime)s{RESET}%(log_color)s[%(levelname)s]{RESET}%(message)s",
    log_colors={
        "DEBUG": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold_red",
    },
)

# ۳. ساخت یک کنترل‌کننده و اعمال فرمت‌دهنده روی آن
handler = colorlog.StreamHandler()
handler.setFormatter(formatter)

# ۴. اضافه کردن کنترل‌کننده به گزارشگر اصلی
log.addHandler(handler)
logging.getLogger("telegram.vendor.ptb_urllib3.urllib3.connectionpool").setLevel(
    logging.CRITICAL
)
logging.getLogger("telegram.ext.updater").setLevel(logging.CRITICAL)


# ... در هر کجای کد که می‌خواهید وضعیت نخ‌ها را ببینید:
def check_active_threads():
    active_threads = threading.enumerate()
    logging.info(f"Total active threads: {len(active_threads)}")
    for thread in active_threads:
        # thread.name نام نخ را برمی‌گرداند (اگر در هنگام ساخت نامگذاری شده باشد)
        # thread.daemon نشان می‌دهد که آیا نخ از نوع Daemon است یا خیر
        logging.info(
            f"  - Thread Name: {thread.name}, Is Daemon: {thread.daemon}, Status: {'Alive' if thread.is_alive() else 'Dead'}"
        )


# ====================== کلاس پنجره شناور سود ======================


def log_memory_usage(operation_name=""):
    """لاگ کردن استفاده از حافظه برای مانیتورینگ"""
    import psutil
    import os

    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024

    if operation_name:
        logging.info(f"🧠 Memory after {operation_name}: {memory_mb:.2f} MB")
    else:
        logging.info(f"🧠 Current memory: {memory_mb:.2f} MB")

    return memory_mb


# استفاده در نقاط مختلف:
# log_memory_usage("generating report")
# log_memory_usage("closing floating window")


class FloatingProfitWindow:
    def __init__(self):
        self.is_monitoring = False
        self.monitor_thread = None
        self.is_running = False  # 🔑 وضعیت کلی نخ GUI (فقط یک بار True می‌شود)
        self.check_interval = 1  # چک هر 1 ثانیه
        self.root = None
        self.label = None
        self.gui_thread = None
        self.last_profit = 0
        self.is_visible = False  # 🔑 وضعیت را به False تغییر می‌دهیم
        # 🔑 مخفی کردن اولیه: پنجره در ابتدا نمایش داده نمی‌شود.

        # --- بخش جدید: برای هماهنگی بین نخ‌ها ---
        # این Event به نخ اصلی اطلاع می‌دهد که GUI آماده است
        self.gui_ready_event = threading.Event()
        # مختصات drag
        self.start_x = 0
        self.start_y = 0

    def clean_exit(self):
        """خاموش کردن کامل پنجره و منتظر ماندن برای پایان نخ GUI"""
        self.is_monitoring = False

        if self.root:
            try:
                # 1. دستور بسته شدن (destroy) را به نخ GUI می‌فرستد
                # اگر self.root وجود داشته باشد، یعنی زمانی پنجره باز شده.
                self.root.after(0, self.root.destroy)
            except Exception as e:
                logging.error(f"Error scheduling GUI close: {e}")

        # 2. بررسی شیء self.gui_thread قبل از استفاده از متدهای آن
        if self.gui_thread is not None:
            # توجه: از is_alive() استفاده کنید، نه is_live()
            if self.gui_thread.is_alive():
                logging.info("Waiting for GUI thread to finish...")
                # 3. منتظر می‌ماند تا نخ GUI بعد از دریافت سیگنال destroy() تمام شود
                self.gui_thread.join(timeout=3)

                if self.gui_thread.is_alive():
                    logging.warning("GUI thread did not shut down gracefully.")
                else:
                    logging.info("GUI thread shut down successfully.")
        # 🔑 حذف رفرنس ها پس از destroy
        self.root = None
        self.label = None
        self.gui_thread = None
        self.is_running = False  # نخ کاملا مرده است
        self._cleanup_memory()
        # self.gui_thread = None
        # self.root = None

    def _create_floating_window(self):
        """ساخت پنجره شناور در وسط بالا"""
        try:
            self.root = tk.Tk()
            # self.root.withdraw()  # مشکل اینجا

            self.gui_ready_event.clear()  # اعلام می‌کنیم که GUI در حال ساخت است
            # حذف titlebar و border
            self.root.overrideredirect(True)

            # همیشه روی همه پنجره‌ها
            self.root.attributes("-topmost", True)

            # پس‌زمینه مشکی شفاف
            self.root.configure(bg="black")
            self.root.attributes("-alpha", 0.9)  # شفافیت

            # محاسبه موقعیت وسط بالا
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            window_width = 300
            window_height = 55

            x_position = (screen_width - window_width) // 2  # وسط افقی
            y_position = 1  # 10 پیکسل از بالا

            self.root.geometry(
                f"{window_width}x{window_height}+{x_position}+{y_position}"
            )

            # لیبل برای نمایش سود
            self.label = tk.Label(
                self.root,
                text="💰 0.00 $",
                font=("Arial", 30, "bold"),
                fg="white",
                bg="black",
                padx=0,
                pady=0,
            )
            self.label.pack(expand=True)

            # امکان drag کردن پنجره
            self.label.bind("<Button-1>", self._start_drag)
            self.label.bind("<B1-Motion>", self._do_drag)

            # دابل کلیک برای بستن
            self.label.bind("<Double-Button-1>", self._close_window)

            logging.info("📊 Floating window created")
            # --- تغییر کلیدی: پنجره را در ابتدا پنهان کن ---
            # self.root.withdraw()
            # --- بخش جدید: به نخ اصلی اطلاع بده که GUI آماده است ---
            self.gui_ready_event.set()
        except Exception as e:
            logging.error(f"📊 Window creation error: {e}")
            self.gui_ready_event.set()  # در صورت خطا هم event را set کن تا نخ اصلی گیر نکند

    def _start_drag(self, event):
        """شروع drag کردن پنجره"""
        self.start_x = event.x
        self.start_y = event.y

    def _do_drag(self, event):
        """انجام drag"""
        x = self.root.winfo_x() + (event.x - self.start_x)
        y = self.root.winfo_y() + (event.y - self.start_y)
        self.root.geometry(f"+{x}+{y}")

    def _close_window(self, event):
        """بستن پنجره با دابل کلیک (فقط پنهان کردن)"""
        # مستقیماً stop_monitoring را صدا می‌زنیم
        # چون این از نخ GUI اجرا می‌شود، امن است
        self.stop_monitoring()

    def _safe_close(self):
        """
        این متد همیشه باید از طریق root.after() فراخوانی شود
        تا تضمین کند که در نخ GUI اجرا می‌شود.
        """
        try:
            if self.root:
                # حذف تمام ویجت‌ها
                # for widget in self.root.winfo_children():
                #     widget.destroy()
                # self.root.quit()
                # self.root.destroy()
                # 🔑 مهم: به جای destroy کردن، فقط مخفی می‌کنیم
                self.root.withdraw()
                self.is_visible = False
                self.is_monitoring = False  # قطع مانیتورینگ
                logging.info("📊 Floating window closed")
        except Exception as e:
            logging.error(f"📊 Safe window close error: {e}")
        finally:
            # self.root = None
            # self.label = None
            # self.gui_thread = None  # نخ GUI کارش تمام شد
            # self._cleanup_memory()
            log_memory_usage("closing floating window")

    def _update_profit_display(self, profit):
        """آپدیت نمایش سود"""
        # اضافه کردن چک برای وجود پنجره و وضعیت مانیتورینگ
        if not self.is_monitoring or not self.root or not self.label:
            return

        # چک کردن که پنجره هنوز وجود دارد
        try:
            self.root.winfo_exists()
        except:
            return
        try:
            # profit_sign = "+" if profit >= 0 else ""
            # color = "#00ff00" if profit >= 0 else "#ff0000"  # سبز و قرمز پررنگ
            profit_sign = "+" if profit > 0 else ""
            color = (
                "#00ff00" if profit > 0 else ("#ff0000" if profit < 0 else "#9B9B9A")
            )

            self.label.config(text=f"💰 {profit_sign}{profit:,.2f} $", fg=color)

        except Exception as e:
            logging.error(f"📊 Display update error: {e}")

    def _gui_loop(self):
        """حلقه GUI"""
        try:
            self._create_floating_window()
            self.root.mainloop()
        except Exception as e:
            logging.error(f"📊 GUI loop error: {e}")

    def _cleanup_memory(self):
        """آزادسازی حافظه مربوط به پنجره شناور"""
        import gc

        # حذف رفرنس‌های بزرگ
        if hasattr(self, "root"):
            self.root = None
        if hasattr(self, "label"):
            self.label = None
        gc.collect()

    def start_monitoring(self):
        log_memory_usage("")
        """شروع مانیتورینگ و نمایش پنجره"""
        if self.is_monitoring:
            return "⚠️ مانیتورینگ فعال است"

        # # اگر نخ مانیتور قبلی هنوز زنده است
        # if self.monitor_thread and self.monitor_thread.is_alive():
        #     self.is_monitoring = False  # سیگنال توقف بده
        #     self.monitor_thread.join(timeout=1)  # 1 ثانیه صبر کن تا بمیرد

        # === تغییر کلیدی ===
        # اگر نخ GUI قبلی هنوز زنده است (مثلا در حال بسته شدن)، اجازه نده شروع شود
        # if self.gui_thread and self.gui_thread.is_alive():
        #     return "⚠️ پنجره قبلی در حال بسته شدن است. لطفاً چند ثانیه صبر کنید و دوباره امتحان کنید."

        # اگر پنجره قبلاً باز است، ببندش
        # if self.root:
        #     try:
        #         self.root.quit()
        #         self.root.destroy()
        #     except:
        #         pass
        # 🔑 بخش کلیدی: فقط یک بار نخ GUI را شروع کن
        if not self.is_running:
            self.gui_thread = threading.Thread(
                target=self._gui_loop, daemon=True, name="Floating_GUI_Thread"
            )
            self.gui_thread.start()
            self.is_running = True

            # صبر کن تا GUI آماده شود (با استفاده از Event بهتر است)
            # time.sleep(2)  # حذف شده
            self.gui_ready_event.wait(timeout=5)  # 5 ثانیه منتظر آماده شدن GUI باش

            if not self.root:
                logging.error("📊 GUI thread failed to create root window.")
                self.is_running = False
                self.gui_thread = None
                return "❌ خطا در ایجاد پنجره. لطفاً لاگ‌ها را بررسی کنید."

        # self.root = None
        # self.label = None
        self.last_profit = 0  # ریست کردن سود قبلی
        self.is_monitoring = True
        # # شروع thread GUI
        # self.gui_thread = threading.Thread(
        #     target=self._gui_loop, daemon=True, name="Floating_GUI_Thread"
        # )
        # self.gui_thread.start()

        # صبر کن تا GUI آماده شود
        # time.sleep(2)

        # === تغییر کلیدی ===
        # مطمئن شو که root ساخته شده است
        if not self.root:
            logging.error("📊 GUI thread failed to start or create root window.")
            self.gui_thread = None
            return "❌ خطا در ایجاد پنجره. لطفاً لاگ‌ها را بررسی کنید."
        # نمایش مجدد پنجره (این کار را به monitor_loop واگذار می‌کنیم)
        # 🔑 مهم: اگر نخ مانیتور قبلی مرده، یکی جدید بساز
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="Floating_Monitor_Thread"
            )
            self.monitor_thread.start()
        # self.is_monitoring = True
        # self.monitor_thread = threading.Thread(
        #     target=self._monitor_loop, daemon=True, name="Floating_Monitor_Thread"
        # )
        # self.monitor_thread.start()

        logging.info("📊 Floating profit monitor started")
        return "✅ پنجره شناور سود فعال شد\n🖱️ برای جابجایی: کلیک و درگ کنید\n🖱️ برای بستن: دابل کلیک"

    def stop_monitoring(self):
        """توقف مانیتورینگ و بستن پنجره"""
        if not self.is_monitoring:
            return "⚠️ مانیتورینگ فعال نیست"

        self.is_monitoring = False
        # self.is_visible = False  # ✅ این خط را اضافه کن
        # === تغییر کلیدی ===
        # بستن پنجره را به نخ GUI واگذار کن
        # این کار غیرمسدودکننده (non-blocking) است و بلافاصله برمی‌گردد
        if self.root:
            try:
                # self.root.after(0, self._safe_close)
                self.root.after(0, self.root.withdraw)  # فقط مخفی کن
                self.is_visible = False
            except Exception as e:
                # اگر نخ GUI قبلاً مرده باشد، این خطا رخ می‌دهد
                logging.error(f"📊 Window close schedule error: {e}")
                self.root = None  # فقط رها کن
                self.label = None

        # === تغییر کلیدی ===
        # دیگر منتظر نخ مانیتورینگ نمان (join نکن)
        # join() باعث می‌شد نخ تلگرام مسدود شود و نتواند پاسخ را ارسال کند
        # چون نخ مانیتور daemon=True است، خودش متوقف خواهد شد
        # if self.monitor_thread:
        #    self.monitor_thread.join(timeout=3) # <--- این خط را حذف کن
        # بستن پنجره
        # if self.root:
        #     try:
        #         self.root.quit()
        #         # self.root.destroy()
        #         self.root = None
        #         self.label = None
        #     except Exception as e:
        #         logging.error(f"📊 Window close error: {e}")

        logging.info("📊 Floating profit monitor stopped")
        return "🛑 پنجره شناور بسته شد"

    def get_status(self):
        """وضعیت فعلی"""
        try:
            positions = mt5.positions_get()
            total_profit = sum(pos.profit for pos in positions) if positions else 0

            status = f"""
📊 **وضعیت پنجره شناور**

💰 **سود کل:** {total_profit:,.2f}$
📈 **تعداد پوزیشن:** {len(positions) if positions else 0}
🔔 **مانیتورینگ:** {'فعال' if self.is_monitoring else 'غیرفعال'}
🪟 **پنجره:** {'باز' if self.root else 'بسته'}
📍 **موقعیت:** وسط بالای صفحه

"""
            return status

        except Exception as e:
            return f"❌ خطا در دریافت وضعیت: {e}"

    def _monitor_loop(self):
        """حلقه اصلی مانیتورینگ"""
        while self.is_monitoring:
            try:
                if not self.is_monitoring:
                    break
                time.sleep(self.check_interval)
                # چک کردن وضعیت مانیتورینگ بعد از sleep
                if not self.is_monitoring:
                    break

                # === تغییر کلیدی ===
                # اگر پنجره (root) دیگر وجود ندارد (بسته شده)، حلقه را متوقف کن
                if not self.root:
                    self.is_monitoring = False
                    break

                if not mt5.terminal_info() or not mt5.terminal_info().connected:
                    continue

                # positions = mt5.positions_get()
                # if not positions:
                #     # اگر پوزیشنی نیست، صفر نمایش بده
                #     if self.last_profit != 0:
                #         # === تغییر کلیدی ===
                #         # آپدیت را به نخ GUI بسپار
                #         if self.root:
                #             self.root.after(0, self._update_profit_display, 0)
                #         self.last_profit = 0
                #     continue
                # ... بخشی از حلقه مانیتورینگ شما (احتمالاً در یک متد به نام monitor_positions)

                positions = mt5.positions_get()

                # =========================================================
                # 🔑 منطق مخفی سازی: اگر پوزیشنی نیست
                # =========================================================
                if not positions:
                    # 1. آپدیت سود به صفر (طبق کد قبلی شما)
                    if self.last_profit != 0:
                        if self.root:
                            self.root.after(0, self._update_profit_display, 0)
                        self.last_profit = 0

                    # 2. اگر پنجره نمایش داده می‌شود، آن را مخفی کن
                    if self.root:
                        self.root.after(
                            0, self.root.withdraw
                        )  # متد Tkinter برای مخفی کردن پنجره
                        # self.is_visible = False

                    continue  # ادامه حلقه برای چک بعدی

                # =========================================================
                # 🔑 منطق نمایش: اگر پوزیشن باز وجود دارد
                # =========================================================

                # اگر پنجره مخفی است، آن را نمایش بده
                if self.root:
                    self.root.after(
                        0, self.root.deiconify
                    )  # متد Tkinter برای نمایش مجدد پنجره
                    # self.is_visible = True

                # ... ادامه منطق برنامه برای محاسبه سود، آپدیت نمایش و ...
                # ...
                total_profit_sum = sum(pos.profit for pos in positions)
                total_swap_sum = sum(pos.swap for pos in positions)
                total_profit = total_profit_sum + total_swap_sum

                # فقط اگر سود تغییر کرده آپدیت کن
                if abs(total_profit - self.last_profit) > 0.01:
                    # === تغییر کلیدی ===
                    # آپدیت را به نخ GUI بسپار
                    if self.root:
                        self.root.after(0, self._update_profit_display, total_profit)
                    self.last_profit = total_profit

            except Exception as e:
                logging.error(f"📊 Monitor error: {e}")
                time.sleep(10)
        logging.info("📊 Monitor loop finished.")


# ایجاد نمونه جهانی
floating_profit = FloatingProfitWindow()


def start_floating_window(update, context):
    """شروع پنجره شناور"""
    try:
        result = floating_profit.start_monitoring()
        update.message.reply_text(result)
    except Exception as e:
        update.message.reply_text(f"❌ خطا: {e}")


def stop_floating_window(update, context):
    """توقف پنجره شناور"""
    try:
        result = floating_profit.stop_monitoring()
        update.message.reply_text(result)
    except Exception as e:
        update.message.reply_text(f"❌ خطا: {e}")


def floating_status(update, context):
    """وضعیت پنجره شناور"""
    try:
        status = floating_profit.get_status()
        update.message.reply_text(status, parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ خطا: {e}")


# ===================================================================


# =====================تابع برگرداندن منطقه زمانی بروکر =====================
def determine_broker_timezone():
    """
    اختلاف زمانی سرور بروکر با UTC را محاسبه کرده و رشته منطقه زمانی صحیح را برمی‌گرداند.
    """
    logging.info("Determining broker timezone...")
    if not mt5.initialize(path=MT5_PATH):
        logging.error("Could not connect to MT5 to determine timezone.")
        return None
    PRIORITY_BASE_SYMBOLS = ["BTCUSD", "XAUUSD"]
    # 2. دریافت لیست تمام نمادهای موجود در سرور
    # این لیست برای پیدا کردن پسوندها ضروری است.
    all_symbols_on_server = mt5.symbols_get()
    selected_full_symbol = None
    for base_symbol in PRIORITY_BASE_SYMBOLS:

        # 3. پیدا کردن نماد کامل (شامل پسوند)
        # این بخش نمادی مثل "XAUUSD.pe" یا "BTCUSD" (بدون پسوند) را پیدا می‌کند
        matching_symbols = [
            s.name for s in all_symbols_on_server if s.name.startswith(base_symbol)
        ]

        if matching_symbols:
            # اولین نماد تطابق داده شده را انتخاب می‌کنیم (معمولاً درست‌ترین است)
            full_symbol = matching_symbols[0]
            # print(f"trying {full_symbol}...")
            # سعی در اضافه کردن نماد به واچ‌لیست
            # 💡 فعال کردن نماد در واچ‌لیست
            while True:
                if not mt5.symbol_select(full_symbol, True):
                    while True:
                        if not mt5.initialize(path=MT5_PATH):
                            logging.error("mt5 not initialized, retrying...")
                            time.sleep(RECONNECT_DELAY)
                        else:
                            break
                    logging.error(
                        f"⚠️ can't add {full_symbol} to watchlist, error code: {mt5.last_error()}"
                    )
                    time.sleep(0.5)  # صبر کن و دوباره تلاش کن
                else:
                    time.sleep(1)  # صبر کن تا سرور به‌روزرسانی کند
                    try:
                        # 4. سعی در گرفتن آخرین تیک نماد کامل
                        last_tick = mt5.symbol_info_tick(full_symbol)
                    except Exception as e:
                        logging.error(
                            f"⚠️ Error retrieving tick for {full_symbol}: {e}"
                        )
                        continue
                    break  # موفق شدیم، از حلقه خارج می‌شویم

            # last_tick = mt5.symbol_info_tick(full_symbol)
            if last_tick and last_tick.time > 0:
                selected_full_symbol = full_symbol
                break  # نماد را پیدا کردیم، از حلقه خارج می‌شویم

    server_tick = mt5.symbol_info_tick(selected_full_symbol)
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
    offset = round(time_difference_hours)  # به نزدیک‌ترین ساعت کامل گرد می‌کنیم

    # logging.info(f"Detected timezone offset: UTC{offset:+}")

    # ساخت رشته صحیح Etc/GMT (علامت برعکس است)
    offset_sign = "+" if offset <= 0 else "-"
    # timezone_str = f"Etc/GMT{offset_sign}{abs(offset)}"
    timezone_str = "Etc/GMT+0"
    # mt5.shutdown()
    logging.info(f"Timezone: {timezone_str}")
    return timezone_str


# ========================= تنظیمات اصلی =========================
# مسیر فایلی که در حال اجرای آن هستیم (مسیر خود اسکریپت پایتون)
script_dir = os.path.dirname(os.path.abspath(__file__))

# مسیر مطلق فایل config.ini را با پیوستن مسیر اسکریپت و نام فایل می‌سازیم
config_file_path = os.path.join(script_dir, "config.ini")

# یک نمونه از ConfigParser می‌سازیم
config = configparser.ConfigParser()

# مسیر فایل config.ini رو چک می‌کنیم
if not os.path.exists(config_file_path):
    log.error(f"config file '{config_file_path}' not found please read README file.")
    exit()
    # می‌توانید در اینجا برنامه را متوقف کنید یا از مقادیر پیش‌فرض استفاده کنید
else:
    # فایل config.ini رو می‌خونیم
    config.read(config_file_path)

    # مقادیر رو از بخش‌های مختلف می‌گیریم
    try:
        TOKEN = config.get("telegram", "TOKEN")
        CHAT_ID = config.getint(
            "telegram", "CHAT_ID"
        )  # اگر عدد هست، بهتره با getint بخونید
        MT5_PATH = config.get("mt5", "MT5_PATH")
    except configparser.Error as e:
        log.error(f"read config file error {e}")
        # مدیریت خطا: مقادیر رو None یا مقدار پیش‌فرض قرار بدید

# حالا می‌تونید از متغیرهای TOKEN، CHAT_ID و MT5_PATH در ادامه‌ی کد استفاده کنید.
log.info(f"token: {TOKEN[:5]}...")  # برای تست، بخشی از توکن رو چاپ کنید
log.info(f"chat_id: {CHAT_ID}")
log.info(f"MT5_path: {MT5_PATH}")

TOKEN = TOKEN
CHAT_ID = CHAT_ID

# --- مراحل مکالمه برای گزارش سفارشی ---
START_DATE, END_DATE = range(2)
GET_SINGLE_DATE = range(1)
# +++ کد جدید +++
# لیستی برای ذخیره شناسه‌های پیام‌های هشدار جهت جلوگیری از حذف با دستور
KEYWORDS_TO_KEEP = [
    "Position Closed",
    "Order Filled",
    "GMM-Glory",
    "Position Closed (Partial)",
    "Position Closed (Complete)",
]
# لیست شناسه‌های تمام پیام‌هایی که می‌توانند حذف شوند (چون کلمات کلیدی بالا را ندارند)
alert_message_ids = []

CHECK_INTERVAL = 5  # فاصله زمانی بین هر چک در حالت عادی

# ---  تنظیمات اتصال مجدد به متاتریدر ---
RECONNECT_DELAY = 10  # هر چند ثانیه برای اتصال مجدد تلاش کند
OVERALL_TIMEOUT = 6000  # مهلت زمانی نهایی به ثانیه (600 ثانیه = 10 دقیقه)

# --- تنظیمات تلاش مجدد برای ارسال تلگرام ---
RETRY_COUNT = 2000
RETRY_DELAY = 2

# --- مسیر متاتریدر خاص ---
MT5_PATH = MT5_PATH  # مسیر کامل فایل terminal64.exe متاتریدر شما
BROKER_TIMEZONE = None
# --- آستانه برای محاسبه وین ریت واقعی ---
WINRATE_THRESHOLD_PERCENT = 0.05  # 0.05% of starting balance

# ========================= تنظیمات پایگاه داده =========================
# آدرس کامل پوشه‌ای که اسکریپت در آن قرار دارد را پیدا می‌کند
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# آدرس کامل فایل پایگاه داده را می‌سازد
DB_NAME = os.path.join(SCRIPT_DIR, "bot_data.db")
# DB_NAME = "bot_data.db" # نام فایل پایگاه داده شما


def setup_database():
    """پایگاه داده و جدول مورد نیاز را در صورت عدم وجود ایجاد می‌کند."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # یک جدول برای نگهداری شناسه‌های پیام ایجاد می‌کنیم
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages_to_delete (
            message_id INTEGER PRIMARY KEY
        )
    """)
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
    cursor.execute(
        "INSERT OR IGNORE INTO messages_to_delete (message_id) VALUES (?)",
        (message_id,),
    )
    conn.commit()
    conn.close()


def remove_id_from_db(message_id):
    """یک شناسه پیام را از پایگاه داده حذف می‌کند."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages_to_delete WHERE message_id = ?", (message_id,))
    conn.commit()
    conn.close()


# ========================= سرور SMTP سفارشی =========================

# =============== خاموش کردن لاگ‌های aiosmtpd ==================
# import logging

# logging.getLogger("aiosmtpd").setLevel(logging.CRITICAL)
logging.getLogger("mail.log").setLevel(logging.CRITICAL)
# logging.getLogger("aiosmtpd.smtp").setLevel(logging.CRITICAL)
# logging.getLogger("aiosmtpd.controller").setLevel(logging.CRITICAL)
# logging.getLogger("asyncio").setLevel(logging.CRITICAL)
# ===============================================================


class QuietSMTPHandler:
    def __init__(self):
        self.bot = Bot(token=TOKEN)

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        try:
            # فقط لاگ مهم را نگه دار
            logging.info(f"📧 New alert from MT5")

            # پارس کردن ایمیل
            msg = BytesParser(policy=policy.default).parsebytes(envelope.content)
            subject = msg.get("subject", "No Subject")
            body = ""

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain":
                        body = part.get_content()
                        break
            else:
                body = msg.get_content()

            if not body:
                body = envelope.content.decode("utf-8", errors="ignore")

            # ارسال به تلگرام
            telegram_message = (
                # f"**📧 MT5 Alert**\n\n**Subject:** {subject}\n**Message:** {body}"
                f"{subject}\n{body}"
            )

            sent_msg = self.bot.send_message(
                chat_id=CHAT_ID, text=telegram_message, parse_mode="Markdown"
            )

            logging.info(f"✅ Alert forwarded to Telegram")

            if sent_msg and not any(keyword in body for keyword in KEYWORDS_TO_KEEP):
                alert_message_ids.append(sent_msg.message_id)
                add_id_to_db(sent_msg.message_id)

            return "250 OK"

        except Exception as e:
            logging.error(f"❌ Error processing alert: {e}")
            return "550 Error"


class TelegramHandler:
    def __init__(self):
        self.bot = Bot(token=TOKEN)

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        try:
            # logging.info(f"📧 Email received from: {envelope.mail_from}")

            # پارس کردن ایمیل
            msg = BytesParser(policy=policy.default).parsebytes(envelope.content)

            # گرفتن موضوع
            subject = msg.get("subject", "No Subject")

            # استخراج متن ایمیل
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain":
                        body = part.get_content()
                        break
            else:
                body = msg.get_content()

            # اگر body خالی بود، از متن اصلی استفاده کن
            if not body:
                body = msg.get_content()

            # ساخت پیام برای تلگرام
            telegram_message = f"{subject}\n{body}"

            # ارسال به تلگرام
            sent_msg = self.bot.send_message(
                chat_id=CHAT_ID, text=telegram_message, parse_mode="Markdown"
            )

            logging.info(f"{subject}")

            # اگر پیام مهم نبود، برای پاک کردن علامت بزن
            if sent_msg and not any(keyword in body for keyword in KEYWORDS_TO_KEEP):
                alert_message_ids.append(sent_msg.message_id)
                add_id_to_db(sent_msg.message_id)

            return "250 Message accepted for delivery"

        except Exception as e:
            logging.error(f"❌ Error processing email: {e}")
            return "550 Error processing message"


class SMTPController:
    def __init__(self):
        self.controller = None
        self.handler = TelegramHandler()

    def start(self):
        """شروع کنترلر SMTP"""
        try:
            self.controller = Controller(
                self.handler,
                hostname="localhost",
                port=1025,
                auth_required=False,
                enable_SMTPUTF8=False,
            )
            self.controller.start()
            logging.info("SMTP Server started on localhost:1025")
            return True
        except Exception as e:
            logging.error(f"❌ Failed to start SMTP server: {e}")
            return False

    def stop(self):
        """توقف کنترلر SMTP"""
        if self.controller:
            self.controller.stop()
            logging.info("🛑 SMTP Server stopped")


def run_smtp_server():
    """اجرای سرور SMTP در ترد جداگانه"""
    try:
        # ایجاد event loop جدید برای این ترد
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        controller = SMTPController()
        if controller.start():
            # اجرای حلقه رویداد
            loop.run_forever()
        else:
            logging.error("❌ SMTP server failed to start")
    except Exception as e:
        logging.error(f"❌ SMTP server error: {e}")
    finally:
        if "loop" in locals():
            loop.close()


def start_smtp_server_thread():
    """شروع سرور SMTP در یک ترد جداگانه"""
    smtp_thread = threading.Thread(target=run_smtp_server, daemon=True)
    smtp_thread.start()
    return smtp_thread


def setup_smtp_server():
    """تنظیم و راه‌اندازی سرور SMTP"""
    try:
        # تست کردن که پورت 1025 آزاد است
        import socket

        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.settimeout(1)
        result = test_socket.connect_ex(("localhost", 1025))
        test_socket.close()

        if result == 0:
            logging.warning("⚠️ Port 1025 is already in use. Trying port 1026...")
            # می‌توانید پورت دیگری را امتحان کنید
            return False

        logging.info("✅ Port 1025 is available for SMTP server")

        # شروع سرور SMTP
        start_smtp_server_thread()
        logging.info("✅ SMTP Server thread started successfully")
        return True

    except Exception as e:
        logging.error(f"❌ SMTP Server setup failed: {e}")
        return False


# دستور تست ایمیل (بدون تغییر)
def test_email_command(update, context):
    """دستور تست برای ارسال ایمیل آزمایشی"""
    try:
        import smtplib
        from email.mime.text import MIMEText

        # ساخت ایمیل تست
        msg = MIMEText("This is a test email from MT5 Bot\nTest message content.")
        msg["Subject"] = "Test Alert from MT5 Bot"
        msg["From"] = "test@localhost"
        msg["To"] = "alert@localhost"

        # ارسال به سرور SMTP محلی
        with smtplib.SMTP("localhost", 1025, timeout=10) as server:
            server.send_message(msg)

        update.message.reply_text("✅ Test email sent successfully!")

    except Exception as e:
        update.message.reply_text(f"❌ Test failed: {str(e)}")
        logging.error(f"Test email error: {e}")


# ====================== اتصال به تلگرام ======================
bot = Bot(token=TOKEN)
updater = None


def send_telegram(text):
    # ۱. تلاش اول انجام می‌شود
    try:
        sent_message = bot.send_message(
            chat_id=CHAT_ID, text=text, parse_mode="Markdown"
        )
        return sent_message  # --- تغییر: به جای True، کل شیء پیام را برگردان ---

    except Exception as e:
        # ۲. اگر تلاش اول ناموفق بود، فقط یک بار هشدار ارسال می‌شود
        logging.error(f"Telegram Send Error retrying... (1/{RETRY_COUNT})")  #: {e}")
        # try:
        #     bot.send_message(chat_id=CHAT_ID, text="⚠️ Network unstable.", parse_mode='Markdown')
        # except Exception as e_warn:
        #     logging.error(f"⚠️Could not send the warning message: {e_warn}")
    finally:
        # 🔥 آزادسازی حافظه موقت
        if len(text) > 1000:  # اگر پیام خیلی بزرگ بود
            _cleanup_large_text()

    # ۳. حلقه تلاش‌های مجدد شروع می‌شود (چون تلاش اول ناموفق بود)
    for i in range(1, RETRY_COUNT):
        time.sleep(RETRY_DELAY)
        # logging.error(f"Telegram Send Error retrying... ({i+1}/{RETRY_COUNT})")
        try:
            # تلاش برای ارسال پیام اصلی
            sent_message = bot.send_message(
                chat_id=CHAT_ID, text=text, parse_mode="Markdown"
            )
            return sent_message  # --- تغییر: در حلقه هم شیء پیام را برگردان ---
        except Exception as e:
            if i > 10:
                bot.send_message(
                    chat_id=CHAT_ID, text="⚠️ Network unstable.", parse_mode="Markdown"
                )
            logging.error(
                f"Telegram Send Error retrying... ({i+1}/{RETRY_COUNT})"
            )  #: {e}")

    # اگر همه تلاش‌ها ناموفق بود
    logging.critical("❌Could not send message to Telegram after all retries.")
    # send_telegram("❌ Failed to send a message after multiple retries.")
    bot.send_message(
        chat_id=CHAT_ID,
        text="❌ Failed to send a message after multiple retries.",
        parse_mode="Markdown",
    )
    return None  # --- تغییر: در صورت شکست نهایی، None برگردان ---


def _cleanup_large_text():
    """آزادسازی حافظه برای متن‌های بزرگ"""
    import gc

    gc.collect()


# ----------------- تابع گزارش خطای شبکه listening -------------------
def handle_error(update, context):
    """خطاهای مربوط به شنونده تلگرام را مدیریت کرده و یک پیام ساده چاپ می‌کند."""
    # ما فقط برای خطاهای مربوط به شبکه پیام ساده چاپ می‌کنیم تا خطاهای مهم دیگر پنهان نشوند
    if "urllib3 HTTPError" in str(context.error) or "SSLEOFError" in str(context.error):
        logging.warning("Listener Network error")
    else:
        # برای خطاهای دیگر، جزئیات را چاپ می‌کنیم تا در صورت نیاز بتوانید آنها را رفع کنید
        logging.critical(f"listener unhandled error: {context.error}")


# -------------------- تابع های گزارش تاریخ دستی ----------------------------------------------
# ====================== توابع گزارش روز خاص ======================
def single_day_report_start(update, context):
    """مکالمه را برای دریافت گزارش یک روز خاص شروع می‌کند."""
    sent_messages_info = []
    prompt_text = "لطفاً تاریخ مورد نظر را در فرمت YYYY/MM/DD وارد کنید (مثال: 2025/09/01).\nبرای لغو، /cancel را ارسال کنید."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
    process_messages_for_clearing(sent_messages_info)
    return GET_SINGLE_DATE  # به مرحله دریافت تاریخ برو


def received_single_date(update, context):
    """تاریخ را دریافت کرده، گزارش آن روز را ساخته و مکالمه را تمام می‌کند."""
    sent_messages_info = []
    try:
        report_date = datetime.strptime(update.message.text, "%Y/%m/%d")

        # محاسبه شروع و پایان روز وارد شده
        naive_start_time = report_date.replace(hour=0, minute=0, second=0)
        naive_end_time = report_date.replace(hour=23, minute=59, second=59)

        start_time = make_aware(naive_start_time)
        end_time = make_aware(naive_end_time)

        # عنوان گزارش را بر اساس تاریخ ورودی تنظیم می‌کنیم
        report_title = f"روز {report_date.strftime('%Y/%m/%d')}"

        # فراخوانی موتور اصلی گزارش‌ساز
        generate_and_send_report(
            update.message, context, start_time, end_time, report_title
        )

        # پایان مکالمه
        return ConversationHandler.END

    except ValueError:
        prompt_text = "فرمت تاریخ اشتباه است. لطفاً دوباره در فرمت YYYY/MM/DD وارد کنید یا برای لغو /cancel را ارسال کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return GET_SINGLE_DATE  # در همین مرحله باقی بمان


def custom_report_start(update, context):
    sent_messages_info = []
    """مکالمه را برای دریافت گزارش سفارشی شروع می‌کند."""
    # ۱. متن پیام را در یک متغیر با نام مشخص قرار دهید
    prompt_text = "لطفاً تاریخ شروع را در فرمت YYYY/MM/DD وارد کنید (مثال: 2025/08/01).\nبرای لغو، /cancel را ارسال کنید."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
    process_messages_for_clearing(sent_messages_info)
    return START_DATE  # به مرحله بعدی (دریافت تاریخ شروع) برو


def received_start_date(update, context):
    sent_messages_info = []
    """تاریخ شروع را دریافت کرده و منتظر تاریخ پایان می‌ماند."""
    try:
        # تاریخ دریافت شده را در حافظه موقت مکالمه ذخیره می‌کنیم
        naive_start_time = datetime.strptime(update.message.text, "%Y/%m/%d")
        context.user_data["start_date"] = make_aware(naive_start_time)
        prompt_text = "عالی! حالا لطفاً تاریخ پایان را در همان فرمت وارد کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return END_DATE  # به مرحله بعدی (دریافت تاریخ پایان) برو
    except ValueError:
        prompt_text = "فرمت تاریخ اشتباه است. لطفاً دوباره در فرمت YYYY/MM/DD وارد کنید یا برای لغو /cancel را ارسال کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return START_DATE  # در همین مرحله باقی بمان


def received_end_date(update, context):
    sent_messages_info = []
    """تاریخ پایان را دریافت کرده، گزارش را ساخته و مکالمه را تمام می‌کند."""
    try:
        naive_end_date_input = datetime.strptime(update.message.text, "%Y/%m/%d")
        # برای اینکه معاملات روز پایان را هم شامل شود، آن را به انتهای روز منتقل می‌کنیم
        naive_end_time = naive_end_date_input.replace(hour=23, minute=59, second=59)
        end_time = make_aware(naive_end_time)
        # --- بخش جدید: چک می‌کنیم که تاریخ پایان از تاریخ شروع بزرگتر باشد ---
        start_time = context.user_data["start_date"]
        if end_time < start_time:
            prompt_text = "خطا: تاریخ پایان نمی‌تواند قبل از تاریخ شروع باشد. لطفاً تاریخ پایان را دوباره وارد کنید یا برای لغو /cancel را ارسال کنید."
            sent_msg = update.message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )
            process_messages_for_clearing(sent_messages_info)
            return END_DATE  # در همین مرحله باقی بمان تا کاربر تاریخ جدید را وارد کند
        start_time = context.user_data["start_date"]

        # فراخوانی موتور اصلی گزارش‌ساز با تاریخ‌های سفارشی
        generate_and_send_report(
            update.message, context, start_time, end_time, "سفارشی"
        )

        # پاک کردن حافظه موقت و پایان مکالمه
        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        prompt_text = "فرمت تاریخ اشتباه است. لطفاً دوباره در فرمت YYYY/MM/DD وارد کنید یا برای لغو /cancel را ارسال کنید."
        sent_msg = update.message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
        process_messages_for_clearing(sent_messages_info)
        return END_DATE  # در همین مرحله باقی بمان


def cancel_conversation(update, context):
    """مکالمه را لغو می‌کند."""
    sent_messages_info = []
    prompt_text = "لغو شد."
    sent_msg = update.message.reply_text(prompt_text)
    if sent_msg:
        sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
    process_messages_for_clearing(sent_messages_info)
    context.user_data.clear()
    return ConversationHandler.END


# ====================== بخش وب‌سرور برای دریافت هشدارها ======================
app = Flask(__name__)


@app.route("/alert", methods=["POST"])
def handle_alert():
    """این تابع هر زمان که یک هشدار از متاتریدر دریافت شود، اجرا می‌شود."""
    alert_message = request.data.decode("utf-8")

    # چاپ پیام در کنسول برای اطمینان از دریافت
    # logging.info(f"{alert_message}")

    # ارسال پیام به تلگرام
    # ما این کار را در یک ترد جداگانه انجام می‌دهیم تا سرور بلافاصله پاسخ دهد
    threading.Thread(
        target=send_alert_and_log, args=(alert_message,), name="Alert_Thread"
    ).start()

    return "OK", 200


def run_flask_server():
    # --- این دو خط، لاگ پیش‌فرض وب‌سرور را غیرفعال می‌کنند ---
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    """این تابع وب‌سرور را راه‌اندازی می‌کند."""
    # پارامتر use_reloader=False برای جلوگیری از خطا در تردها ضروری است
    app.run(host="127.0.0.1", port=5000, use_reloader=False)


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
            add_id_to_db(sent_message.message_id)  # +++ این خط را اضافه کنید +++
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
# ================ تابع محاسبه دراودان بالانس ==================
def calculate_drawdown_for_period(deals_history, start_date, end_date):
    """
    حداکثر دراودان اکوئیتی را برای یک بازه زمانی مشخص محاسبه می‌کند.
    این تابع از تاریخچه کل معاملات استفاده می‌کند تا اکوئیتی اولیه را به درستی پیدا کند.
    """
    if not deals_history:
        return {"amount": 0.0, "percent": 0.0}

    # مرتب‌سازی کل تاریخچه بر اساس زمان (برای اطمینان)
    deals_history = sorted(deals_history, key=lambda d: d.time_msc)

    # ۱. محاسبه اکوئیتی اولیه تا قبل از شروع بازه گزارش
    initial_equity = 0.0
    for deal in deals_history:
        deal_time = datetime.fromtimestamp(deal.time, tz=pytz.utc)
        if deal_time < start_date:
            initial_equity += deal.profit + deal.commission + deal.swap
        else:
            break  # معاملات وارد بازه گزارش شدند، محاسبه اولیه تمام است

    # ۲. محاسبه دراودان فقط برای معاملات داخل بازه گزارش
    max_equity_in_period = initial_equity
    current_equity = initial_equity
    max_drawdown_amount = 0.0

    for deal in deals_history:
        deal_time = datetime.fromtimestamp(deal.time, tz=pytz.utc)
        # فقط معاملاتی که در بازه زمانی ما هستند را پردازش کن
        if start_date <= deal_time <= end_date:
            current_equity += deal.profit + deal.commission + deal.swap

            if current_equity > max_equity_in_period:
                max_equity_in_period = current_equity

            current_drawdown = max_equity_in_period - current_equity

            if current_drawdown > max_drawdown_amount:
                max_drawdown_amount = current_drawdown

    # محاسبه درصد دراودان نسبت به بالاترین اکوئیتی در همان بازه
    # اگر قله منفی باشد، درصد دراودان معنا ندارد و صفر در نظر گرفته می‌شود
    if max_equity_in_period <= 0:
        max_drawdown_percent = 0.0
    else:
        max_drawdown_percent = max_drawdown_amount / max_equity_in_period * 100

    return {
        "amount": round(max_drawdown_amount, 2),
        "percent": round(max_drawdown_percent, 2),
    }


# ====================== تابع دکمه ی جزئیات و نمودار ======================
def report_button_handler(update, context):
    sent_messages_info = []
    """پاسخ دکمه‌های انتخاب نوع گزارش را مدیریت می‌کند."""
    query = update.callback_query
    query.answer()

    # --- بخش جدید برای مدیریت دکمه لغو ---
    if query.data == "cancel_operation":
        sent_msg = query.edit_message_text(text="عملیات لغو شد.")
        if sent_msg:
            sent_messages_info.append(
                {"id": sent_msg.message_id, "text": sent_msg.text}
            )
            process_messages_for_clearing(sent_messages_info)
        return  # از ادامه اجرای تابع جلوگیری می‌کند
    # --- پایان بخش جدید ---
    # داده‌های ارسالی از دکمه را جدا می‌کنیم، مثلا '7day_full'
    parts = query.data.split("_")
    report_type = parts[0]
    mode = "_".join(parts[1:])  # 'full' or 'chart_only'

    # محاسبه بازه زمانی بر اساس نوع گزارش
    end_time = get_server_time()
    start_time = None
    title = ""

    # این بلوک if/elif برای مدیریت انواع گزارش ضروری است
    if report_type == "time":
        title = "۲۴ ساعت گذشته"
        start_time = end_time - timedelta(hours=24)
    elif report_type == "today":
        start_time = make_aware(datetime.combine(end_time.date(), datetime.min.time()))
        title = "امروز"
    elif report_type == "3days":
        start_time = make_aware(
            datetime.combine(end_time.date() - timedelta(days=3), datetime.min.time())
        )
        title = "۳ روز گذشته"
    elif report_type == "thisweek":
        start_time = make_aware(
            datetime.combine(
                end_time.date() - timedelta(days=end_time.weekday()),
                datetime.min.time(),
            )
        )
        title = "هفته جاری"
    elif report_type == "7day":
        start_time = make_aware(
            datetime.combine(end_time.date() - timedelta(days=7), datetime.min.time())
        )
        title = "۷ روز گذشته"
    elif report_type == "14day":
        start_time = make_aware(
            datetime.combine(end_time.date() - timedelta(days=14), datetime.min.time())
        )
        title = "۱۴ روز گذشته"
    elif report_type == "30day":
        start_time = make_aware(
            datetime.combine(end_time.date() - timedelta(days=30), datetime.min.time())
        )
        title = "۳۰ روز گذشته"
    elif report_type == "60day":
        start_time = make_aware(
            datetime.combine(end_time.date() - timedelta(days=60), datetime.min.time())
        )
        title = "۶۰ روز گذشته"
    elif report_type == "thismonth":
        today = end_time.date()
        start_time = make_aware(datetime(today.year, today.month, 1))
        title = "ماه جاری"
    elif report_type == "90day":
        start_time = make_aware(
            datetime.combine(end_time.date() - timedelta(days=90), datetime.min.time())
        )
        title = "۹۰ روز گذشته"

    elif report_type == "yesterday":
        # محاسبه بازه زمانی دیروز
        yesterday_date = end_time.date() - timedelta(days=1)
        # شروع دیروز: ساعت ۰۰:۰۰:۰۰
        start_time = make_aware(datetime.combine(yesterday_date, datetime.min.time()))
        # پایان دیروز: ساعت ۲۳:۵۹:۵۹
        end_time = make_aware(
            datetime.combine(yesterday_date, datetime.max.time()).replace(microsecond=0)
        )
        title = "دیروز"
    elif report_type == "lastweek":
        # محاسبه بازه زمانی هفته گذشته (دوشنبه تا یکشنبه)
        today = end_time.date()
        last_saturday = today + relativedelta(weekday=SA(-1))
        end_time = make_aware(datetime.combine(last_saturday, datetime.min.time()))
        start_time = end_time - timedelta(days=7)
        title = "هفته گذشته"
    elif report_type == "last2weeks":
        today = end_time.date()
        last_saturday = today + relativedelta(weekday=SA(-1))
        end_time = make_aware(datetime.combine(last_saturday, datetime.min.time()))
        start_time = end_time - timedelta(days=14)
        title = "۲ هفته گذشته"
    elif report_type == "lastmonth":
        today = end_time.date()
        end_time = make_aware(datetime(today.year, today.month, 1))
        start_time = end_time - relativedelta(months=1)
        title = "ماه گذشته"
    elif report_type == "last2months":
        today = end_time.date()
        end_time = make_aware(datetime(today.year, today.month, 1))
        start_time = end_time - relativedelta(months=2)
        title = "۲ ماه گذشته"
    elif report_type == "last3months":
        today = end_time.date()
        end_time = make_aware(datetime(today.year, today.month, 1))
        start_time = end_time - relativedelta(months=3)
        title = "۳ ماه گذشته"

    sent_msg = query.edit_message_text(text=f"در حال تهیه گزارش {title}...")
    if sent_msg:
        sent_messages_info.append({"id": sent_msg.message_id, "text": sent_msg.text})
    process_messages_for_clearing(sent_messages_info)

    # حالا تابع اصلی را با حالت (mode) مناسب فراخوانی می‌کنیم
    if start_time and title:
        generate_and_send_report(
            query.message, context, start_time, end_time, title, mode
        )
    else:
        # اگر نوع گزارش تعریف نشده بود
        context.bot.send_message(
            chat_id=query.message.chat_id, text="خطا: نوع گزارش تعریف نشده است."
        )


def generate_and_send_report(
    message, context, start_time, end_time, title, mode="full"
):
    with _cleanup_after_report():
        log_memory_usage("")
        sent_messages_info = []  # <--- لیست محلی برای این تابع

        """موتور اصلی برای ساخت و ارسال تمام گزارش‌ها"""
        terminal_info = mt5.terminal_info()
        if not terminal_info or not terminal_info.connected:
            prompt_text = (
                "اسکریپت به متاتریدر متصل نیست. لطفاً چند لحظه دیگر دوباره تلاش کنید."
            )
            sent_msg = message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )
            process_messages_for_clearing(sent_messages_info)
            return
        # به جای 0، از یک تاریخ معقول در گذشته (مثلاً ۵ سال قبل) شروع می‌کنیم
        # این کار از محدودیت‌های احتمالی بروکر جلوگیری می‌کند
        # naive_start_time = datetime.combine(end_time.date() - timedelta(days=7), datetime.min.time())
        # start_time = make_aware(naive_start_time)
        start_date_for_history = end_time - timedelta(days=365 * 5)  # 5 years back
        # در ابتدا کل تاریخچه رو میگیریم بعدا فیلتر میکنیم که توی بازه نشون بده فقط
        deals = mt5.history_deals_get(start_date_for_history, end_time)
        # --- بخش جدید: گرفتن کل تاریخچه برای محاسبه دراودان ---
        all_deals_for_drawdown = mt5.history_deals_get(
            datetime(2000, 1, 1, tzinfo=pytz.utc), end_time
        )

        if not deals:
            prompt_text = f"در بازه زمانی گزارش ({title}) هیچ معامله‌ای یافت نشد."
            sent_msg = message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )
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
                continue  # این دیل را نادیده بگیر و برو سراغ دیل بعدی
            # --- این بلوک کد جدید را اضافه کنید ---
            # این بلاک برای محاسبه ی این متغییر ها در بازه ی گزارش است
            deal_time = datetime.fromtimestamp(deal.time, tz=pytz.utc)
            if start_time <= deal_time <= end_time:
                total_balance_change_period += deal.profit + deal.commission + deal.swap
                commission += deal.commission
                swap += deal.swap
            # --- پایان بلوک جدید ---
            total_profit += (
                deal.commission + deal.swap
            )  # سود کل معاملات تاریخچه نه بازه
            # --- بخش جدید برای تجمیع اطلاعات پوزیشن ---
            position_id = deal.position_id
            if position_id not in positions:
                positions[position_id] = {
                    "profit": 0.0,
                    "volume": 0.0,
                    "symbol": deal.symbol,
                    "close_time": 0,
                    "trade_volume": 0.0,
                }

            positions[position_id][
                "profit"
            ] += deal.profit  # + deal.commission + deal.swap

            if deal.entry == mt5.DEAL_ENTRY_IN:
                positions[position_id]["volume"] += deal.volume
                # positions[position_id]['close_time'] = deal.time # اگه این فعال کنی و اونو غیر فعال پوزیشنها بر اساس زمان باز شدن مرتب میشن
                # فقط حجم اولین معامله ورودی را به عنوان حجم کل پوزیشن در نظر می‌گیریم
                if positions[position_id]["trade_volume"] == 0:
                    positions[position_id]["trade_volume"] = deal.volume
            elif deal.entry in (
                mt5.DEAL_ENTRY_OUT,
                mt5.DEAL_ENTRY_OUT_BY,
                mt5.DEAL_ENTRY_INOUT,
            ):
                positions[position_id]["volume"] -= deal.volume
                positions[position_id][
                    "close_time"
                ] = deal.time  # زمان آخرین خروج ثبت می‌شود
            # --- پایان بخش جدید ---
            if deal.entry in (
                mt5.DEAL_ENTRY_OUT,
                mt5.DEAL_ENTRY_OUT_BY,
                mt5.DEAL_ENTRY_INOUT,
            ):
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
            is_closed = abs(pos_data["volume"]) < 0.01

            if is_closed and pos_data["close_time"] > 0:
                # زمان را به صورت آگاه از منطقه زمانی (UTC) ایجاد می‌کنیم
                close_datetime = datetime.fromtimestamp(
                    pos_data["close_time"], tz=pytz.utc
                )
                # logging.info("position id: ", pos_id," close time: ", close_datetime)
                # شرط ۲: زمان بسته شدن پوزیشن باید در بازه گزارش اصلی باشد
                if start_time <= close_datetime <= end_time:
                    final_positions[pos_id] = pos_data
        # --- پایان بخش جدید ---
        active_trading_days_set = set()
        # مرتب‌سازی پوزیشن‌ها بر اساس زمان بسته شدن برای نمایش به ترتیب
        sorted_positions = sorted(
            final_positions.items(), key=lambda item: item[1]["close_time"]
        )

        # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
        # این بلوک کد را برای پیدا کردن تاریخ اولین ترید اضافه کنید
        # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
        first_trade_date_str = "---"  # مقدار پیش‌فرض
        if sorted_positions:
            # زمان اولین پوزیشن بسته شده در بازه را می‌گیریم
            first_trade_timestamp = sorted_positions[0][1]["close_time"]
            first_trade_dt_utc = datetime.fromtimestamp(
                first_trade_timestamp, tz=pytz.utc
            )

            # به منطقه زمانی بروکر تبدیل می‌کنیم
            broker_tz = pytz.timezone(BROKER_TIMEZONE)
            first_trade_dt_broker = first_trade_dt_utc.astimezone(broker_tz)
            first_trade_date_str = first_trade_dt_broker.strftime("%Y/%m/%d")
        # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

        for position_id, pos_data in sorted_positions:
            # یک پوزیشن زمانی کاملا بسته شده که حجم باقی‌مانده آن نزدیک به صفر باشد
            if abs(pos_data["volume"]) < 0.01 and pos_data["close_time"] > 0:
                utc_time = datetime.fromtimestamp(pos_data["close_time"], tz=pytz.utc)
                broker_tz = pytz.timezone(BROKER_TIMEZONE)
                broker_dt_object = utc_time.astimezone(broker_tz)
                trade_date = broker_dt_object.strftime("%y/%m/%d %H:%M:%S")
                active_trading_days_set.add(broker_dt_object.date())

                # شمارنده وین ریت قدیمی همچنان برای مقایسه محاسبه می‌شود
                closed_trades_count += 1
                # ... کد قبلی شما
                if pos_data["profit"] >= 0:
                    win_count += 1
                    profit_trades_count += 1
                    total_profit_sum += pos_data["profit"]
                    if pos_data["profit"] > max_profit:
                        max_profit = pos_data["profit"]
                else:  # اگر معامله با ضرر بسته شده بود
                    loss_trades_count += 1
                    total_loss_sum += pos_data[
                        "profit"
                    ]  # ضررهایی که منفی هستند،جمع می‌کنیم
                    if pos_data["profit"] < max_loss:
                        max_loss = pos_data["profit"]

                line = f"{trade_counter:02d}-{pos_data['symbol']}|{pos_data['trade_volume']:.2f}|{pos_data['profit']:>8,.2f}|{trade_date}"
                # line = f"{trade_counter:02d}-{pos_data['symbol']}|{pos_data['trade_volume']:.2f}|{pos_data['profit']:>8,.2f}|{trade_date}"
                report_lines.append(f"`{line}`")
                trade_counter += 1
        # --- پایان بخش جدید ---

        avg_profit = (
            total_profit_sum / profit_trades_count if profit_trades_count > 0 else 0.0
        )
        avg_loss = total_loss_sum / loss_trades_count if loss_trades_count > 0 else 0.0

        if not report_lines:
            prompt_text = f"در بازه زمانی گزارش ({title}) هیچ پوزیشنی بسته نشده است."
            sent_msg = message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )
            process_messages_for_clearing(sent_messages_info)
            return

        win_rate = (
            (win_count / closed_trades_count * 100) if closed_trades_count > 0 else 0
        )
        total_profit_sign = "✅" if total_profit >= 0 else "🔻"

        # --- بخش جدید: محاسبه دقیق سود و رشد ---
        account_info = mt5.account_info()
        profit_line = ""
        growth_line = ""
        Not_available = ""  # اگر توی گزارش مقداری نبود این کاراکتر

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
            is_live_report = False  # پیش‌فرض را روی تاریخی می‌گذاریم

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
                starting_balance_period = (
                    account_info.balance - total_balance_change_period
                )
                actual_trading_days_count = len(active_trading_days_set)
                actual_date_report = (
                    f"اولین ترید(روز معاملاتی): ‎{first_trade_date_str}‏ ({str(actual_trading_days_count)})\n"
                    if actual_trading_days_count > 1
                    else ""
                )

                for position_id, pos_data in sorted_positions:
                    # یک پوزیشن زمانی کاملا بسته شده که حجم باقی‌مانده آن نزدیک به صفر باشد
                    if abs(pos_data["volume"]) < 0.01 and pos_data["close_time"] > 0:
                        # --- بخش جدید: محاسبه هوشمند برد، باخت و سر به سر ---
                        threshold_amount = starting_balance_period * (
                            WINRATE_THRESHOLD_PERCENT / 100.0
                        )

                        if pos_data["profit"] > threshold_amount:
                            real_win_count += 1
                        elif pos_data["profit"] < -threshold_amount:
                            real_loss_count += 1
                        else:
                            breakeven_count += 1

                # --- بخش جدید: گرفتن اطلاعات بالانس و اکوییتی ---
                account_info = mt5.account_info()
                balance_equity_line = (
                    f"**موجودی ابتدای بازه:**`‎{starting_balance_period:,.2f}`‏\n**موجودی(حال):**‎`{account_info.balance:>8.2f}`**\nاکوییتی(حال):**`{account_info.equity:,.2f}`\n"
                    if account_info
                    else ""
                )
                current_balance = f"{account_info.balance:,.2f}"
                current_equity = (
                    f"{account_info.equity:,.2f}" if account_info else Not_available
                )
                display_end_time = end_time
            else:
                logging.info("Generating historical report...")
                # این یک گزارش تاریخ خاص است، از فرمول پیچیده‌تر استفاده کن
                # ابتدا سود معاملاتی که بعد از بازه گزارش انجام شده را پیدا می‌کنیم
                deals_after_period = mt5.history_deals_get(end_time, get_server_time())
                profit_after_period = 0.0
                if deals_after_period:
                    for d in deals_after_period:
                        if d.entry in (
                            mt5.DEAL_ENTRY_IN,
                            mt5.DEAL_ENTRY_OUT,
                            mt5.DEAL_ENTRY_OUT_BY,
                            mt5.DEAL_ENTRY_INOUT,
                        ):
                            profit_after_period += d.profit + d.commission + d.swap
                # logging.info(f"Profit from deals after the period: {profit_after_period}")
                # بالانس در انتهای بازه = بالانس فعلی - سود معاملات بعدی
                balance_at_period_end = account_info.balance - profit_after_period
                # logging.info(f"Current balance: {account_info.balance}")
                # logging.info(f"Balance at period end: {balance_at_period_end}")
                # بالانس ابتدای بازه = بالانس انتهای بازه - سود خود بازه
                starting_balance_period = (
                    balance_at_period_end - total_balance_change_period
                )

                for position_id, pos_data in sorted_positions:
                    # یک پوزیشن زمانی کاملا بسته شده که حجم باقی‌مانده آن نزدیک به صفر باشد
                    if abs(pos_data["volume"]) < 0.01 and pos_data["close_time"] > 0:
                        # --- بخش جدید: محاسبه هوشمند برد، باخت و سر به سر ---
                        threshold_amount = starting_balance_period * (
                            WINRATE_THRESHOLD_PERCENT / 100.0
                        )

                        if pos_data["profit"] > threshold_amount:
                            real_win_count += 1
                        elif pos_data["profit"] < -threshold_amount:
                            real_loss_count += 1
                        else:
                            breakeven_count += 1

                # --- گرفتن اطلاعات بالانس تاریخی ---
                balance_equity_line = (
                    f"**موجودی ابتدای بازه:** `‎{starting_balance_period:,.2f}‏`\n**موجودی انتهای بازه:**`‎{balance_at_period_end:,.2f}`\n"
                    if balance_at_period_end and starting_balance_period
                    else ""
                )
                historical_end_balance = (
                    f"{balance_at_period_end:,.2f}"
                    if balance_at_period_end and starting_balance_period
                    else Not_available
                )
                # این شرط تضمین می‌کند که یک روز از تاریخ پایان فقط و فقط زمانی کم شود که گزارش شما یک گزارش تاریخی باشد و زمان پایان آن دقیقاً ساعت ۰۰:۰۰ بامداد باشد.
                # این کار باعث می‌شود که گزارش سفارشی شما (که زمان پایان آن ۲۳:۵۹ است) به درستی و بدون تغییر نمایش داده شود.
                if end_time.time() == datetime.min.time():
                    display_end_time = end_time - timedelta(days=1)
                else:
                    display_end_time = end_time
                # برای گزارش‌های تاریخی، یک روز از تاریخ پایان کم می‌کنیم تا بازه درست نمایش داده شود
                # display_end_time = end_time - timedelta(days=1)

            profit_line = f"**سود اکانت(حال):**‎`{true_total_account_profit:>8.2f}$`‏\n**سود بازه:** ‎`{total_balance_change_period:,.2f}$`\n"

            # --- محاسبه درصد رشد کل اکانت ---
            initial_deposit = account_info.balance - true_total_account_profit
            total_growth_percentage = 0.0
            if initial_deposit != 0:
                total_growth_percentage = (
                    true_total_account_profit / initial_deposit
                ) * 100
            total_growth_sign = "+" if total_growth_percentage >= 0 else ""
            total_growth_str = f"{total_growth_sign}{total_growth_percentage:.2f}%"

            # --- محاسبه درصد رشد بازه زمانی ---
            period_growth_percentage = 0.0
            if starting_balance_period != 0:
                period_growth_percentage = (
                    total_balance_change_period / starting_balance_period
                ) * 100
            period_growth_sign = "+" if period_growth_percentage >= 0 else ""
            period_growth_str = f"{period_growth_sign}{period_growth_percentage:.2f}%"

            growth_line = f"**درصد رشد اکانت(حال): **‎`{total_growth_str}`‏\n**درصد رشد بازه: **‎`{period_growth_str}`\n"
            broker_account_line = (
                f"`{account_info.company} | {account_info.login}`\n"
                if account_info
                else ""
            )

            # --- بخش جدید: فراخوانی تابع دراودان برای دو حالت ---
            # فراخوانی برای کل حساب
            total_drawdown_info = calculate_drawdown_for_period(
                all_deals_for_drawdown, datetime(2000, 1, 1, tzinfo=pytz.utc), end_time
            )
            # فراخوانی برای بازه گزارش
            period_drawdown_info = calculate_drawdown_for_period(
                all_deals_for_drawdown, start_time, end_time
            )
            drawdown_line = (
                f"**دراودان کل:** `‎{total_drawdown_info['amount']:.2f}$`‎(`{total_drawdown_info['percent']:.2f}%`)\n"
                f"**دراودان بازه:** `‎{period_drawdown_info['amount']:.2f}$`‎(`{period_drawdown_info['percent']:.2f}%`)\n"
            )
            reward_ratio = (avg_profit / abs(avg_loss)) if avg_loss != 0 else None
            reward_ratio_str = (
                f"{reward_ratio:.2f}" if reward_ratio is not None else "---"
            )

            summary_old = (
                f"**📊 گزارش {title}**\n"
                f"_{start_time.strftime('%Y/%m/%d')} - {display_end_time.strftime('%Y/%m/%d')}_\n\n"
                f"{actual_date_report}"
                f"{balance_equity_line}"
                f"{profit_line}"
                f"{drawdown_line}"
                f"{growth_line}"
                f"کمیسیون بازه:`‎{commission:.2f}`‏|سواپ بازه:‎`{swap:.2f}`\n"
                f"**نرخ برد بازه: **‎`{win_rate:.2f}%` ‏({win_count}/{closed_trades_count})\n"
                f"**🏆نرخ برد واقعی: **`‎{((real_win_count / (real_win_count + real_loss_count) * 100) if (real_win_count + real_loss_count) > 0 else 0):.2f}%` ‏({real_win_count}/{real_win_count + real_loss_count})\n"
                f"**معاملات سر به سر:** `{breakeven_count}`\n"
                f"بیشترین س،ض: ‎{max_profit:,.2f}‏|‎{max_loss:,.2f}$\n"
                f"میانگین س،ض: ‎{avg_profit:,.2f}‏|‎{avg_loss:,.2f}$\n"
                # f"میانگین ریوارد: ‎{(avg_profit / abs(avg_loss)) if avg_loss != 0 else '':.2f}\n"
                f"⛷️میانگین ریوارد: ‎{reward_ratio_str}\n"
                f"**ت. پوزیشن‌های بازه:**`{closed_trades_count}`\n"
                f"{broker_account_line}"
                f"-------------------------------------------"
            )

            # داده‌های جدول
            rows = [
                ["شاخص", "بازه", "اکنون"],
                ["موجودی", f"{starting_balance_period:,.2f}", Not_available],
                [
                    "موجودی پایان",
                    historical_end_balance + current_balance,
                    Not_available,
                ],
                ["اکوئیتی", Not_available, current_equity],  # تا اینجا فکر کنم درسته
                [
                    "سود خالص",
                    f"{total_balance_change_period:,.2f}$",
                    f"{true_total_account_profit:,.2f}$",
                ],
                ["رشد", f"{period_growth_str}", f"{total_growth_str}"],
                [
                    "حداکثر افت حساب",
                    f"(%{period_drawdown_info.get('percent', 0):.2f})${period_drawdown_info.get('amount', 0):.2f}",
                    f"(%{total_drawdown_info.get('percent', 0):.2f})${total_drawdown_info.get('amount', 0):.2f}",
                ],
                [
                    "نرخ برد",
                    f"({win_count}/{closed_trades_count})%{win_rate:.2f}",
                    Not_available,
                ],
                [
                    "نرخ برد واقعی",
                    f"({real_win_count}/{real_win_count + real_loss_count})%{((real_win_count / (real_win_count + real_loss_count) * 100) if (real_win_count + real_loss_count) > 0 else 0):.2f}",
                    Not_available,
                ],
                ["سر به سر", f"{breakeven_count}", Not_available],
                ["بیشترین س،ض$", f"{max_loss:.2f},{max_profit:.2f}", Not_available],
                ["میانگین س،ض$", f"{avg_loss:.2f},{avg_profit:.2f}", Not_available],
                ["میانگین ریوارد", f"{reward_ratio_str}", Not_available],
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
            lines.append(
                f"`بازه  : ‎{start_time.strftime('%Y/%m/%d')}-{display_end_time.strftime('%Y/%m/%d')}`"
            )
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
            # sent_msg = message.reply_text(summary)

        sent_msg = message.reply_text(summary_old, parse_mode="Markdown")
        if sent_msg:
            sent_messages_info.append({"id": sent_msg.message_id, "text": summary_old})
        # sent_msg = message.reply_text(summary, parse_mode="Markdown")
        # if sent_msg:
        #     sent_messages_info.append({"id": sent_msg.message_id, "text": summary})
        time.sleep(1)
        if mode == "full":
            # --- بخش جدید: اضافه کردن هدر ---
            prompt_text = f"#N| Symbol | lot   |          Profit | Date"
            sent_msg = message.reply_text(prompt_text, parse_mode="Markdown")
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )

            CHUNK_SIZE = 40
            for i in range(0, len(report_lines), CHUNK_SIZE):
                chunk = report_lines[i : i + CHUNK_SIZE]
                message_part = "\n".join(chunk)
                sent_msg = message.reply_text(message_part, parse_mode="Markdown")
                if sent_msg:
                    sent_messages_info.append(
                        {"id": sent_msg.message_id, "text": message_part}
                    )
                time.sleep(1)

        # ساخت لیست تمیز از پوزیشن‌های نهایی برای ارسال به تابع نمودار
        fully_closed_positions = [
            pos_data for position_id, pos_data in sorted_positions
        ]
        # فراخوانی تابع برای ساخت و ارسال نمودار
        create_and_send_growth_chart(
            message, context, fully_closed_positions, starting_balance_period, title
        )
        prompt_text = "End report.\nmonitoring continue..."
        sent_msg = message.reply_text(prompt_text)
        if sent_msg:
            sent_messages_info.append({"id": sent_msg.message_id, "text": prompt_text})
        process_messages_for_clearing(sent_messages_info)

        # حذف داده‌های بزرگ
        deals = None
        all_deals_for_drawdown = None
        sorted_positions = None
        report_lines = None
        fully_closed_positions = None
        # # 🔥 آزادسازی حافظه بعد از اتمام گزارش
        # _cleanup_after_report()


@contextmanager
def _cleanup_after_report():
    """مدیریت خودکار حافظه برای گزارش‌گیری"""
    try:
        yield
    finally:
        import gc

        try:
            import matplotlib.pyplot as plt

            plt.close("all")
            plt.clf()
            plt.cla()
        except:
            pass
        gc.collect()
        logging.info("🧹 Report memory cleaned")
        log_memory_usage("After report cleanup")


# ====================== رسم نمودار رشد ======================
def create_and_send_growth_chart(
    message, context, fully_closed_positions, starting_balance, title
):
    with _cleanup_chart_memory():
        sent_messages_info = []
        """نمودار رشد حساب را ساخته و به تلگرام ارسال می‌کند."""
        logging.info("Creating growth chart...")

        # # ۱. آماده‌سازی داده‌ها
        # dates = []
        # cumulative_profit = []
        # current_equity = starting_balance

        # # مرتب کردن معاملات بر اساس زمان
        closed_deals = fully_closed_positions  # sorted([d for d in fully_closed_positions if d.entry == mt5.DEAL_ENTRY_OUT], key=lambda x: x.time)

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
            sent_msg = message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )
            process_messages_for_clearing(sent_messages_info)
            return

        # اضافه کردن نقطه شروع نمودار (معامله شماره صفر)
        trade_numbers.append(0)
        cumulative_profit.append(starting_balance)

        # محاسبه سود تجمعی برای هر معامله
        for i, position_data in enumerate(closed_deals):
            # --- این خط را برای دیباگ اضافه کنید ---
            # logging.info(f"Trade #{i+1}: current_equity_before={current_equity}, profit_to_add={position_data['profit']}")
            current_equity += position_data[
                "profit"
            ]  # + position_data['commission'] + position_data['swap']
            trade_numbers.append(i + 1)  # شماره معامله (۱، ۲، ۳، ...)
            cumulative_profit.append(current_equity)
        # --- این شرط حیاتی را اضافه کنید ---
        if len(trade_numbers) < 4:
            logging.warning("Not enough data to create a chart.")
            prompt_text = "تعداد معاملات برای رسم نمودار کافی نیست."
            sent_msg = message.reply_text(prompt_text)
            if sent_msg:
                sent_messages_info.append(
                    {"id": sent_msg.message_id, "text": prompt_text}
                )
            process_messages_for_clearing(sent_messages_info)
            # در صورت تمایل می‌توانید یک پیام مناسب به کاربر تلگرام بفرستید
            # sent_msg = message.reply_text("تعداد معاملات برای رسم نمودار کافی نیست.")
            return  # از ادامه تابع و بروز خطا جلوگیری می‌کند

        # ۲. رسم نمودار با خطوط منحنی و نرم
        # تبدیل لیست‌های عادی به آرایه‌های NumPy برای محاسبات
        x_original = np.array(trade_numbers)
        y_original = np.array(cumulative_profit)

        # ایجاد نقاط بسیار بیشتر برای رسم یک منحنی نرم
        x_smooth = np.linspace(x_original.min(), x_original.max(), 400)

        # ساخت مدل ریاضی (spline) و محاسبه مقادیر y برای نقاط جدید
        spl = make_interp_spline(x_original, y_original, k=3)  # k=3 for a cubic spline
        y_smooth = spl(x_smooth)

        # ۲. رسم نمودار با کیفیت و اندازه بزرگ
        plt.figure(figsize=(12, 7), dpi=150)  # اندازه ۱۲x۷ اینچ با کیفیت ۱۵۰ DPI

        # اگر سود نهایی مثبت بود، نمودار سبز باشد، در غیر این صورت قرمز
        line_color = "green" if current_equity >= starting_balance else "red"

        # رسم منحنی نرم
        plt.plot(x_smooth, y_smooth, color=line_color, linewidth=0.4)

        # اضافه کردن نقاط معاملات اصلی روی منحنی
        plt.scatter(
            x_original, y_original, color=line_color, s=1
        )  # s=1 for marker size

        # تغییر: رسم بر اساس شماره معامله به جای تاریخ
        # plt.plot(trade_numbers, cumulative_profit, linestyle='-', color=line_color, marker='.', markersize=1, linewidth=0.4, label='Cumulative Profit')

        # این خط دیگر نیازی نیست و باید حذف یا کامنت شود
        # plt.xticks(rotation=45)
        # --- بخش جدید: آماده‌سازی متن فارسی برای نمایش صحیح ---
        # ابتدا متن فارسی را برای اتصال حروف، آماده می‌کنیم
        reshaped_title_text = arabic_reshaper.reshape(f"نمودار رشد: {title}")
        # سپس، متن آماده شده را از راست به چپ مرتب می‌کنیم
        bidi_title_text = get_display(reshaped_title_text)

        # زیباسازی نمودار
        plt.title(bidi_title_text, fontsize=16, fontname="Tahoma")
        plt.xlabel("trade number", fontsize=6)
        plt.ylabel("balance", fontsize=6)
        plt.grid(True, linestyle="--", alpha=0.2)

        # --- بخش جدید: تنظیم هوشمند محورها ---
        ax = plt.gca()  # گرفتن محورهای فعلی نمودار

        # محور افقی (تعداد معاملات) را طوری تنظیم کن که حداکثر 100 عدد صحیح نمایش دهد
        ax.xaxis.set_major_locator(MaxNLocator(nbins=100, integer=True))
        plt.xticks(fontname="calibri", fontsize=6, rotation=80)
        # --- تغییر کلیدی: تنظیم نقطه شروع محور افقی ---
        plt.xlim(left=0)  # محور افقی را مجبور کن که از صفر شروع شود

        # محور عمودی (ارزش حساب) را طوری تنظیم کن که حداکثر 50 عدد خوانا نمایش دهد
        ax.yaxis.set_major_locator(MaxNLocator(nbins=50))
        plt.yticks(fontname="calibri", fontsize=6)

        # --- تغییر کلیدی: تنظیم دستی پرش اعداد در محور افقی ---
        # تنظیم اعداد محور افقی با فونت مشخص
        # plt.xticks(range(0, len(trade_numbers), 1), fontname='calibri', fontsize=6)
        # --- تغییر کلیدی: تنظیم دستی پرش اعداد در محور عمودی ---
        # تنظیم اعداد محور عمودی با فونت مشخص
        # plt.yticks(range(int(min(cumulative_profit)), int(max(cumulative_profit)) + 100, 20), fontname='calibri', fontsize=6)
        plt.tight_layout()  # برای جلوگیری از بریده شدن برچسب‌ها

        # ۳. ذخیره نمودار در حافظه RAM
        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        buf.seek(0)

        # ۴. ارسال تصویر به تلگرام
        logging.info("Sending chart to Telegram...")
        send_msg = message.reply_photo(photo=buf, caption=f"نمودار رشد: {title}")
        if send_msg:
            sent_messages_info.append(
                {"id": send_msg.message_id, "text": f"نمودار رشد: {title}"}
            )
        # بستن نمودار برای آزاد کردن حافظه
        plt.close()
        buf.close()
        logging.info("RAM freed.")
        logging.info("Monitoring continue...")
        process_messages_for_clearing(sent_messages_info)

        # حذف داده‌های بزرگ
        position_data = None
        closed_deals = None
        trade_numbers = None
        # # 🔥 آزادسازی حافظه فوری بعد از ارسال نمودار
        # _cleanup_chart_memory()


@contextmanager
def _cleanup_chart_memory():
    """آزادسازی حافظه مربوط به نمودار"""
    try:
        yield
    finally:
        import gc

        # تمیزکاری matplotlib
        try:
            import matplotlib.pyplot as plt

            plt.close("all")
            plt.clf()
            plt.cla()
        except:
            pass

        # garbage collector
        gc.collect()
        logging.info("🧹 Chart memory cleaned")


# ============================================== گزارش روزانه ===========================================================
def _24H_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="time_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="time_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def today_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="today_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="today_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _3days_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="3days_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="3days_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _this_week_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="thisweek_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="thisweek_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _7day_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="7day_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="7day_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _14day_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="14day_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="14day_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _30day_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="30day_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="30day_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _this_month_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="thismonth_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="thismonth_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _60day_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="60day_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="60day_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def _90day_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="90day_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="90day_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


# --------------------توابع گزارش‌گیری بر اساس هفته و ماه--------------------


def yesterday_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="yesterday_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="yesterday_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def last_week_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="lastweek_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="lastweek_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def last_2_weeks_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="last2weeks_full"),
            InlineKeyboardButton(
                "📈 فقط نمودار", callback_data="last2weeks_chart_only"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def last_month_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="lastmonth_full"),
            InlineKeyboardButton("📈 فقط نمودار", callback_data="lastmonth_chart_only"),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def last_2_months_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="last2months_full"),
            InlineKeyboardButton(
                "📈 فقط نمودار", callback_data="last2months_chart_only"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


def last_3_months_report(update, context):
    keyboard = [
        [
            InlineKeyboardButton("📄 با جزئیات", callback_data="last3months_full"),
            InlineKeyboardButton(
                "📈 فقط نمودار", callback_data="last3months_chart_only"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو", callback_data="cancel_operation"
            )  # <-- این ردیف جدید را اضافه کنید
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "لطفاً نوع گزارش را انتخاب کنید:", reply_markup=reply_markup
    )


# ====================== توابع قالب‌بندی پیام‌ها ======================
def format_market_order_open(deal, order=None):
    """قالب پیام برای باز شدن پوزیشن مارکت"""
    side = "🔵 Buy" if deal.type == mt5.DEAL_TYPE_BUY else "🔴 Sell"
    comment_text = (
        f"`Comment : {order.comment}\n\n`"
        if order and getattr(order, "comment", None)
        else ""
    )
    # زمان با ms در deal.time_msc مانند تابع دیگر
    utc_time = (
        datetime.fromtimestamp(deal.time_msc / 1000, tz=pytz.utc)
        if getattr(deal, "time_msc", None)
        else datetime.fromtimestamp(deal.time, tz=pytz.utc)
    )
    broker_tz = pytz.timezone(BROKER_TIMEZONE)
    broker_dt_object = utc_time.astimezone(broker_tz)
    milliseconds = deal.time_msc % 1000 if getattr(deal, "time_msc", None) else 0
    broker_time_str = (
        f"{broker_dt_object.strftime('%y/%m/%d..%H:%M:%S')}.{milliseconds:03d}"
    )

    account_info = mt5.account_info()
    balance_equity_line = (
        f"`Bal|Eq  : {account_info.balance:,.2f}|{account_info.equity:,.2f}`\n"
        if account_info
        else ""
    )
    broker_account_line = (
        f"`{account_info.company}|Acc: {account_info.login}`\n" if account_info else ""
    )

    return (
        f"**----- Market -----**\n\n"
        f"{broker_account_line}"
        f"{balance_equity_line}"
        f"``{deal.symbol} {side} {deal.volume} \n"
        f"`{deal.position_id}`\n"
        f"{comment_text}"
        f"`{broker_time_str}`"
    )


def format_pending_order_filled(deal, order):
    """قالب پیام برای فعال شدن اردر بر اساس deal و order"""
    side = "🔵 Buy" if deal.type == mt5.DEAL_TYPE_BUY else "🔴 Sell"
    comment_text = f"`Comment : {order.comment}\n\n`" if order.comment else ""
    utc_time = datetime.fromtimestamp(deal.time_msc / 1000, tz=pytz.utc)
    broker_tz = pytz.timezone(BROKER_TIMEZONE)
    broker_dt_object = utc_time.astimezone(broker_tz)
    milliseconds = deal.time_msc % 1000
    broker_time_str = (
        f"{broker_dt_object.strftime('%y/%m/%d..%H:%M:%S')}.{milliseconds:03d}"
    )
    # --- بخش جدید: گرفتن اطلاعات حساب ---
    account_info = mt5.account_info()
    balance_equity_line = (
        f"`Bal|Eq  : {account_info.balance:,.2f}|{account_info.equity:,.2f}`\n"
        if account_info
        else ""
    )
    broker_account_line = (
        f"`{account_info.company}|Acc: {account_info.login}`\n" if account_info else ""
    )

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


def format_position_closed(
    deal,
    original_order_comment,
    initial_volume,
    is_complete_close,
    total_position_profit,
    total_position_commission,
    total_position_swap,
    total_sign,
):
    """قالب پیام برای بسته شدن پوزیشن با کامنت اصلی"""
    side = "🔴 Sell" if deal.type == mt5.DEAL_TYPE_BUY else "🔵 Buy"
    result = "ℹ️ Manually Closed"
    # ==== متد 1 ====
    # if deal.reason == 3 or '[tp' in deal.comment.lower(): result = "✅ TP"
    # elif deal.reason == 4 or '[sl' in deal.comment.lower(): result = "❌ SL"

    # ==== متد 2 ====
    if "[tp" in deal.comment.lower():
        result = "✅ TP"
    elif "[sl" in deal.comment.lower():
        result = "❌ SL"

    # ==== متد 3 ====
    # # یک معامله فقط زمانی TP است که دلیل آن TP بوده و حجم آن برابر با حجم اولیه باشد (خروج کامل)
    # if (deal.reason == 3 or '[tp' in deal.comment.lower()) and deal.volume == initial_volume:
    #     result = "✅ TP"
    # # یک معامله فقط زمانی SL است که دلیل آن SL بوده و حجم آن برابر با حجم اولیه باشد
    # elif (deal.reason == 4 or '[sl' in deal.comment.lower()) and deal.volume == initial_volume:
    #     result = "❌ SL"

    comment_text = (
        f"`Comment: {original_order_comment}`\n\n" if original_order_comment else ""
    )
    utc_time = datetime.fromtimestamp(deal.time_msc / 1000, tz=pytz.utc)
    broker_tz = pytz.timezone(BROKER_TIMEZONE)
    broker_dt_object = utc_time.astimezone(broker_tz)
    milliseconds = deal.time_msc % 1000
    broker_time_str = (
        f"{broker_dt_object.strftime('%y/%m/%d..%H:%M:%S')}.{milliseconds:03d}"
    )
    # --- بخش جدید: گرفتن اطلاعات حساب ---
    account_info = mt5.account_info()
    balance_equity_line = (
        f"`Bal|Eq : {account_info.balance:,.2f}|{account_info.equity:,.2f}`\n"
        if account_info
        else ""
    )
    # --- بخش جدید: ساخت خط اطلاعات بروکر و حساب ---
    broker_account_line = (
        f"`{account_info.company} | {account_info.login}`\n" if account_info else ""
    )
    # --- بخش جدید: نمایش هوشمند حجم معامله ---
    commission_pos = 0.0
    swap_pos = 0.0
    if deal.volume < initial_volume and initial_volume > 0:
        if is_complete_close:
            # اگر خروج کامل باشد
            position_close_title = f"**⚔️ Position Closed (Complete)**"
            lots = f"{deal.volume:.2f}/{initial_volume:.2f}"
            # اگر خروج کامل بود، سود کل را هم نمایش بده
            p_sign = "+" if deal.profit > 0 else ""
            profit = f"{p_sign}{deal.profit:,.2f}$ ({total_sign}{total_position_profit:,.2f}$)"
            commission_pos = (
                f"{total_position_commission:,.2f}$"
                if total_position_commission
                else "0$"
            )
            swap_pos = f"{total_position_swap:,.2f}$" if total_position_swap else "0$"
        else:
            # اگر حجم بسته شده کمتر از حجم اولیه بود (خروج بخشی)
            position_close_title = f"**⚔️ Position Closed (Partial)**"
            lots = f"{deal.volume:.2f}/{initial_volume:.2f}"
            # در غیر این صورت، فقط سود این بخش را نمایش بده
            p_sign = "+" if deal.profit > 0 else ""
            profit = f"{p_sign}{deal.profit:,.2f} $"

    else:  # یعنی پارشیال نبوده
        # در غیر این صورت، فقط حجم بسته شده را نمایش بده
        position_close_title = f"**⚔️ Position Closed**"
        lots = f"{deal.volume:.2f}"
        # در غیر این صورت، فقط سود این بخش را نمایش بده
        p_sign = "+" if deal.profit > 0 else ""
        profit = f"{p_sign}{deal.profit:,.2f} $"
        commission_pos = (
            f"{total_position_commission:,.2f}$" if total_position_commission else "0$"
        )
        swap_pos = f"{total_position_swap:,.2f}$" if total_position_swap else "0$"

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
        mt5.ORDER_TYPE_BUY_LIMIT: "Buy Limit",
        mt5.ORDER_TYPE_SELL_LIMIT: "Sell Limit",
        mt5.ORDER_TYPE_BUY_STOP: "Buy Stop",
        mt5.ORDER_TYPE_SELL_STOP: "Sell Stop",
    }
    return type_map.get(order.type, "Pending")


def get_server_time():
    #     """زمان سرور بروکر را با منطقه زمانی صحیح برمی‌گرداند."""
    #     last_tick = mt5.symbol_info_tick("BTCUSD")
    #     if last_tick and last_tick.time > 0:
    #         utc_time = datetime.fromtimestamp(last_tick.time, tz=pytz.utc)
    #         broker_tz = pytz.timezone(BROKER_TIMEZONE)
    #         return utc_time.astimezone(broker_tz)
    #     else:
    #         return None

    # def get_server_time():
    PRIORITY_BASE_SYMBOLS = ["BTCUSD", "XAUUSD"]

    # 2. دریافت لیست تمام نمادهای موجود در سرور
    # این لیست برای پیدا کردن پسوندها ضروری است.
    all_symbols_on_server = mt5.symbols_get()

    selected_full_symbol = None

    for base_symbol in PRIORITY_BASE_SYMBOLS:

        # 3. پیدا کردن نماد کامل (شامل پسوند)
        # این بخش نمادی مثل "XAUUSD.pe" یا "BTCUSD" (بدون پسوند) را پیدا می‌کند
        matching_symbols = [
            s.name for s in all_symbols_on_server if s.name.startswith(base_symbol)
        ]

        if matching_symbols:
            # اولین نماد تطابق داده شده را انتخاب می‌کنیم (معمولاً درست‌ترین است)
            full_symbol = matching_symbols[0]
            last_tick = None
            # logging.info(f"trying: {full_symbol}...")
            # 💡 فعال کردن نماد در واچ‌لیست
            while True:
                if not mt5.symbol_select(full_symbol, True):
                    while True:
                        if not mt5.initialize(path=MT5_PATH):
                            logging.error("mt5 not initialized, retrying...")
                            time.sleep(RECONNECT_DELAY / 2)
                        else:
                            logging.info("mt5 initialized successfully.")
                            break
                    logging.error(
                        f"⚠️ can't add {full_symbol} to watchlist, error code: {mt5.last_error()}"
                    )
                    time.sleep(0.5)  # صبر کن و دوباره تلاش کن
                else:
                    time.sleep(1)  # صبر کن تا سرور به‌روزرسانی کند
                    try:
                        # 4. سعی در گرفتن آخرین تیک نماد کامل
                        last_tick = mt5.symbol_info_tick(full_symbol)
                    except Exception as e:
                        logging.error(
                            f"⚠️ Error retrieving tick for {full_symbol}: {e}"
                        )
                        continue
                    break  # موفق شدیم، از حلقه خارج می‌شویم

            if last_tick and last_tick.time > 0:
                selected_full_symbol = full_symbol
                break  # نماد را پیدا کردیم، از حلقه خارج می‌شویم

    # --- بخش اجرای نهایی در صورت پیدا شدن نماد ---
    if selected_full_symbol:
        # 6. محاسبه و برگرداندن زمان
        utc_time = datetime.fromtimestamp(last_tick.time, tz=pytz.utc)
        try:
            broker_tz = pytz.timezone(BROKER_TIMEZONE)
        except NameError:
            logging.error("❌ BROKER_TIMEZONE is not defined.")
            return None

        return utc_time.astimezone(broker_tz)
    # --- بخش مدیریت خطا در صورت عدم موفقیت ---
    else:
        logging.error(
            "❌ Failed to retrieve server time using either BTCUSD or XAUUSD."
        )
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
    update.message.reply_text(
        f"در حال پاک‌سازی {len(alert_message_ids)} پیام غیر ضروری..."
    )

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
            logging.warning(
                f"Could not delete message ID {msg_id}, Permanent error: {e}"
            )
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
        confirmation_parts.append(
            f"❌ {failed_permanently_count} پیام قابل حذف نبود (قدیمی یا ناموجود)."
        )

    if failed_temporarily_count > 0:
        confirmation_parts.append(
            f"⚠️ {failed_temporarily_count} پیام به دلیل خطای شبکه حذف نشد (در اجرای بعدی دوباره تلاش خواهد شد)."
        )

    confirmation_message = "\n".join(confirmation_parts)
    update.message.reply_text(confirmation_message)

    remaining_count = len(alert_message_ids)
    logging.info(
        f"Del:{deleted_count},FP:{failed_permanently_count},FT:{failed_temporarily_count},R:{remaining_count}."
    )
    # 🔥 آزادسازی حافظه بعد از پاک‌سازی
    _cleanup_alert_memory()


def _cleanup_alert_memory():
    """آزادسازی حافظه مربوط به هشدارها"""
    import gc

    # اگر لیست هشدارها خیلی بزرگ شده، می‌توانید آن را trim کنید
    global alert_message_ids
    if len(alert_message_ids) > 1000:  # محدود کردن سایز لیست
        alert_message_ids = alert_message_ids[-500:]  # نگه داشتن آخرین 500 تا

    gc.collect()
    logging.info("🧹 Alert memory cleaned")


def process_messages_for_clearing(sent_messages_info):
    """
    یک لیست از اطلاعات پیام‌های ارسال شده را گرفته، آن‌ها را بررسی کرده
    و شناسه‌های پیام‌های غیرمهم را به لیست اصلی حذفی‌ها اضافه می‌کند.
    """
    # logging.info(f"Processing {len(sent_messages_info)} sent messages for clearing...")
    for msg_info in sent_messages_info:
        # شرط را برای هر پیام ذخیره شده اجرا می‌کنیم
        is_important = any(keyword in msg_info["text"] for keyword in KEYWORDS_TO_KEEP)

        # اگر پیام مهم نبود، شناسه‌اش را به لیست اصلی حذفی‌ها اضافه کن
        if not is_important:
            alert_message_ids.append(msg_info["id"])
            add_id_to_db(msg_info["id"])  # شناسه را در پایگاه داده هم ذخیره می‌کند
            # logging.info(f"Message ID {msg_info['id']} marked for clearing.")


# ====================== تابع اصلی مانیتورینگ ======================
def main():
    floating_profit.start_monitoring()
    # +++ این دو خط را اضافه کنید +++
    setup_database()  # پایگاه داده را آماده می‌کند
    global alert_message_ids
    alert_message_ids = load_ids_from_db()  # شناسه‌های قدیمی را بارگذاری می‌کند

    # --- تغییر کلیدی: راه‌اندازی وب‌سرور در یک ترد پس‌زمینه ---
    flask_thread = threading.Thread(
        target=run_flask_server, daemon=True, name="Flask_Thread"
    )
    flask_thread.start()
    logging.info("Alert Server is running.")
    setup_smtp_server()  # راه‌اندازی سرور SMTP
    # این حلقه تا زمانی که اینترنت آماده شود، ادامه دارد
    while True:
        # تلاش برای راه‌اندازی شنونده تلگرام
        try:
            global updater
            updater = Updater(TOKEN, use_context=True)
            dispatcher = updater.dispatcher
            # --- بخش جدید: ساخت و ثبت مکالمه برای گزارش سفارشی ---
            conv_handler = ConversationHandler(
                entry_points=[CommandHandler("custom_report", custom_report_start)],
                states={
                    START_DATE: [
                        MessageHandler(
                            Filters.text & ~Filters.command, received_start_date
                        )
                    ],
                    END_DATE: [
                        MessageHandler(
                            Filters.text & ~Filters.command, received_end_date
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", cancel_conversation)],
            )

            # v-- این بلوک کد جدید را اینجا اضافه کنید --v
            single_day_conv_handler = ConversationHandler(
                entry_points=[CommandHandler("day_report", single_day_report_start)],
                states={
                    GET_SINGLE_DATE: [
                        MessageHandler(
                            Filters.text & ~Filters.command, received_single_date
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", cancel_conversation)],
            )
            # ^-- پایان بلوک کد جدید --^
            dispatcher.add_handler(conv_handler)
            dispatcher.add_handler(single_day_conv_handler)
            dispatcher.add_handler(CallbackQueryHandler(report_button_handler))
            # در بخش main بعد از ایجاد dispatcher
            dispatcher.add_handler(CommandHandler("test_email", test_email_command))
            dispatcher.add_handler(CommandHandler("clear", clear_alerts))
            dispatcher.add_handler(CommandHandler("time", _24H_report))
            dispatcher.add_handler(CommandHandler("today", today_report))
            dispatcher.add_handler(CommandHandler("3days", _3days_report))
            dispatcher.add_handler(CommandHandler("7day", _7day_report))
            dispatcher.add_handler(CommandHandler("thisweek", _this_week_report))
            dispatcher.add_handler(CommandHandler("14day", _14day_report))
            dispatcher.add_handler(CommandHandler("30day", _30day_report))
            dispatcher.add_handler(CommandHandler("thismonth", _this_month_report))
            dispatcher.add_handler(CommandHandler("60day", _60day_report))
            dispatcher.add_handler(CommandHandler("90day", _90day_report))
            dispatcher.add_handler(CommandHandler("yesterday", yesterday_report))
            dispatcher.add_handler(CommandHandler("lastweek", last_week_report))
            dispatcher.add_handler(CommandHandler("last2weeks", last_2_weeks_report))
            dispatcher.add_handler(CommandHandler("lastmonth", last_month_report))
            dispatcher.add_handler(CommandHandler("last2months", last_2_months_report))
            dispatcher.add_handler(CommandHandler("last3months", last_3_months_report))
            # در تابع main
            dispatcher.add_handler(CommandHandler("start_float", start_floating_window))
            dispatcher.add_handler(CommandHandler("stop_float", stop_floating_window))
            dispatcher.add_handler(CommandHandler("float_status", floating_status))
            dispatcher.add_error_handler(handle_error)
            # اگر همه چیز موفق بود، از حلقه راه‌اندازی خارج می‌شویم
            updater.start_polling()  # راه اندازی شنونده
            logging.info("Listener started successfully.")
            # logging.info("Telegram connection successful. Starting main operations.")
            break

        except Exception as e:
            # اگر اینترنت وصل نبود، خطا را چاپ کرده و حلقه را تکرار می‌کنیم
            logging.error(f"initial listener run fail Retrying in 10 seconds...")
            time.sleep(10)
            continue

    is_connected = False
    disconnect_time = None
    last_check_time = None  # در ابتدا خالی است و پس از اولین اتصال مقداردهی می‌شود
    processed_deals = set()

    # برای شروع تمیز، معاملات یک ساعت اخیر را بر اساس زمان سرور نادیده می‌گیریم
    if mt5.initialize(path=MT5_PATH):
        # ------------------
        server_time_now = get_server_time()
        # اگر زمان سرور در دسترس بود، ادامه بده
        if server_time_now:
            last_check_time = server_time_now
            initial_deals = mt5.history_deals_get(
                server_time_now - timedelta(hours=1), server_time_now
            )
            if initial_deals:
                processed_deals.update(d.ticket for d in initial_deals)
        else:
            # اگر زمان سرور در دسترس نبود، بعدا در حلقه اصلی تلاش می‌کنیم
            last_check_time = None
        # mt5.shutdown() # اتصال اولیه را می‌بندیم
    else:
        # اگر اتصال اولیه برقرار نشد، بعدا تلاش می‌کنیم
        last_check_time = None
    # ------------------
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
                time.sleep(0.1)  # ⬅️ فقط 0.1 ثانیه!برای نرم‌تر کردن بار استفاده از CPU
                if new_deals:
                    for deal in new_deals:
                        if deal.ticket in processed_deals:
                            continue

                        # if deal.entry == mt5.DEAL_ENTRY_IN:
                        #     order = mt5.history_orders_get(ticket=deal.order)
                        #     if order and order[0].type in [2, 3, 4, 5]:
                        #         msg = format_pending_order_filled(deal, order[0])
                        #         send_telegram(msg)
                        if deal.entry == mt5.DEAL_ENTRY_IN:
                            order = mt5.history_orders_get(ticket=deal.order)
                            if order and order[0].type in [2, 3, 4, 5]:
                                # Pending order filled
                                msg = format_pending_order_filled(deal, order[0])
                                send_telegram(msg)
                            else:
                                # Market order opened (یا order ممکن است None باشد)
                                order_obj = order[0] if order else None
                                msg = format_market_order_open(deal, order_obj)
                                send_telegram(msg)

                        elif deal.entry in (
                            mt5.DEAL_ENTRY_OUT,
                            mt5.DEAL_ENTRY_OUT_BY,
                            mt5.DEAL_ENTRY_INOUT,
                        ):
                            original_comment = ""
                            initial_volume = 0.0  # متغیر جدید برای حجم اولیه
                            # --- بخش جدید: محاسبه حجم کل بسته شده ---
                            total_closed_volume = 0.0
                            is_complete_close = False

                            # --- بخش جدید: محاسبه سود کل پوزیشن ---
                            total_position_profit = 0.0
                            total_position_commission = 0.0
                            total_position_swap = 0.0
                            position_deals = mt5.history_deals_get(
                                position=deal.position_id
                            )
                            if position_deals:
                                for opening_deal in position_deals:
                                    if opening_deal.entry == mt5.DEAL_ENTRY_IN:
                                        # حجم و کامنت را از معامله ورودی استخراج می‌کنیم
                                        initial_volume = opening_deal.volume
                                        opening_order = mt5.history_orders_get(
                                            ticket=opening_deal.order
                                        )
                                        # total_position_profit += opening_deal.commission + opening_deal.swap
                                        total_position_commission += (
                                            opening_deal.commission
                                        )
                                        total_position_swap += opening_deal.swap

                                        if opening_order:
                                            original_comment = opening_order[0].comment

                                    # جمع زدن حجم تمام معاملات خروجی
                                    if (
                                        opening_deal.entry
                                        in (
                                            mt5.DEAL_ENTRY_OUT,
                                            mt5.DEAL_ENTRY_OUT_BY,
                                            mt5.DEAL_ENTRY_INOUT,
                                        )
                                        and opening_deal.time_msc <= deal.time_msc
                                    ):
                                        total_closed_volume += opening_deal.volume
                                        # سود تمام معاملات خروجی را جمع می‌زنیم
                                        total_position_profit += (
                                            opening_deal.profit
                                        )  # + opening_deal.commission + opening_deal.swap
                                        total_position_commission += (
                                            opening_deal.commission
                                        )
                                        total_position_swap += opening_deal.swap

                                total_sign = "+" if total_position_profit > 0 else ""
                                # چک می‌کنیم که آیا حجم کل بسته شده با حجم اولیه برابر است
                                # (با یک خطای کوچک برای اعداد اعشاری)
                                if abs(total_closed_volume - initial_volume) < 0.001:
                                    is_complete_close = True
                            # ارسال اطلاعات کامل به تابع قالب‌بندی
                            msg = format_position_closed(
                                deal,
                                original_comment,
                                initial_volume,
                                is_complete_close,
                                total_position_profit,
                                total_position_commission,
                                total_position_swap,
                                total_sign,
                            )
                            send_telegram(msg)

                        processed_deals.add(deal.ticket)

                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                logging.critical(f"Connection to MT5 lost")
                send_telegram("⚠️ Connection to MT5 lost. reconnect...")
                is_connected = False
                disconnect_time = time.time()
                mt5.shutdown()
                time.sleep(RECONNECT_DELAY)
                continue
        else:
            # --- حالت قطع شده: تلاش برای اتصال مجدد ---
            logging.info("Connecting to MetaTrader 5...")

            if disconnect_time and (time.time() - disconnect_time > OVERALL_TIMEOUT):
                logging.error(
                    f"Could not reconnect within {int(OVERALL_TIMEOUT/60)} minutes. Shutting down for good."
                )
                send_telegram(
                    f"❌ Could not reconnect to MT5 for {int(OVERALL_TIMEOUT/60)} minutes. Bot is shutting down."
                )
                break

            if mt5.initialize(path=MT5_PATH):
                if disconnect_time:
                    logging.info("Reconnected to MT5 successfully!")
                    send_telegram("✅ Reconnected to MT5. Monitoring resumed.")
                else:  # در غیر این صورت این اولین اتصال است
                    logging.info("Connected to MT5 successfully!")
                    send_telegram("✅ *Bot is running*\nMonitoring...")

                is_connected = True
                disconnect_time = None
                # تغییر: ریست کردن زمان پس از اتصال بر اساس زمان سرور
                last_check_time = get_server_time()

                # این بخش دیگر ضروری نیست چون ما تاریخچه را چک می‌کنیم نه پوزیشن‌های باز را
                positions_result = mt5.positions_get()
                last_known_positions = (
                    {p.ticket: p for p in positions_result} if positions_result else {}
                )
                # logging.info(f"Ignoring {len(last_known_positions)} existing position(s).")
                # send_telegram(f"{len(last_known_positions)} existing position(s).")
                # --- بخش جدید: ساخت و ارسال لیست پوزیشن‌های باز ---
                logging.info(
                    f"Ignoring {len(last_known_positions)} existing position(s)."
                )
                logging.info("Monitoring...")
                # اگر پوزیشنی باز بود، لیست آنها را تهیه و ارسال کن
                if last_known_positions:
                    position_lines = []
                    # ساخت هدر پیام
                    header = f"{len(last_known_positions)} position exist"
                    position_lines.append(header)

                    # اضافه کردن جزئیات هر پوزیشن به لیست
                    for ticket, position in last_known_positions.items():
                        side = (
                            "Buy" if position.type == mt5.POSITION_TYPE_BUY else "Sell"
                        )
                        lot = position.volume
                        profit = position.profit
                        p_sign = "+" if profit > 0 else ""
                        # header = f"Symbol  |Side  |Lots   |Profit"
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
                logging.error(
                    f"Connection failed. Retrying in {RECONNECT_DELAY} seconds..."
                )
                time.sleep(RECONNECT_DELAY)
                # mt5.initialize(path=MT5_PATH)#فقط به خاطر اینکه متاتریدر اگه اجرا نبود اجرا بشه

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
try:
    while True:
        # حالا که منطقه زمانی با موفقیت پیدا شده، برنامه اصلی را اجرا کن
        try:
            main()
        except KeyboardInterrupt:
            send_telegram("ℹ️ *Script Stopped Manually*")
            logging.info("Script stopped by user.")
            break  # <--- این break برای خروج از حلقه نگهبان ضروری است

        except Exception as e:
            # اول خطای اصلی را در لاگ کنسول ثبت کن
            # logging.critical(f"Critical unhandled error caught: {e}")
            logging.critical(f"Critical Error: {e}")
            # حالا سعی کن به تلگرام هم خبر بدهی، اما نگذار این تلاش خودش باعث کرش شود
            try:
                send_telegram(f"❌ *CRITICAL ERROR*\nBot has crashed!\nError: {e}")
            except Exception as report_error:
                # اگر حتی ارسال گزارش خطا هم شکست خورد، فقط در لاگ کنسول بنویس
                logging.error(
                    f"Could not send the final crash notification to Telegram: {report_error}"
                )

            # کمی صبر می‌کنیم تا از حلقه کرش سریع (crash-loop) جلوگیری شود
            if updater and updater.running:
                # تغییر ۳: در نهایت، چه با خطا و چه با Ctrl+C، شنونده را متوقف می‌کنیم
                logging.info("{wait}Stopping updater...")
                updater.stop()
                logging.info("Updater stopped.")

            logging.info("Restarting the script(30s)...")
            time.sleep(30)
            # سپس حلقه نگهبان دوباره main() را اجرا می‌کند
finally:
    # === این بخش حیاتی است ===
    # این کد *همیشه* هنگام خروج از بلوک try اجرا می‌شود
    # (یعنی زمانی که KeyboardInterrupt باعث 'break' شده است)

    # logging.info("Gracefully shutting down all components...")

    # 1. توقف پنجره شناور (اگر در حال اجرا باشد)
    # (با فرض اینکه floating_profit یک متغیر سراسری است)
    # if "floating_profit" in globals() and floating_profit.is_monitoring:
    #     logging.info("Stopping floating profit window...")
    #     floating_profit.stop_monitoring()
    #     # --- بخش حیاتی: منتظر بمانید تا نخ GUI تمام شود ---
    #     if floating_profit.gui_thread and floating_profit.gui_thread.is_alive():
    #         logging.info("Waiting for GUI thread to finish...")
    #         floating_profit.gui_thread.join(timeout=3)
    #     logging.info("Floating window stopped.")

    # 1. خاموش کردن تضمینی پنجره شناور (با انتظار)
    if "floating_profit" in globals():
        floating_profit.clean_exit()  # <--- استفاده از متد جدید
    # 2. توقف شنونده تلگرام
    if updater and updater.running:
        logging.info("Stopping updater...")
        updater.stop()
        logging.info("Updater stopped.")

    # 3. قطع اتصال از MT5
    if mt5.terminal_info():
        logging.info("Shutting down MT5 connection...")
        mt5.shutdown()
        logging.info("MT5 connection shut down.")

    # نخ Flask چون daemon=True است، به طور خودکار بسته خواهد شد
    logging.info("Script exited gracefully.")
    # check_active_threads()  # <--- بررسی نهایی
# if updater and updater.running:
#     # تغییر ۳: در نهایت، چه با خطا و چه با Ctrl+C، شنونده را متوقف می‌کنیم
#     logging.info("{wait}Stopping updater...")
#     updater.stop()
#     logging.info("Updater stopped.")
# if mt5.terminal_info():
#     mt5.shutdown()
# logging.info("Script exited gracefully.")
