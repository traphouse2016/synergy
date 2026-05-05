ogle.com" not in cur:
                log_msg(f"Login success for {account.get('email','?')}", "info")
                return True
            log_msg(f"Still not on voice.google.com after login attempt", "warning")
            return False
        except Exception as e:
            log_msg(f"ensure_voice_ready error: {e}", "warning")
            return False


def _wait_for_pickup_and_classify(driver):
    """Wait for call pickup and classify: voicemail / screening / human / no_answer.
    Uses ring duration + audio energy + DOM cues.
    Returns dict: {type, total_speech_ms, silence_ms, max_burst_ms, phrase_count, ring_ms}
    """
    ring_start = time.time()
    deadline   = ring_start + _PICKUP_TIMEOUT_S
    ring_ended_at = None

    # Wait for call timer to appear (pickup signal)
    while time.time() < deadline:
        if state.get("_stop") or state.get("paused"):
            return {"type": "no_answer"}
        timer = _get_call_timer(driver)
        if timer:
            ring_ended_at = time.time()
            break
        dom = _dom_classify(driver)
        if dom in ("no_answer", "voicemail"):
            return {"type": dom}
        time.sleep(0.35)

    if not ring_ended_at:
        return {"type": "no_answer"}

    ring_ms = (ring_ended_at - ring_start) * 1000

    # Quick early-VM: very short ring and then call timer — often VM pickup
    if ring_ms < 4000:
        debug_msg(f"Ring < 4s ({ring_ms:.0f}ms) — likely VM fast-pickup")

    # Reset audio stats and classify over the window
    _reset_classify(driver)
    classify_deadline = time.time() + _CLASSIFY_WINDOW_S

    while time.time() < classify_deadline:
        if state.get("_stop"):
            return {"type": "no_answer"}
        dom = _dom_classify(driver)
        if dom:
            return {"type": dom, "ring_ms": ring_ms}
        time.sleep(0.4)

    try:
        cs = driver.execute_script("return window._gvStats ? window._gvStats() : {};")
    except Exception:
        cs = {}

    audio_type = _classify_audio(cs, elapsed_ms=_CLASSIFY_WINDOW_S * 1000)

    max_burst    = cs.get("maxBurstMs", 0)
    total_speech = cs.get("totalSpeechMs", 0)
    phrase_count = cs.get("phraseCount", 0)

    debug_msg(
        f"classify: ring={ring_ms:.0f}ms type={audio_type} "
        f"speech={total_speech}ms burst={max_burst}ms phrases={phrase_count}"
    )

    return {
        "type":            audio_type or "human",
        "total_speech_ms": total_speech,
        "silence_ms":      cs.get("silenceMs", 0),
        "max_burst_ms":    max_burst,
        "phrase_count":    phrase_count,
        "ring_ms":         ring_ms,
    }


def _classify_post_screen(driver, timeout=25):
    """After playing bypass audio to a screener, wait for them to go silent
    then re-classify: human accepted / voicemail / no_answer.
    """
    deadline = time.time() + timeout
    # Wait for screener speech to stop
    prev_speech = None
    while time.time() < deadline:
        try:
            cs = driver.execute_script("return window._gvStats ? window._gvStats() : {};")
        except Exception:
            cs = {}
        total_speech = cs.get("totalSpeechMs", 0)
        if prev_speech is not None and total_speech == prev_speech:
            # No new speech for a tick — screener may have stopped
            break
        prev_speech = total_speech
        dom = _dom_classify(driver)
        if dom:
            return dom
        time.sleep(0.5)

    _reset_classify(driver)
    time.sleep(3)
    dom = _dom_classify(driver)
    if dom:
        return dom
    try:
        cs = driver.execute_script("return window._gvStats ? window._gvStats() : {};")
    except Exception:
        cs = {}
    return _classify_audio(cs, 3000) or "human"


# ---------- Classify (lightweight fast version for screening) -----------

def classify_call(driver, classify_seconds=8):
    """Lightweight classifier used when vm_detection or screen_hangup is on."""
    start = time.time()
    while time.time() - start < classify_seconds:
        if state.get("_stop"):
            return "unknown"
        time.sleep(0.5)
    try:
        cs = driver.execute_script("return window._gvStats ? window._gvStats() : {};")
    except Exception:
        return "unknown"
    max_burst    = cs.get("maxBurstMs", 0)
    total_speech = cs.get("totalSpeechMs", 0)
    phrase_count = cs.get("phraseCount", 0)
    energy       = cs.get("lastEnergy", 0)
    debug_msg(
        f"energy={energy:.5f} speech={total_speech} burst={max_burst} phrases={phrase_count}"
    )
    if max_burst >= _VM_MAXBURST_MS:
        return "voicemail"
    if total_speech >= _SCREEN_SPEECH_MS and phrase_count >= 2:
        return "screening"
    if total_speech >= _HUMAN_SPEECH_MS:
        return "human"
    return "unknown"
