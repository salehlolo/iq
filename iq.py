# -*- coding: utf-8 -*-
"""Utility patches for iqoptionapi stability."""

# --- Patch to tame iqoptionapi digital thread noise ---
import threading
import sys
import logging
from iqoptionapi.stable_api import IQ_Option as _IQO

# 1) Ensure get_digital_underlying_list_data returns a safe structure
if hasattr(_IQO, "get_digital_underlying_list_data"):
    _orig_get_dul = _IQO.get_digital_underlying_list_data

    def _safe_get_dul(self, *args, **kwargs):
        try:
            data = _orig_get_dul(self, *args, **kwargs)
            if not isinstance(data, dict) or "underlying" not in data:
                return {"underlying": []}
            return data
        except Exception:
            return {"underlying": []}

    _IQO.get_digital_underlying_list_data = _safe_get_dul


# 2) Thread excepthook wrapper to silence noisy KeyErrors
def _thread_excepthook(args: threading.ExceptHookArgs):
    # Suppress only KeyError originating from iqoptionapi digital thread
    if args.exc_type is KeyError:
        tb = "".join(__import__("traceback").format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        if "iqoptionapi" in tb and "stable_api.py" in tb and "underlying" in str(args.exc_value):
            logging.debug("Suppressed KeyError from iqoptionapi digital thread")
            return
    sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)


threading.excepthook = _thread_excepthook
# --- end patch ---
