 or number'],
      "input[aria-label='Enter a name or number']",
      "input[aria-label='Search contacts or dial']",
    ]
    for sel in dial_selectors:
        try:
            el = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            return el
        except Exception:
            continue
    return None


def play_audio_in_tab(driver, audio_filename, number, account_email,
                      dtmf_timeout=20, dtmf_key="1"):
    """Play an audio file in the active tab and monitor for DTMF."""
    if not audio_filename:
        return None
    audio_url = f"http://127.0.0.1:5050/api/audio/file/{audio_filename}"
    try:
        driver.execute_script(f"window._gvPlayAudio('{audio_url}');")
    except Exception as e:
        log_msg(f"play_audio_in_tab script error: {e}", "warning")
        return None

    settings_snap = load_settings()
    if not settings_snap.get("dtmf_enabled", False):
        # Just wait for audio to finish
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                done = driver.execute_script("return window._gvAudioDone === true;")
                if done:
                    break
            except Exception:
                break
            time.sleep(0.5)
        return None

    # DTMF listening
    try:
        driver.execute_script(f"window._startDtmfListen('{dtmf_key}');")
    except Exception as e:
        log_msg(f"DTMF start error: {e}", "warning")

    deadline = time.time() + dtmf_timeout
    while time.time() < deadline:
        try:
            confirmed = driver.execute_script("return window._dtmfConfirmed === true;")
            if confirmed:
                key = driver.execute_script("return window._dtmfDetected;")
                log_msg(f"DTMF key detected: {key} for {number}")
                tg_notify_dtmf(number, key, account_email)
                return key
        except Exception:
            break
        time.sleep(0.3)
    return None


def _stop_dtmf_listen(driver):
    try:
        driver.execute_script("if(window._stopDtmfListen) window._stopDtmfListen();")
    except Exception:
        pass


def _poll_for_dtmf(driver, timeout_s, number, account_email, dtmf_key):
    """Poll for DTMF confirmation. Returns key string or None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            confirmed = driver.execute_script("return window._dtmfConfirmed === true;")
            if confirmed:
                key = driver.execute_script("return window._dtmfDetected;")
                tg_notify_dtmf(number, key, account_email)
                return key
        except Exception:
            break
        time.sleep(0.3)
    return None
