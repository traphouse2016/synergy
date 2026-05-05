counts})
        save_settings_to_disk(settings)
        log_msg("Settings saved")
        return jsonify({"ok": True})


@flask_app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    email = data.get("email", "")
    password = data.get("password", "")
    if not email:
        return jsonify({"error": "email required"}), 400
    account = {"email": email, "password": password}
    profile = profile_name_for_account(account)
    headless = settings.get("headless", False)

    def _do_login():
        with _login_status_lock:
            state["login_status"][email] = "logging_in"
        try:
            driver = get_driver(profile, headless=headless)
            with _drivers_lock:
                _drivers[email] = driver
            ok = gv_login(driver, account)
            with _login_status_lock:
                state["login_status"][email] = "logged_in" if ok else "failed"
        except Exception as e:
            log_msg(f"Login error for {email}: {e}", "error")
            with _login_status_lock:
                state["login_status"][email] = "failed"

    threading.Thread(target=_do_login, daemon=True).start()
    return jsonify({"message": f"Login started for {email}"})


@flask_app.route("/api/login/status")
def api_login_status():
    with _login_status_lock:
        return jsonify(dict(state.get("login_status", {})))


@flask_app.route("/api/recover", methods=["POST"])
def api_recover():
    data = request.json or {}
    email = data.get("email", "")
    password = data.get("password", "")
    if not email:
        return jsonify({"error": "email required"}), 400
    account = {"email": email, "password": password}
    profile = profile_name_for_account(account)
    headless = settings.get("headless", False)

    def _do_recover():
        with _login_status_lock:
            state["login_status"][email] = "logging_in"
        try:
            driver = get_driver(profile, headless=headless)
            with _drivers_lock:
                _drivers[email] = driver
            try:
                safe_get(driver, "https://voice.google.com/u/0/calls")
                time.sleep(2)
            except Exception:
                pass
            is_banned, reason = _detect_ban(driver)
            if is_banned:
                log_msg(f"[recover] \u26d4 BAN DETECTED for {email}: {reason}", "error")
                tg_notify(f"\u26d4 BAN DETECTED\nAccount: {email}\nReason: {reason}")
                with _login_status_lock:
                    state["login_status"][email] = "banned"
                return
            ok = gv_login(driver, account)
            if ok:
                is_banned, reason = _detect_ban(driver)
                if is_banned:
                    log_msg(f"[recover] \u26d4 POST-LOGIN BAN for {email}: {reason}", "error")
                    with _login_status_lock:
                        state["login_status"][email] = "banned"
                    return
                log_msg(f"[recover] \u2713 Account recovered: {email}", "success")
                tg_notify(f"\u2705 Account recovered: {email}")
                with _login_status_lock:
                    state["login_status"][email] = "logged_in"
            else:
                log_msg(f"[recover] Login failed for {email}", "error")
                with _login_status_lock:
                    state["login_status"][email] = "failed"
        except Exception as e:
            log_msg(f"[recover] Error for {email}: {e}", "error")
            with _login_status_lock:
                state["login_status"][email] = "failed"

    threading.Thread(target=_do_recover, daemon=True).start()
    return jsonify({"message": f"Recovery started for {email}"})
