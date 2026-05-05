ers.pop(key, None)
        if d:
            try: d.quit()
            except Exception: pass
    log_msg("All browsers closed")
    return jsonify({"ok": True})


@flask_app.route("/api/kill_browsers", methods=["POST"])
def api_kill_browsers():
    import platform
    system = platform.system()
    killed = 0
    for proc_name in ["chrome", "chromium", "chromedriver"]:
        try:
            if system == "Windows":
                subprocess.run(["taskkill", "/F", "/IM", f"{proc_name}.exe"], capture_output=True)
            else:
                subprocess.run(["pkill", "-f", proc_name], capture_output=True)
            killed += 1
        except Exception:
            pass
    with _drivers_lock:
        _drivers.clear()
    log_msg(f"Killed browser processes ({killed} targets)")
    return jsonify({"ok": True})


@flask_app.route("/api/test_call", methods=["POST"])
def api_test_call():
    data = request.json or {}
    number = data.get("number", "")
    if not number:
        return jsonify({"error": "number required"}), 400
    accounts = settings.get("accounts", [])
    if not accounts or not accounts[0].get("email"):
        return jsonify({"error": "no accounts configured"}), 400
    account = accounts[0]
    profile = profile_name_for_account(account)
    headless = settings.get("headless", False)
    audio_file = settings.get("audio_initial", "")
    audio_file_path = os.path.join(AUDIO_D, audio_file) if audio_file else ""
    dtmf_enabled = settings.get("dtmf_enabled", False)
    dtmf_timeout = settings.get("dtmf_timeout", 20)
    dtmf_key = str(settings.get("dtmf_key_to_detect", "1"))
    audio_press1 = settings.get("audio_press1", "")
    audio_press1_path = os.path.join(AUDIO_D, audio_press1) if audio_press1 else ""

    def _run():
        try:
            driver = get_driver(profile, headless=headless)
            ensure_voice_ready(driver)
            ok = make_call(driver, number, _account=account)
            if not ok:
                log_msg(f"Test call failed to connect: {number}", "warning")
                driver.quit()
                return
            if audio_file_path and os.path.exists(audio_file_path):
                key = play_audio_in_tab(
                    driver, audio_file_path,
                    block=True,
                    dtmf_interrupt=dtmf_enabled,
                    press1_filepath=audio_press1_path if audio_press1_path else None,
                    number=number,
                    account_email=account["email"],
                )
                if key:
                    tg_notify_dtmf(number, key, account["email"])
            hang_up(driver)
            log_msg(f"Test call complete: {number}")
            driver.quit()
        except Exception as e:
            log_msg(f"Test call error: {e}", "error")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": f"Test call to {number} started"})


@flask_app.route("/api/audio/play", methods=["POST"])
def api_audio_play():
    data = request.json or {}
    filename = data.get("filename", "")
    email = data.get("email", "")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    filepath = os.path.join(AUDIO_D, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": f"File not found: {filename}"}), 404
    driver = _drivers.get(email)
    if not driver:
        return jsonify({"error": f"No active driver for {email}"}), 404
    threading.Thread(
        target=play_audio_in_tab, args=(driver, filepath), daemon=True
    ).start()
    return jsonify({"ok": True})


@flask_app.route("/api/audio/status")
def api_audio_status():
    email = request.args.get("email", "")
    driver = _drivers.get(email)
    if not driver:
        return jsonify({"playing": False, "done": True})
    try:
        playing = driver.execute_script("return window._gvAudioPlaying === true;")
        done    = driver.execute_script("return window._gvAudioDone === true;")
        err     = driver.execute_script("return window._gvAudioError || null;")
        return jsonify({"playing": playing, "done": done, "error": err})
    except Exception as e:
        return jsonify({"playing": False, "done": True, "error": str(e)})


@flask_app.route("/api/audio/file/<filename>")
def api_audio_file(filename):
    from flask import send_from_directory
    safe = os.path.basename(filename)
    return send_from_directory(AUDIO_D, safe)
