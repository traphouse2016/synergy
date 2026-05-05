m-usage",
    ]
    for a in args:
        opts.add_argument(a)
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,800")
    chromium_path = _find_chromium()
    if chromium_path:
        opts.binary_location = chromium_path
    return opts


def _is_driver_alive(driver):
    try: _ = driver.window_handles; return True
    except Exception: return False


def profile_name_for_account(account):
    import hashlib
    key = account.get("email", "") + account.get("profile", "")
    return "profile_" + hashlib.md5(key.encode()).hexdigest()[:8]


def safe_get(driver, url, retries=2):
    """FIX: retry driver.get() on WebDriverException."""
    for attempt in range(retries + 1):
        try:
            driver.get(url)
            return True
        except WebDriverException as e:
            if attempt < retries:
                time.sleep(1)
            else:
                log_msg(f"safe_get failed after {retries+1} attempts: {e}", "warning")
                return False


def ensure_voice_ready(driver, account_index=0):
    """Make sure GV is loaded and the page is not an error page."""
    try:
        cur = driver.current_url
    except Exception:
        return False
    if "voice.google.com" not in cur:
        try:
            safe_get(driver, "https://voice.google.com")
            time.sleep(3)
        except Exception:
            return False
    # Check for error page
    try:
        title = driver.title.lower()
        if "error" in title or "not found" in title:
            safe_get(driver, "https://voice.google.com")
            time.sleep(3)
    except Exception:
        pass
    return True


def get_or_create_driver(account):
    key = profile_name_for_account(account)
    with _drivers_lock:
        d = _drivers.get(key)
        if d and _is_driver_alive(d):
            return d
    headless = settings.get("headless", False)
    d = get_driver(key, headless=headless)
    ensure_voice_ready(d)
    with _drivers_lock:
        _drivers[key] = d
    return d


def release_drivers():
    with _drivers_lock:
        for d in list(_drivers.values()):
            try: d.quit()
            except Exception: pass
        _drivers.clear()


def _dom_has(driver, selectors):
    for sel in selectors:
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return sel
        except Exception:
            pass
    return None


def _get_call_timer(driver):
    try:
        text = driver.execute_script("return window._getCallTimer ? window._getCallTimer() : null;")
        return text
    except Exception:
        return None


def _reset_classify(driver):
    try: driver.execute_script("""
        window._totalSpeechMs = 0; window._silenceMs = 0;
        window._maxBurstMs = 0; window._phraseCount = 0;
        window._callStartTime = null; window._lastSpeech = false;
    """)
    except Exception: pass


def _get_call_state(driver):
    try: return driver.execute_script("return window._getCallState ? window._getCallState() : 'idle';")
    except Exception: return "idle"


def _dom_classify(driver):
    """FIX #62: use Shadow-DOM-aware JS to check for VM/end indicators."""
    try:
        dom = driver.execute_script("""
            function deepQuery(root, sel) {
              if (!root) return null;
              let el = root.querySelector(sel);
              if (el) return el;
              for (const node of root.querySelectorAll('*')) {
                if (node.shadowRoot) {
                  const found = deepQuery(node.shadowRoot, sel);
                  if (found) return found;
                }
              }
              return null;
            }
            const endSels = ["[jsname='qWD2Ee']",".Jyj8Xc","[data-call-ended]",".U5SBDe"];
            for (const s of endSels) { if (deepQuery(document, s)) return 'no_answer'; }
            const bodyText = document.body ? document.body.innerText.toLowerCase() : '';
            const vmPhrases = [
              'leave a message','after the tone','after the beep','not available',
              'voicemail','record your message','hang up or press','press 1 to accept',
              'mailbox is full'
            ];
            for (const p of vmPhrases) { if (bodyText.includes(p)) return 'voicemail'; }
            return null;
        """)
        return dom
    except Exception:
        return None


def _classify_audio(cs, elapsed_ms):
    max_burst  = cs.get("maxBurstMs", 0)
    total_speech = cs.get("totalSpeechMs", 0)
    phrase_count = cs.get("phraseCount", 0)
    silence    = cs.get("silenceMs", 0)
    if max_burst >= _VM_MAXBURST_MS:
        return "voicemail"
    if total_speech >= _SCREEN_SPEECH_MS and phrase_count >= 2:
        return "screening"
    if total_speech >= _HUMAN_SPEECH_MS:
        return "human"
    return None
