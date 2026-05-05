# DEFAULT_BASE_BUILD: fixed46_press1_tg_filter_vm_fastfail_contacts_parser
#!/usr/bin/env python3
"""Synergy 1.0 — FFT-based DTMF detection via Web Audio CDP injection"""

import os, json, time, re, threading, logging, subprocess, asyncio, tempfile, shutil
from datetime import datetime
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.common.exceptions import WebDriverException

BASE_DIR = os.path.join(os.path.expanduser("~"), "synergy")
SETTINGS_F = os.path.join(BASE_DIR, "settings.json")
NUMBERS_F = os.path.join(BASE_DIR, "numbers.txt")
PROFILES_D = os.path.join(BASE_DIR, "profiles")
AUDIO_D = os.path.join(BASE_DIR, "audio")
for _d in [BASE_DIR, PROFILES_D, AUDIO_D]:
    os.makedirs(_d, exist_ok=True)

SETTINGS_VERSION = 5

DEFAULT_SETTINGS = {
    "settings_version": SETTINGS_VERSION,
    "accounts": [{"email": "", "password": "", "profile": "profile_1"}],
    "telegram_bot_token": "",
    "telegram_user_id": "",
    "delay_between_calls": 45,
    "concurrent_limit": 1,
    "headless": False,
    "rotate_accounts": True,
    "vm_detection_enabled": False,
    "vm_hangup": True,
    "vm_action": "hangup",
    "screen_hangup_enabled": False,
    "screen_calls_enabled": False,
    "screen_hangup_action": "hangup",
    "dtmf_enabled": False,
    "dtmf_timeout": 20,
    "dtmf_key_to_detect": "1",
    "audio_initial": "",
    "audio_screen_bypass": "",
    "audio_press1": "",
    "verbose_debug": False,
    "dtmf_level2_enabled": False,
    "dtmf_level2_key": "1",
    "audio_level2": "",
}


def _deep_merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_settings():
    if os.path.exists(SETTINGS_F):
        try:
            with open(SETTINGS_F) as f:
                saved = json.load(f)
        except Exception:
            saved = {}
        saved_ver = saved.get("settings_version", 0)
        if saved_ver != SETTINGS_VERSION:
            fresh = dict(DEFAULT_SETTINGS)
            if "accounts" in saved and saved["accounts"]:
                fresh["accounts"] = saved["accounts"]
            save_settings_to_disk(fresh)
            return fresh
        return _deep_merge(DEFAULT_SETTINGS, saved)
    save_settings_to_disk(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)


def save_settings_to_disk(s):
    s["settings_version"] = SETTINGS_VERSION
    with open(SETTINGS_F, "w") as f:
        json.dump(s, f, indent=2)


settings = load_settings()

# ---------- state ----------
state = {
    "running": False,
    "paused": False,
    "login_status": {},
    "numbers": [],
    "completed": 0,
    "failed": 0,
    "total": 0,
    "current_number": "",
    "current_account": "",
    "log": [],
    "_stop": False,
}

_login_status_lock = threading.Lock()

# Global map: dialable-number -> full original contact line
CONTACT_LABELS = {}


# ---- contact parser helpers ------------------------------------------------

def _extract_phone(line: str):
    """Extract the first valid 10-digit US phone number from any line format.
    Strips +1 prefix, handles parens/dashes/dots/spaces/semicolons, and skips
    ISO date/time sequences so 2021-02-16 07:00:45 is never misread.
    """
    if not line:
        return None
    # Remove ISO datetime / date patterns before extracting digits
    cleaned = re.sub(
        r'\b\d{4}[-/]\d{2}[-/]\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?\b',
        '',
        line,
    )
    # Strip +1 country code in various spacings
    cleaned = re.sub(r'\+\s*1[\s\-.(]?', '', cleaned)
    # Collect all digit runs and find the first 10-digit sequence
    digits = ''.join(re.findall(r'\d+', cleaned))
    m = re.search(r'\d{10}', digits)
    return m.group(0) if m else None


def _parse_contact_lines(lines, existing_numbers=None):
    """Parse raw contact lines into a deduped list of dialable numbers.

    Extracts the first valid 10-digit US number from each line.
    Dedupes by number across existing_numbers and within this batch.
    Populates CONTACT_LABELS[num] = full original line (first occurrence wins).
    Returns (numbers_list, skipped_count).
    """
    existing = set(existing_numbers or [])
    seen = set(existing)
    numbers = []
    skipped = 0
    for line in lines:
        line = (line or '').strip()
        if not line:
            continue
        num = _extract_phone(line)
        if not num:
            skipped += 1
            continue
        if num in seen:
            skipped += 1
            continue
        seen.add(num)
        if num not in CONTACT_LABELS:
            CONTACT_LABELS[num] = line
        numbers.append(num)
    return numbers, skipped


def load_numbers_from_file():
    if os.path.exists(NUMBERS_F):
        with open(NUMBERS_F) as f:
            raw_lines = [l.strip() for l in f if l.strip()]
        CONTACT_LABELS.clear()
        nums, skipped = _parse_contact_lines(raw_lines, existing_numbers=None)
        state.update({"numbers": nums, "total": len(nums), "completed": 0, "failed": 0})
        msg = f"Loaded {len(nums)} contact(s) from file"
        if skipped:
            msg += f" ({skipped} skipped — no valid number or duplicate)"
        log_msg(msg)


def _clear_numbers_file():
    """Wipe numbers.txt on startup so old queues never auto-reload."""
    try:
        if os.path.exists(NUMBERS_F):
            open(NUMBERS_F, "w").close()
    except Exception:
        pass


_clear_numbers_file()


def log_msg(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    state["log"].append(entry)
    if len(state["log"]) > 500:
        state["log"] = state["log"][-500:]
    if level == "error":
        logging.error(msg)
    elif level == "warning":
        logging.warning(msg)
    else:
        logging.info(msg)


def debug_msg(msg):
    if settings.get("verbose_debug"):
        log_msg(f"[DBG] {msg}")


# ---------- Telegram ----------
from concurrent.futures import ThreadPoolExecutor
_tg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")


def _tg_send(msg):
    token = settings.get('telegram_bot_token', '')
    chat_id = settings.get('telegram_user_id', '')
    if not token or not chat_id:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': msg},
            timeout=10,
        )
    except Exception as e:
        logging.warning(f'Telegram send failed: {e}')


def tg_notify(msg):
    _tg_executor.submit(_tg_send, msg)


def tg_notify_dtmf(number, key, account_email):
    """Press-1 Telegram notification including the full contact line when known."""
    contact_line = CONTACT_LABELS.get(number, number)
    msg = (
        f"\U0001f7e2 PRESS {key} RECEIVED\n"
        f"Contact: {contact_line}\n"
        f"Number: {number}\n"
        f"Account: {account_email}"
    )
    _tg_executor.submit(_tg_send, msg)
