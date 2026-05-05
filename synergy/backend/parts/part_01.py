 in f if l.strip()]
        CONTACT_LABELS.clear()
        nums, skipped = _parse_contact_lines(raw_lines, existing_numbers=None)
        state.update({"numbers": nums, "total": len(nums), "completed": 0, "failed": 0})
        msg = f"Loaded {len(nums)} contact(s) from file"
        if skipped:
            msg += f" ({skipped} skipped \u2014 no valid number or duplicate)"
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


def clear_cache():
    cleared = 0
    if os.path.isdir(PROFILES_D):
        for profile in os.listdir(PROFILES_D):
            for cache_dir in ["Cache", "Code Cache", "GPUCache", "Service Worker"]:
                path = os.path.join(PROFILES_D, profile, cache_dir)
                if os.path.isdir(path):
                    try:
                        shutil.rmtree(path)
                        cleared += 1
                    except Exception as e:
                        log_msg(f"Cache clear error: {e}", "warning")
    return cleared


def _find_chromium():
    candidates = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    try:
        result = subprocess.run(["which", "chromium-browser"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


_VM_MAXBURST_MS  = 400
_SCREEN_SPEECH_MS = 300
_HUMAN_SPEECH_MS  = 80
_PICKUP_TIMEOUT_S = 55
_CLASSIFY_WINDOW_S = 8
_SEL_ENDED = [
    "[jsname='qWD2Ee']",
    ".Jyj8Xc",
    "[data-call-ended]",
    ".U5SBDe",
]
_VM_PHRASES = [
    "leave a message", "after the tone", "after the beep",
    "not available", "voicemail", "record your message",
    "hang up or press", "press 1 to accept", "mailbox is full",
]


def make_options(profile_name, headless=False):
    opts = Options()
    profile_path = os.path.join(PROFILES_D, profile_name)
    os.makedirs(profile_path, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_path}")
    opts.add_argument("--use-fake-ui-for-media-stream")
    opts.add_argument("--use-fake-device-for-media-stream")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.media_stream_mic": 1,
        "profile.default_content_setting_values.media_stream_camera": 1,
        "profile.default_content_setting_values.geolocation": 1,
        "profile.default_content_setting_values.notifications": 1,
    })
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,800")
    chromium_path = _find_chromium()
    if chromium_path:
        opts.binary_location = chromium_path
    return opts
