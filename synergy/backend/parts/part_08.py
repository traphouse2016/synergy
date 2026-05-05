mf_key_to_detect, not any key.
    Returns the key string or None on timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if state.get("_stop") or state.get("paused"):
            break
        try:
            confirmed = driver.execute_script("return window._dtmfConfirmed === true;")
            if confirmed:
                key = driver.execute_script("return window._dtmfDetected;")
                return key
        except Exception:
            break
        time.sleep(0.3)
    return None


# -----------------------------------------------------------------------
# Campaign runner
# -----------------------------------------------------------------------

def run_campaign():
    global settings
    settings = load_settings()
    numbers  = state.get("numbers", [])
    if not numbers:
        log_msg("No numbers to call", "warning")
        state["running"] = False
        return

    accounts = [a for a in settings.get("accounts", []) if a.get("email")]
    if not accounts:
        log_msg("No accounts configured", "error")
        state["running"] = False
        return

    concurrent = min(
        max(1, int(settings.get("concurrent_limit", 1))),
        10,
        len(accounts),
    )
    log_msg(f"Campaign start: {len(numbers)} numbers, {concurrent} concurrent worker(s)")
    tg_notify(f"\U0001f680 Campaign started\nNumbers: {len(numbers)}\nWorkers: {concurrent}")

    # Distribute numbers across workers (round-robin)
    chunks = [[] for _ in range(concurrent)]
    for i, num in enumerate(numbers):
        chunks[i % concurrent].append(num)

    threads = []
    for i in range(concurrent):
        account = accounts[i % len(accounts)]
        t = threading.Thread(
            target=_account_worker,
            args=(account, chunks[i]),
            daemon=True,
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    state.update({"running": False, "paused": False, "_stop": False})
    done = state["completed"]
    failed = state["failed"]
    log_msg(f"Campaign complete: {done} done, {failed} failed")
    tg_notify(f"\U0001f3c1 Campaign complete\n\u2713 {done} done\n\u2717 {failed} failed")


# -----------------------------------------------------------------------
# Ban detection / recovery
# -----------------------------------------------------------------------

BAN_PHRASES = [
    "your account has been suspended",
    "account suspended",
    "unusual activity",
    "verify it's you",
    "couldn't sign you in",
    "action required",
]


def detect_ban(driver):
    try:
        body = driver.execute_script("return document.body ? document.body.innerText.toLowerCase() : '';")
        for phrase in BAN_PHRASES:
            if phrase in body:
                return phrase
    except Exception:
        pass
    return None


def try_recover(driver, account):
    """Attempt a simple page refresh / re-navigate to recover a banned/stuck session."""
    try:
        safe_get(driver, "https://voice.google.com")
        time.sleep(5)
        ban = detect_ban(driver)
        if ban:
            log_msg(f"Recovery failed — ban phrase still present: {ban}", "warning")
            return False
        log_msg("Recovery successful")
        return True
    except Exception as e:
        log_msg(f"Recovery error: {e}", "error")
        return False


# -----------------------------------------------------------------------
# Flask API
# -----------------------------------------------------------------------

flask_app = Flask(__name__)
CORS(flask_app)


@flask_app.route("/api/state")
def api_state():
    return jsonify({
        "running":         state["running"],
        "paused":          state["paused"],
        "completed":       state["completed"],
        "failed":          state["failed"],
        "total":           state["total"],
        "current_number":  state["current_number"],
        "current_account": state["current_account"],
        "log":             state["log"][-100:],
        "numbers_loaded":  len(state["numbers"]),
        "login_status":    state.get("login_status", {}),
    })


@flask_app.route("/api/start", methods=["POST"])
def api_start():
    if state["running"]:
        return jsonify({"ok": False, "msg": "Already running"})
    if not state["numbers"]:
        return jsonify({"ok": False, "msg": "No numbers loaded"})
    state.update({"running": True, "paused": False, "_stop": False,
                  "completed": 0, "failed": 0})
    threading.Thread(target=run_campaign, daemon=True).start()
    return jsonify({"ok": True})


@flask_app.route("/api/stop", methods=["POST"])
def api_stop():
    state["_stop"] = True
    state["running"] = False
    state["paused"] = False
    return jsonify({"ok": True})


@flask_app.route("/api/pause", methods=["POST"])
def api_pause():
    state["paused"] = not state.get("paused", False)
    return jsonify({"ok": True, "paused": state["paused"]})
