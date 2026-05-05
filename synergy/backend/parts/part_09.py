l detected — leaving message: {num}", "warning")
            _hangup(driver)
            time.sleep(2)
            continue

        # Human or unknown — proceed
        if audio_initial:
            try:
                play_audio_in_tab(
                    driver, audio_initial, num, email,
                    dtmf_timeout=dtmf_timeout,
                    dtmf_key=dtmf_key,
                )
            except Exception as e:
                log_msg(f"[{email}] Audio error for {num}: {e}", "warning")

        state["completed"] += 1
        log_msg(f"[{email}] \u2713 Done {num} [{CONTACT_LABELS.get(num,num)}] \u2014 {idx}/{total} ({pct}%)")

        if idx < total and not state.get("_stop"):
            time.sleep(delay)


# -----------------------------------------------------------------------
# Numbers & contacts endpoints
# -----------------------------------------------------------------------

@flask_app.route("/api/numbers/load", methods=["POST"])
def api_load_numbers():
    data = request.json or {}
    raw_text = data.get("numbers", "")
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    existing = list(state["numbers"])
    nums, skipped = _parse_contact_lines(lines, existing_numbers=existing)

    state["numbers"].extend(nums)
    state["total"] = len(state["numbers"])

    msg = f"Loaded {len(nums)} contact(s)"
    if skipped:
        msg += f" ({skipped} skipped \u2014 no valid number or duplicate)"
    log_msg(msg)
    return jsonify({"ok": True, "count": len(state["numbers"]), "added": len(nums), "skipped": skipped})


@flask_app.route("/api/numbers/clear", methods=["POST"])
def api_clear_numbers():
    state["numbers"] = []
    state["total"]   = 0
    CONTACT_LABELS.clear()
    _clear_numbers_file()
    log_msg("Numbers cleared")
    return jsonify({"ok": True})


@flask_app.route("/api/numbers/list")
def api_list_numbers():
    nums = state["numbers"]
    preview = [
        {"number": n, "label": CONTACT_LABELS.get(n, n)}
        for n in nums[:200]
    ]
    return jsonify({"count": len(nums), "numbers": preview})


@flask_app.route("/api/settings", methods=["GET"])
def api_get_settings():
    safe = dict(settings)
    # Redact passwords
    safe_accounts = []
    for acc in safe.get("accounts", []):
        a = dict(acc)
        if a.get("password"):
            a["password"] = "***"
        safe_accounts.append(a)
    safe["accounts"] = safe_accounts
    return jsonify(safe)


@flask_app.route("/api/settings", methods=["POST"])
def api_save_settings():
    global settings
    new_s = request.json or {}
    # Restore redacted passwords
    old_accounts = {a.get("email", ""): a for a in settings.get("accounts", [])}
    for acc in new_s.get("accounts", []):
        if acc.get("password") == "***":
            email = acc.get("email", "")
            if email in old_accounts:
                acc["password"] = old_accounts[email].get("password", "")
    # Auto-generate stable profiles
    for acc in new_s.get("accounts", []):
        if not acc.get("profile"):
            acc["profile"] = profile_name_for_account(acc)
    # Clamp concurrent
    if "concurrent_limit" in new_s:
        cl = int(new_s["concurrent_limit"])
        if cl > 10:
            log_msg(f"concurrent_limit clamped from {cl} to 10", "warning")
            new_s["concurrent_limit"] = 10
    settings = _deep_merge(DEFAULT_SETTINGS, new_s)
    save_settings_to_disk(settings)
    log_msg("Settings saved")
    return jsonify({"ok": True})
