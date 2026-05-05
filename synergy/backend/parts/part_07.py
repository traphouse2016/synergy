  f"energy={energy:.5f} speech={total_speech} burst={max_burst} phrases={phrase_count}"
    )
    if max_burst >= _VM_MAXBURST_MS:
        return "voicemail"
    if total_speech >= _SCREEN_SPEECH_MS and phrase_count >= 2:
        return "screening"
    if total_speech >= _HUMAN_SPEECH_MS:
        return "human"
    return "unknown"


def _hangup(driver):
    """Hang up the current call."""
    hangup_selectors = [
        "button[aria-label='End call']",
        "button[aria-label='Hang up']",
        ".yBDTNe",
        "button.VfPpkd-LgbsSe[jsname='BOHaEe']",
    ]
    for sel in hangup_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            btn.click()
            time.sleep(0.5)
            return True
        except Exception:
            continue
    try:
        driver.execute_script("""
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
                if (lbl.includes('end') || lbl.includes('hang')) { b.click(); break; }
            }
        """)
        time.sleep(0.5)
    except Exception:
        pass
    return False


def _account_worker(account, numbers_slice):
    """Worker thread: dials each number in numbers_slice for a single account."""
    email = account.get("email", "?")
    profile = profile_name_for_account(account)
    headless = settings.get("headless", False)
    delay = settings.get("delay_between_calls", 45)
    dtmf_enabled = settings.get("dtmf_enabled", False)
    dtmf_timeout = settings.get("dtmf_timeout", 20)
    dtmf_key = str(settings.get("dtmf_key_to_detect", "1"))
    audio_initial = settings.get("audio_initial", "")
    audio_screen_bypass = settings.get("audio_screen_bypass", "")
    audio_press1 = settings.get("audio_press1", "")
    vm_action = settings.get("vm_action", "hangup")
    screen_hangup_enabled = settings.get("screen_hangup_enabled", False)
    screen_calls_enabled = settings.get("screen_calls_enabled", False)
    screen_hangup_action = settings.get("screen_hangup_action", "hangup")
    vm_detection_enabled = settings.get("vm_detection_enabled", False)
    dtmf_level2_enabled = settings.get("dtmf_level2_enabled", False)
    dtmf_level2_key = str(settings.get("dtmf_level2_key", "1"))
    audio_level2 = settings.get("audio_level2", "")

    driver = None
    try:
        driver = get_driver(profile, headless=headless)
        ensure_voice_ready(driver)
        log_msg(f"[{email}] Worker started — {len(numbers_slice)} numbers")

        for idx, num in enumerate(numbers_slice, 1):
            if state.get("_stop"):
                break
            while state.get("paused") and not state.get("_stop"):
                time.sleep(1)
            if state.get("_stop"):
                break

            contact_line = CONTACT_LABELS.get(num, num)
            total_nums = state.get("total", len(numbers_slice))
            pct = round(idx / total_nums * 100, 1) if total_nums else 0
            log_msg(f"[{email}] \u2192 Dialing {num} [{contact_line}] ({idx}/{total_nums})")
            state["current_number"]  = num
            state["current_account"] = email

            try:
                result = make_call(
                    driver, num,
                    audio_file=audio_initial,
                    account_email=email,
                    vm_action=vm_action,
                    screen_hangup=screen_hangup_enabled,
                    screen_hangup_action=screen_hangup_action,
                    screen_calls_enabled=screen_calls_enabled,
                    screen_bypass_audio=audio_screen_bypass,
                    dtmf_enabled=dtmf_enabled,
                    dtmf_timeout=dtmf_timeout,
                    dtmf_key=dtmf_key,
                    audio_press1=audio_press1,
                    dtmf_level2_enabled=dtmf_level2_enabled,
                    dtmf_level2_key=dtmf_level2_key,
                    audio_level2=audio_level2,
                )
                status = result.get("status", "completed")
                state["completed"] += 1
                log_msg(
                    f"[{email}] \u2713 Done {num} [{contact_line}] \u2014 {idx}/{total_nums} "
                    f"({pct}%) status={status}"
                )
                if status == "press1":
                    tg_notify_dtmf(num, dtmf_key, email)
            except Exception as e:
                state["failed"] += 1
                log_msg(f"[{email}] \u2717 Error on {num}: {e}", "error")

            if not state.get("_stop") and idx < len(numbers_slice):
                time.sleep(delay)

    except Exception as e:
        log_msg(f"[{email}] Worker fatal: {e}", "error")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
        log_msg(f"[{email}] Worker finished")
