n: pass
    return None

# FIX #70: removed get_call_timer() global — inlined into _wait_for_pickup_and_classify


def make_call(driver, number, audio_file=None, account_email="",
              vm_action="hangup", screen_hangup=False, screen_hangup_action="hangup",
              screen_calls_enabled=False, screen_bypass_audio=None,
              dtmf_enabled=False, dtmf_timeout=20, dtmf_key="1",
              audio_press1=None,
              dtmf_level2_enabled=False, dtmf_level2_key="1", audio_level2=None):
    """Dial number, play audio, optionally detect DTMF."""
    contact_line = CONTACT_LABELS.get(number, number)

    if not ensure_voice_ready(driver):
        log_msg(f"Driver not ready for {number}", "warning")
        return {"status": "failed", "reason": "driver_not_ready"}

    # Navigate to GV if needed
    try:
        cur = driver.current_url
        if "voice.google.com" not in cur:
            safe_get(driver, "https://voice.google.com")
            time.sleep(3)
    except Exception:
        pass

    # Find the dial input
    dial_input = None
    dial_selectors = [
        "input[aria-label='Search contacts or dial']",
        "input[aria-label='Enter a name or number']",
        "input[placeholder='Search contacts or dial']",
        "input[placeholder='Enter a name or number']",
    ]
    for sel in dial_selectors:
        try:
            dial_input = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            break
        except Exception:
            continue

    if not dial_input:
        log_msg(f"Dial input not found for {number}", "warning")
        return {"status": "failed", "reason": "no_dial_input"}

    # Type and dial
    try:
        dial_input.clear()
        dial_input.send_keys(number)
        time.sleep(0.5)
        dial_input.send_keys(Keys.RETURN)
        time.sleep(1)
    except Exception as e:
        log_msg(f"Dial send_keys error: {e}", "warning")
        return {"status": "failed", "reason": "dial_error"}

    # Wait for pickup and classify
    call_info = _wait_for_pickup_and_classify(driver)
    call_type  = call_info.get("type", "human")

    debug_msg(f"make_call classify result: {call_type} | {call_info}")

    if call_type in ("voicemail", "no_answer"):
        vm_action_to_use = vm_action
        if call_type == "no_answer":
            vm_action_to_use = "hangup"
        if vm_action_to_use == "hangup":
            _hangup(driver)
            return {"status": "no_answer" if call_type == "no_answer" else "voicemail",
                    "reason": call_type, "contact": contact_line}
        # vm_action == "leave_message" — play the initial audio then hang up
        if audio_file:
            play_audio_in_tab(
                driver, audio_file, number, account_email,
                dtmf_timeout=0, dtmf_key=dtmf_key,
            )
        time.sleep(2)
        _hangup(driver)
        return {"status": "voicemail", "contact": contact_line}

    if call_type == "screening":
        if not screen_calls_enabled:
            _hangup(driver)
            return {"status": "screened_hangup", "contact": contact_line}
        # Play bypass audio and re-classify
        if screen_bypass_audio:
            play_audio_in_tab(
                driver, screen_bypass_audio, number, account_email,
                dtmf_timeout=0, dtmf_key=dtmf_key,
            )
        post_screen_type = _classify_post_screen(driver)
        if post_screen_type in ("voicemail", "no_answer"):
            _hangup(driver)
            return {"status": "screened_vm", "contact": contact_line}
        # Human accepted — fall through to normal flow

    # Human path: play initial audio
    dtmf_result = None
    if audio_file:
        dtmf_result = play_audio_in_tab(
            driver, audio_file, number, account_email,
            dtmf_timeout=dtmf_timeout,
            dtmf_key=dtmf_key,
        )

    if dtmf_result and dtmf_result == dtmf_key:
        # Press-1 confirmed — play press1 audio if configured
        if audio_press1:
            play_audio_in_tab(
                driver, audio_press1, number, account_email,
                dtmf_timeout=dtmf_timeout if dtmf_level2_enabled else 0,
                dtmf_key=dtmf_level2_key if dtmf_level2_enabled else dtmf_key,
            )
        _hangup(driver)
        return {"status": "press1", "contact": contact_line}

    _hangup(driver)
    return {"status": "completed", "contact": contact_line}
