# -*- coding: utf-8 -*-

import os
import time
import logging
import threading
from iqoptionapi.stable_api import IQ_Option
import pandas as pd
import pandas_ta as ta
import sys
import datetime

# ==============================================================================
# --- 1. إعدادات السكريبت ---
# ==============================================================================

# -- بيانات الدخول (استخدم بياناتك الحقيقية هنا) --
EMAIL = "Salehmagdy124@gmail.com"
PASSWORD = "01227372440Saleh"
ACCOUNT_TYPE = "PRACTICE"

# -- إعدادات التداول --
MINIMUM_PAYOUT = 70 # لن يتم البحث عن إشارة إذا كانت نسبة الربح أقل من هذا الرقم


def _bool_env(var_name, default):
    value = os.getenv(var_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# -- إعدادات التنفيذ التلقائي --
AUTO_TRADE = _bool_env("AUTO_TRADE", True)  # شغّل/طفّي التنفيذ التلقائي
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "1"))  # قيمة الصفقة
EXPIRY_MIN = int(os.getenv("EXPIRY_MIN", "1"))  # انتهاء الصفقة بالدقائق (١ دقيقة)
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "120"))  # فترة تبريد لكل أصل لمنع تكرار الدخول
last_trade_ts = {}

# -- حدود إدارة المخاطر اليومية --
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "20"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "50"))  # بالدولار مثلاً

daily_trades = 0
daily_pnl = 0.0
daily_lock = threading.Lock()
last_daily_reset = datetime.date.today()

# -- قوائم الأصول للمسح (مقسمة حسب أيام الأسبوع) --
WEEKDAY_ASSETS_TO_SCAN = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", 
    "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"
]
WEEKEND_ASSETS_TO_SCAN = [
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDCAD-OTC",
    "EURJPY-OTC", "GBPJPY-OTC", "NZDUSD-OTC", "AUDUSD-OTC"
]

# -- إعدادات التحليل الفني (استراتيجية تقاطع المتوسطات النشطة) --
TIMEFRAME = 1 * 60      # إطار زمني دقيقة واحدة لمزيد من الإشارات
CANDLE_COUNT = 50
FAST_MA_PERIOD = 9      # المتوسط المتحرك السريع
SLOW_MA_PERIOD = 21     # المتوسط المتحرك البطيء

# ==============================================================================
# --- 2. إعداد الاتصال والوظائف الأساسية ---
# ==============================================================================

# إعداد نظام تسجيل الأحداث لعرض المعلومات بشكل واضح
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
Iq = IQ_Option(EMAIL, PASSWORD)

def connect_to_iq_option():
    """يقوم بالاتصال أو إعادة الاتصال بالمنصة بشكل آمن."""
    logger.info("Attempting to connect to IQ Option...")
    check, reason = Iq.connect()
    if check:
        logger.info("✅ Successfully connected!")
        Iq.change_balance(ACCOUNT_TYPE)
        logger.info(f"👍 Switched to {ACCOUNT_TYPE} account.")
        return True
    else:
        logger.error(f"❌ Connection failed, reason: {reason}")
        return False


def get_open_state(iq, asset):
    """
    يرجع حالتي الفتح لـ Digital و Turbo للأصل المطلوب.
    """
    try:
        ot = iq.get_all_open_time()
        digital_open = bool(ot.get('digital', {}).get(asset, {}).get('open'))
        turbo_open   = bool(ot.get('turbo',   {}).get(asset, {}).get('open'))
        return digital_open, turbo_open
    except Exception as e:
        logger.error(f"⚠️ تعذر جلب حالة فتح الأصل {asset}: {e}")
        return False, False


def reset_daily_counters_if_needed():
    """إعادة تعيين عدادات التداول اليومية عند الانتقال إلى يوم جديد."""
    global daily_trades, daily_pnl, last_daily_reset
    today = datetime.date.today()
    with daily_lock:
        if today != last_daily_reset:
            daily_trades = 0
            daily_pnl = 0.0
            last_daily_reset = today
            logger.info("🔄 تمت إعادة تعيين عدادات التداول اليومية.")


def may_continue():
    """التحقق من حدود إدارة المخاطر قبل الدخول في صفقة جديدة."""
    reset_daily_counters_if_needed()
    with daily_lock:
        if daily_trades >= MAX_DAILY_TRADES:
            logger.warning("⛔ وصلنا الحد اليومي للصفقات")
            return False
        if daily_pnl <= -MAX_DAILY_LOSS:
            logger.warning("⛔ وصلنا حد الخسارة اليومية")
            return False
    return True


def _await_trade_result(iq, order_id, asset, direction, is_digital):
    """الانتظار حتى تُغلق الصفقة ثم إعادة صافي الربح/الخسارة."""
    try:
        if is_digital:
            while True:
                status, result = iq.check_win_digital_v2(order_id)
                if status:
                    return result
                time.sleep(2)
        else:
            while True:
                result = iq.check_win_v4(order_id)
                if result is not None:
                    return result
                time.sleep(2)
    except Exception as exc:
        logger.error(f"⚠️ تعذر التحقق من نتيجة الصفقة {order_id} على {asset}: {exc}")
    return 0.0


def _monitor_trade_result(iq, order_id, asset, direction, is_digital):
    """متابعة نتيجة الصفقة لتحديث عدادات المخاطر اليومية."""
    global daily_pnl
    profit = _await_trade_result(iq, order_id, asset, direction, is_digital)
    with daily_lock:
        daily_pnl += profit

    if profit > 0:
        logger.info(f"✅ الصفقة {direction.upper()} على {asset} انتهت بربح {profit:.2f}$")
    elif profit < 0:
        logger.info(f"❌ الصفقة {direction.upper()} على {asset} انتهت بخسارة {profit:.2f}$")
    else:
        logger.info(f"➖ الصفقة {direction.upper()} على {asset} انتهت بالتعادل")


def place_trade(iq, asset, direction):
    """إرسال صفقة مع مراعاة التبريد واختيار الأداة الأنسب (Digital ثم Turbo)."""
    reset_daily_counters_if_needed()

    now = time.time()
    if now - last_trade_ts.get(asset, 0) < COOLDOWN_S:
        logger.info(f"⏳ تبريد مفعّل لـ {asset}.. تخطي الدخول")
        return False

    digital_open, turbo_open = get_open_state(iq, asset)

    ok = False
    order_id = None
    is_digital = False

    # 1) جرّب Digital إذا كان مفتوحًا
    if digital_open:
        logger.info(f"🛒 محاولة تنفيذ Digital على {asset} | {direction.upper()} {TRADE_AMOUNT}$ لمدة {EXPIRY_MIN} دقيقة")
        is_digital = True
        ok, order_id = iq.buy_digital_spot(asset, TRADE_AMOUNT, direction, EXPIRY_MIN)
        if not ok:
            logger.info(f"↩️ فشل Digital على {asset}، سنحاول Turbo إن أمكن")
            is_digital = False  # سنحوّل لمحاولة Turbo

    # 2) إن لم ينجح Digital وTurbo مفتوح — جرّب Turbo
    if not ok and turbo_open:
        logger.info(f"🛒 محاولة تنفيذ Turbo على {asset} | {direction.upper()} {TRADE_AMOUNT}$ لمدة {EXPIRY_MIN} دقيقة")
        ok, order_id = iq.buy(TRADE_AMOUNT, asset, direction, EXPIRY_MIN)

    if ok:
        logger.warning(f"🧾 أُرسلت صفقة {direction.upper()} على {asset} بقيمة {TRADE_AMOUNT} | id={order_id} | mode={'DIGITAL' if is_digital else 'TURBO'}")
        last_trade_ts[asset] = now
        global daily_trades
        with daily_lock:
            daily_trades += 1
        threading.Thread(
            target=_monitor_trade_result,
            args=(iq, order_id, asset, direction, is_digital),
            daemon=True,
        ).start()
        return True
    else:
        if not (digital_open or turbo_open):
            logger.error(f"❌ فشل إرسال الصفقة: {asset} مغلق الآن (لا Digital ولا Turbo).")
        else:
            logger.error(f"❌ فشل إرسال الصفقة على {asset} رغم كون الأداة مفتوحة. تحقق من صلاحية الحساب أو توقيت الشمعة.")
        return False

# ==============================================================================
# --- 3. وظائف الاستراتيجية والتحليل ---
# ==============================================================================

def get_tradable_assets(base_assets, notified_assets):
    """
    يقوم بمسح السوق ويعيد قائمة بالأصول المفتوحة والتي لديها نسبة ربح جيدة
    والتي لم يتم إرسال تنبيه بشأنها مؤخراً.
    """
    logger.info("Scanning market for tradable assets...")
    try:
        all_profit = Iq.get_all_profit()
        tradable_assets = []
        now = time.time()
        # نجلب أوقات الفتح مرة واحدة لهذه الدورة
        ot = Iq.get_all_open_time()

        for asset in base_assets:
            # تبريد التنبيهات
            if asset in notified_assets and now < notified_assets[asset]:
                continue

            digital_open = bool(ot.get('digital', {}).get(asset, {}).get('open'))
            turbo_open   = bool(ot.get('turbo',   {}).get(asset, {}).get('open'))

            if not (digital_open or turbo_open):
                continue  # الأصل غير مفتوح بأي أداة

            ap = all_profit.get(asset, {})
            # أعلى عائد متاح بين digital و turbo
            payout_candidates = []
            if digital_open and 'digital' in ap and ap['digital']:
                payout_candidates.append(ap['digital'] * 100)
            if turbo_open and 'turbo' in ap and ap['turbo']:
                payout_candidates.append(ap['turbo'] * 100)

            payout = max(payout_candidates) if payout_candidates else 0
            if payout >= MINIMUM_PAYOUT:
                tradable_assets.append({'name': asset, 'payout': payout})

        if tradable_assets:
            logger.info(f"Found {len(tradable_assets)} tradable assets to monitor.")
        else:
            logger.warning("No tradable assets with sufficient payout found at the moment.")
        return tradable_assets
    except Exception as e:
        logger.error(f"Could not scan market due to an error: {e}")
        return []


def get_and_prepare_data(asset):
    """يجلب بيانات الشموع ويضيف إليها المتوسطات المتحركة."""
    candles = Iq.get_candles(asset, TIMEFRAME, CANDLE_COUNT, time.time())
    if not candles:
        logger.warning(f"Could not fetch candle data for {asset}.")
        return None
    df = pd.DataFrame(candles)
    df.rename(columns={'min': 'low', 'max': 'high'}, inplace=True)
    
    # إضافة المتوسطات المتحركة للتحليل
    df.ta.sma(length=FAST_MA_PERIOD, append=True)
    df.ta.sma(length=SLOW_MA_PERIOD, append=True)
    return df

def check_signal_ma_cross(df):
    """يتحقق من وجود إشارة بناءً على استراتيجية تقاطع المتوسطات."""
    if df is None or len(df) < 3: return "NONE"
    
    # الحصول على آخر شمعتين مكتملتين للمقارنة
    prev_candle = df.iloc[-3]
    last_candle = df.iloc[-2]
    
    fast_ma_col = f'SMA_{FAST_MA_PERIOD}'
    slow_ma_col = f'SMA_{SLOW_MA_PERIOD}'

    # إشارة الشراء (التقاطع الذهبي)
    if prev_candle[fast_ma_col] < prev_candle[slow_ma_col] and last_candle[fast_ma_col] > last_candle[slow_ma_col]:
        return "CALL"

    # إشارة البيع (تقاطع الموت)
    if prev_candle[fast_ma_col] > prev_candle[slow_ma_col] and last_candle[fast_ma_col] < last_candle[slow_ma_col]:
        return "PUT"
        
    return "NONE"

# ==============================================================================
# --- 4. حلقة توليد الإشارات الرئيسية ---
# ==============================================================================

def run_signal_generator():
    """الحلقة الرئيسية التي تشغل البوت بشكل مستمر ومستقر."""
    if not connect_to_iq_option(): sys.exit()

    notified_assets = {} # لمنع إرسال تنبيهات متكررة لنفس الأصل
    NOTIFICATION_COOLDOWN = 5 * 60 # فترة تهدئة 5 دقائق لكل أصل بعد إرسال تنبيه
    
    # التحديد الذكي لقائمة الأصول حسب يوم الأسبوع
    base_asset_list = WEEKEND_ASSETS_TO_SCAN if datetime.datetime.now().weekday() >= 5 else WEEKDAY_ASSETS_TO_SCAN
    market_type = "OTC (Weekend)" if datetime.datetime.now().weekday() >= 5 else "Forex (Weekday)"

    logger.info(f"==================================================================")
    logger.info(f"🤖 PROFESSIONAL SIGNAL GENERATOR - STARTED SUCCESSFULLY ?")
    logger.info(f"==================================================================")
    logger.info(f"Today is {datetime.datetime.now().strftime('%A')}, scanning for {market_type} assets.")
    logger.info(f"💡 Strategy Activated: Active MA Cross Strategy (1-min timeframe)")
    logger.info(
        f"CONFIG => AUTO_TRADE={AUTO_TRADE}, TRADE_AMOUNT={TRADE_AMOUNT}, EXPIRY_MIN={EXPIRY_MIN}, "
        f"COOLDOWN_S={COOLDOWN_S}, MAX_DAILY_TRADES={MAX_DAILY_TRADES}, MAX_DAILY_LOSS={MAX_DAILY_LOSS}"
    )

    while True:
        try:
            reset_daily_counters_if_needed()

            # فحص الاتصال في بداية كل دورة لضمان الموثوقية
            if not Iq.check_connect():
                logger.warning("Connection lost! Attempting to reconnect...")
                connect_to_iq_option()
                time.sleep(5)
                continue

            logger.info("--------------------------------------------------")
            
            # مسح السوق بحثاً عن أصول متاحة للتداول
            tradable_assets = get_tradable_assets(base_asset_list, notified_assets)
            
            if not tradable_assets:
                time.sleep(60) # إذا لم تكن هناك أصول، انتظر دقيقة كاملة
                continue

            # المرور على الأصول المتاحة وتحليلها
            for asset_info in tradable_assets:
                asset_name = asset_info['name']
                asset_payout = asset_info['payout']

                logger.info(f"🔍 Analyzing: {asset_name} (Payout: {asset_payout:.0f}%)")
                data_df = get_and_prepare_data(asset_name)

                if data_df is not None:
                    signal = check_signal_ma_cross(data_df)
                    if signal != "NONE":
                        allowed = may_continue()
                        logger.info(f"Signal on {asset_name}: {signal}, AUTO_TRADE={AUTO_TRADE}, ALLOWED={allowed}")
                        # --- إرسال التنبيه الصوتي والمرئي ---
                        print('\a') # إصدار صوت "بيب"
                        logger.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        logger.warning(f"🚨🚨🚨 STRONG {signal} SIGNAL ON {asset_name} (Payout: {asset_payout:.0f}%) 🚨🚨🚨")

                        manual_required = True
                        if AUTO_TRADE and allowed:
                            direction = signal.lower()
                            if place_trade(Iq, asset_name, direction):
                                manual_required = False

                        if manual_required:
                            logger.warning("🚨🚨🚨 STRONG ... تحقّق يدويًا")
                            logger.warning(">>>>> PLEASE CHECK THE CHART AND PLACE TRADE MANUALLY! <<<<<")

                        logger.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        
                        # إضافة الأصل إلى قائمة التهدئة لمنع تكرار التنبيهات
                        notified_assets[asset_name] = time.time() + NOTIFICATION_COOLDOWN
                
                time.sleep(2) # تأخير بسيط بين فحص كل أصل

            logger.info("⏳ Scan complete. No new signals found. Re-scanning in 30 seconds.")
            time.sleep(30)

        except Exception as e:
            logger.error(f"A critical error occurred: {e}. Attempting to reconnect...")
            time.sleep(60)
            connect_to_iq_option()

if __name__ == "__main__":
    run_signal_generator()
