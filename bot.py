# -*- coding: utf-8 -*-

import os
import time
import logging
import threading
import iq  # noqa: F401  # apply iqoptionapi stability patches
from iqoptionapi.stable_api import IQ_Option
import pandas as pd
import pandas_ta as ta
import sys
import datetime

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format='%(asctime)s - %(levelname)s - %(message)s')

# ==============================================================================
# --- 1. إعدادات السكريبت ---
# ==============================================================================

# -- بيانات الدخول (استخدم بياناتك الحقيقية هنا) --
EMAIL = "Salehmagdy124@gmail.com"
PASSWORD = "01227372440Saleh"
ACCOUNT_TYPE = "PRACTICE"

# -- إعدادات التداول --
MINIMUM_PAYOUT = int(os.getenv("MINIMUM_PAYOUT", "70"))


# -- إعدادات التنفيذ التلقائي --
AUTO_TRADE = os.getenv("AUTO_TRADE", "1").strip().lower() in {"1", "true", "on", "yes"}
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "1"))  # قيمة الصفقة
EXPIRY_MIN = int(os.getenv("EXPIRY_MIN", "1"))  # انتهاء الصفقة بالدقائق (١ دقيقة)
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "120"))  # فترة تبريد لكل أصل لمنع تكرار الدخول
BINARY_EXPIRY_MIN = int(os.getenv("BINARY_EXPIRY_MIN", "15"))  # مدة انتهاء للـ Binary
ENTRY_CUTOFF_S = int(os.getenv("ENTRY_CUTOFF_S", "5"))      # لا ندخل لو باقي <= هذه الثواني على إغلاق الدقيقة
_last_trade_ts = {}

ENABLE_DIGITAL = os.getenv("ENABLE_DIGITAL", "0").strip().lower() in {"1", "true", "yes", "on"}

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


def _asset_forms(asset):
    # جرّب الصيغ الأكثر شيوعًا
    forms = {asset, asset.upper(), asset.lower()}
    # صيغ إضافية محتملة (خاصة بالمنصة)
    if not asset.endswith("-OTC"):
        forms.add(f"{asset}-OTC")
        forms.add(f"{asset.lower()}-otc")
        forms.add(f"{asset.upper()}-OTC")
    return list(forms)


def _get_open_flag(ot, cat, asset):
    for key in _asset_forms(asset):
        try:
            if bool(ot.get(cat, {}).get(key, {}).get('open')):
                return True
        except Exception:
            pass
    return False


def get_open_state(iq, asset):
    """
    يرجع ثلاثي: (digital_open, turbo_open, binary_open)
    """
    digital_open = False
    turbo_open = False
    binary_open = False
    try:
        ot = iq.get_all_open_time() or {}
        if ENABLE_DIGITAL:
            digital_open = _get_open_flag(ot, "digital", asset)
        turbo_open = _get_open_flag(ot, "turbo", asset)
        binary_open = _get_open_flag(ot, "binary", asset)
    except Exception as e:
        logger.error(f"⚠️ تعذر جلب حالة الفتح لـ {asset}: {e}")
    return digital_open, turbo_open, binary_open


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


def _sec_to_next_minute():
    # تقريبية لكن كافية؛ إن أردت الدقة استخدم توقيت السيرفر إذا توفر
    now = time.time()
    return 60 - (int(now) % 60)


def _minutes_to_next_quarter(server_ts: float, cutoff_s: int) -> int:
    """
    يحسب الدقائق حتى أقرب فتحة ربع ساعة (00/15/30/45) وفق توقيت خادم IQ.
    يضيف دقيقة لو كنا داخل نافذة الأمان الأخيرة من الدقيقة الحالية.
    يضمن ألا نرجع مدة < 5 دقائق للـ Binary؛ وإن حدث، ننتقل للربع التالي.
    """
    dt = datetime.datetime.fromtimestamp(server_ts)
    # دقائق حتى ربع الساعة التالي
    qrem = (15 - (dt.minute % 15)) % 15
    if qrem == 0:
        qrem = 15
    # لو نحن داخل آخر ثواني من الدقيقة الحالية → زُح للربع التالي
    if dt.second >= 60 - max(cutoff_s, 1):
        qrem += 1
    # الـ Binary يحتاج عادة ≥ 5 دقائق (وأحيانًا يُقبل فقط ربع ساعة)
    if qrem < 5:
        qrem += 15
    return qrem


def place_trade(iq, asset, direction):
    """إرسال صفقة مع مراعاة التبريد واختيار الأداة الأنسب (Digital ثم Turbo)."""
    reset_daily_counters_if_needed()

    now = time.time()
    if now - _last_trade_ts.get(asset, 0) < COOLDOWN_S:
        logger.info(f"⏳ تبريد مفعّل لـ {asset}.. تخطي الدخول")
        return False

    digital_open, turbo_open, binary_open = get_open_state(iq, asset)

    ok = False
    order_id = None
    is_digital = False
    mode = None
    logger.info(f"OpenState[{asset}] => digital={digital_open}, turbo={turbo_open}, binary={binary_open}")

    # 0) نافذة أمان للـ Turbo دقيقة واحدة
    if turbo_open and EXPIRY_MIN == 1:
        rem = _sec_to_next_minute()
        if rem <= ENTRY_CUTOFF_S:
            logger.info(f"⏱️ قريب جدًا من إغلاق الشمعة ({rem}s)… تخطي الدخول على {asset}")
            return False

    # 1) Digital أولًا فقط إذا مفعّل ومفتوح
    if ENABLE_DIGITAL and digital_open:
        mode = "DIGITAL"
        logger.info(f"🛒 محاولة تنفيذ {mode} على {asset} | {direction.upper()} {TRADE_AMOUNT}$ لمدة {EXPIRY_MIN} دقيقة")
        is_digital = True
        ok, order_id = iq.buy_digital_spot(asset, TRADE_AMOUNT, direction, EXPIRY_MIN)
        if not ok:
            logger.info(f"↩️ فشل {mode} على {asset}، سنحاول TURBO/BINARY إن أمكن")
            is_digital = False
            mode = None

    # 2) Turbo إن كان مفتوحًا
    if not ok and turbo_open:
        mode = "TURBO"
        expiry = EXPIRY_MIN  # 1 عادة
        logger.info(f"🛒 محاولة تنفيذ {mode} على {asset} | {direction.upper()} {TRADE_AMOUNT}$ لمدة {expiry} دقيقة")
        ok, order_id = iq.buy(TRADE_AMOUNT, asset, direction, expiry)

    # 3) Binary إن كان مفتوحًا → احسب مدة محاذاة ربع الساعة بدقة
    if not ok and binary_open:
        mode = "BINARY"
        try:
            server_ts = Iq.get_server_timestamp() or time.time()
        except Exception:
            server_ts = time.time()
        expiry = _minutes_to_next_quarter(server_ts, ENTRY_CUTOFF_S)
        logger.info(f"🛒 محاولة تنفيذ {mode} على {asset} | {direction.upper()} {TRADE_AMOUNT}$ لمدة {expiry} دقيقة (محاذاة ربع الساعة)")
        ok, order_id = iq.buy(TRADE_AMOUNT, asset, direction, expiry)

        # Fallback: لو فشل، جرّب الربع التالي مباشرة
        if not ok:
            expiry += 15
            logger.info(f"↩️ إعادة المحاولة {mode} على {asset} بمدة {expiry} دقيقة (الربع التالي)")
            ok, order_id = iq.buy(TRADE_AMOUNT, asset, direction, expiry)

    if ok:
        logger.warning(f"🧾 أُرسلت صفقة {direction.upper()} على {asset} بقيمة {TRADE_AMOUNT} | id={order_id} | mode={mode or ('DIGITAL' if is_digital else 'TURBO')}")
        _last_trade_ts[asset] = time.time()
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
        if not (digital_open or turbo_open or binary_open):
            logger.error(f"❌ فشل إرسال الصفقة: {asset} مغلق الآن (لا Digital ولا Turbo ولا Binary).")
        else:
            logger.error(f"❌ فشل إرسال الصفقة على {asset} رغم كون الأداة مفتوحة. جرّب ضبط المدة أو تجنّب نافذة الإغلاق.")
        return False

# ==============================================================================
# --- 3. وظائف الاستراتيجية والتحليل ---
# ==============================================================================

def best_payout(all_profit, asset):
    """
    يدعم شكلين:
    A) {asset: {'turbo':0.84,'digital':0.84,'binary':0.84}}
    B) {'turbo':{asset:0.84}, 'digital':{asset:0.84}, 'binary':{asset:0.84}}
    يرجّع أعلى عائد كنسبة مئوية [0..100]
    """
    cands = []

    # شكل A: متمركز حول الأصل
    try:
        ap = (
            all_profit.get(asset)
            or all_profit.get(asset.upper())
            or all_profit.get(asset.lower())
            or {}
        )
        if isinstance(ap, dict):
            for key in ("digital", "turbo", "binary"):
                v = ap.get(key)
                if v:
                    try:
                        cands.append(float(v) * 100.0)
                    except Exception:
                        pass
    except Exception:
        pass

    # شكل B: متمركز حول النوع
    for key in ("digital", "turbo", "binary"):
        try:
            type_map = all_profit.get(key, {})
            if isinstance(type_map, dict):
                for a in _asset_forms(asset):
                    v = type_map.get(a)
                    if v:
                        try:
                            cands.append(float(v) * 100.0)
                            break
                        except Exception:
                            pass
        except Exception:
            pass

    return max(cands) if cands else 0.0


def get_tradable_assets(base_assets, notified_assets):
    logger.info("Scanning market for tradable assets...")
    try:
        now = time.time()
        tradable_assets = []

        try:
            all_profit = Iq.get_all_profit() or {}
        except Exception as e:
            logger.error(f"⚠️ تعذر جلب العوائد: {e}")
            all_profit = {}

        try:
            ot = Iq.get_all_open_time() or {}
        except Exception as e:
            logger.error(f"⚠️ تعذر جلب أوقات الفتح: {e}")
            ot = {}

        for asset in base_assets:
            if asset in notified_assets and now < notified_assets[asset]:
                continue

            digital_open = False
            if ENABLE_DIGITAL:
                try:
                    digital_open = _get_open_flag(ot, "digital", asset)
                except Exception:
                    digital_open = False
            turbo_open = _get_open_flag(ot, "turbo", asset) or _get_open_flag(ot, "binary", asset)

            if not (turbo_open or (ENABLE_DIGITAL and digital_open)):
                continue

            payout = best_payout(all_profit, asset)
            if payout >= MINIMUM_PAYOUT:
                tradable_assets.append({"name": asset, "payout": payout})

        if tradable_assets:
            logger.info(f"Found {len(tradable_assets)} tradable assets to monitor.")
        else:
            logger.warning("No tradable assets with sufficient payout found at the moment.")
        return tradable_assets, all_profit
    except Exception as e:
        logger.error(f"Could not scan market due to an error: {e}")
        return [], {}


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
    logger.info(f"ENABLE_DIGITAL={ENABLE_DIGITAL}")
    logger.info(f"MINIMUM_PAYOUT={MINIMUM_PAYOUT}")

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
            tradable_assets, all_profit = get_tradable_assets(base_asset_list, notified_assets)

            if not tradable_assets:
                time.sleep(60) # إذا لم تكن هناك أصول، انتظر دقيقة كاملة
                continue

            # المرور على الأصول المتاحة وتحليلها
            for asset_info in tradable_assets:
                asset_name = asset_info['name']
                asset_payout = best_payout(all_profit, asset_name)

                logger.info(f"🔍 Analyzing: {asset_name} (Payout: {int(asset_payout)}%)")
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
