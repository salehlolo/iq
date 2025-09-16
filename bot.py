# -*- coding: utf-8 -*-

import time
import logging
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
Iq = IQ_Option(EMAIL, PASSWORD)

def connect_to_iq_option():
    """يقوم بالاتصال أو إعادة الاتصال بالمنصة بشكل آمن."""
    logging.info("Attempting to connect to IQ Option...")
    check, reason = Iq.connect()
    if check:
        logging.info("✅ Successfully connected!")
        Iq.change_balance(ACCOUNT_TYPE)
        logging.info(f"👍 Switched to {ACCOUNT_TYPE} account.")
        return True
    else:
        logging.error(f"❌ Connection failed, reason: {reason}")
        return False

# ==============================================================================
# --- 3. وظائف الاستراتيجية والتحليل ---
# ==============================================================================

def get_tradable_assets(base_assets, notified_assets):
    """
    يقوم بمسح السوق ويعيد قائمة بالأصول المفتوحة والتي لديها نسبة ربح جيدة
    والتي لم يتم إرسال تنبيه بشأنها مؤخراً.
    """
    logging.info("Scanning market for tradable assets...")
    try:
        all_profit = Iq.get_all_profit()
        tradable_assets = []
        now = time.time()
        
        for asset in base_assets:
            # تخطي الأصل إذا تم إرسال تنبيه له مؤخراً
            if asset in notified_assets and now < notified_assets[asset]:
                continue

            # التحقق من أن الأصل متاح ونسبة الربح مقبولة
            if asset in all_profit and 'turbo' in all_profit[asset] and all_profit[asset]['turbo'] * 100 >= MINIMUM_PAYOUT:
                tradable_assets.append({'name': asset, 'payout': all_profit[asset]['turbo'] * 100})
        
        if tradable_assets:
            logging.info(f"Found {len(tradable_assets)} tradable assets to monitor.")
        else:
            logging.warning("No tradable assets with sufficient payout found at the moment.")
        return tradable_assets
    except Exception as e:
        logging.error(f"Could not scan market due to an error: {e}")
        return []


def get_and_prepare_data(asset):
    """يجلب بيانات الشموع ويضيف إليها المتوسطات المتحركة."""
    candles = Iq.get_candles(asset, TIMEFRAME, CANDLE_COUNT, time.time())
    if not candles: 
        logging.warning(f"Could not fetch candle data for {asset}.")
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

    logging.info(f"==================================================================")
    logging.info(f"🤖 PROFESSIONAL SIGNAL GENERATOR - STARTED SUCCESSFULLY ?")
    logging.info(f"==================================================================")
    logging.info(f"Today is {datetime.datetime.now().strftime('%A')}, scanning for {market_type} assets.")
    logging.info(f"💡 Strategy Activated: Active MA Cross Strategy (1-min timeframe)")

    while True:
        try:
            # فحص الاتصال في بداية كل دورة لضمان الموثوقية
            if not Iq.check_connect():
                logging.warning("Connection lost! Attempting to reconnect...")
                connect_to_iq_option()
                time.sleep(5)
                continue

            logging.info("--------------------------------------------------")
            
            # مسح السوق بحثاً عن أصول متاحة للتداول
            tradable_assets = get_tradable_assets(base_asset_list, notified_assets)
            
            if not tradable_assets:
                time.sleep(60) # إذا لم تكن هناك أصول، انتظر دقيقة كاملة
                continue

            # المرور على الأصول المتاحة وتحليلها
            for asset_info in tradable_assets:
                asset_name = asset_info['name']
                asset_payout = asset_info['payout']

                logging.info(f"🔍 Analyzing: {asset_name} (Payout: {asset_payout:.0f}%)")
                data_df = get_and_prepare_data(asset_name)

                if data_df is not None:
                    signal = check_signal_ma_cross(data_df)
                    if signal != "NONE":
                        # --- إرسال التنبيه الصوتي والمرئي ---
                        print('\a') # إصدار صوت "بيب"
                        logging.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        logging.warning(f"🚨🚨🚨 STRONG {signal} SIGNAL ON {asset_name} (Payout: {asset_payout:.0f}%) 🚨🚨🚨")
                        logging.warning(">>>>> PLEASE CHECK THE CHART AND PLACE TRADE MANUALLY! <<<<<")
                        logging.warning("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        
                        # إضافة الأصل إلى قائمة التهدئة لمنع تكرار التنبيهات
                        notified_assets[asset_name] = time.time() + NOTIFICATION_COOLDOWN
                
                time.sleep(2) # تأخير بسيط بين فحص كل أصل

            logging.info("⏳ Scan complete. No new signals found. Re-scanning in 30 seconds.")
            time.sleep(30)

        except Exception as e:
            logging.error(f"A critical error occurred: {e}. Attempting to reconnect...")
            time.sleep(60)
            connect_to_iq_option()

if __name__ == "__main__":
    run_signal_generator()
