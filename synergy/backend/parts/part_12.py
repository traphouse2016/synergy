pture_output=True, text=True
                )
             # (kill_browsers response already sent above)


# ---- Telegram Bot ----------------------------------------------------------

def run_telegram_bot():
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters as tg_filters
    import asyncio

    bot_token = settings.get("telegram_bot_token", "")
    if not bot_token:
        log_msg("[tg] No bot token \u2014 Telegram bot not started", "warning")
        return

    _tg_pending_paste: dict = {}

    async def _process_contact_paste(raw_lines, update):
        existing_nums = set(state["numbers"])
        nums, skipped = _parse_contact_lines(raw_lines, existing_numbers=existing_nums)
        state["numbers"].extend(nums)
        state["total"] = len(state["numbers"])
        with open(NUMBERS_F, "w") as fh:
            fh.write("\n".join(state["numbers"]))
        msg = f"\u2705 Added {len(nums)} contact(s). Total: {state['total']}"
        if skipped:
            msg += f"\n\u26a0\ufe0f {skipped} line(s) skipped (no valid number)"
        await update.message.reply_text(msg)

    async def start_cmd(u, c):
        await u.message.reply_text(
            "\U0001f916 Synergy online!\n\n"
            "/call \u2014 start campaign\n/stop \u2014 stop\n/pause \u2014 toggle pause\n"
            "/status \u2014 stats\n/addnumbers \u2014 add contacts\n"
            "/clearnumbers \u2014 clear queue\n/lognumbers \u2014 preview queue"
        )

    async def call_cmd(u, c):
        if state["running"]:
            await u.message.reply_text("Already running!"); return
        state.update({"running": True, "_stop": False, "paused": False, "completed": 0, "failed": 0})
        threading.Thread(target=campaign_worker, daemon=True).start()
        await u.message.reply_text(f"Campaign started \u2014 {state['total']} numbers.")

    async def stop_cmd(u, c):
        state["_stop"] = True; state["paused"] = False
        await u.message.reply_text("\U0001f6d1 Stop signal sent.")

    async def pause_cmd(u, c):
        if not state["running"]:
            await u.message.reply_text("Not running."); return
        state["paused"] = not state.get("paused", False)
        await u.message.reply_text("\u23f8 Paused." if state["paused"] else "\u25b6\ufe0f Resumed.")

    async def status_cmd(u, c):
        pct = (state["completed"] / state["total"] * 100) if state["total"] else 0
        status = "Running" if state["running"] else "Stopped"
        if state.get("paused"): status += " (paused)"
        await u.message.reply_text(
            f"{status}\nTotal: {state['total']} Done: {state['completed']} "
            f"Failed: {state['failed']}\n{pct:.1f}%"
        )

    async def addnumbers_cmd(u, c):
        chat_id = u.effective_chat.id
        if not c.args:
            _tg_pending_paste[chat_id] = True
            await u.message.reply_text(
                "\U0001f4cb Paste your contacts / txt now.\n"
                "Accepts any format \u2014 one per line:\n"
                "  +18315219699,Max Newton,android,...\n"
                "  6316712632 ; email@x.com , [tag1|tag2]\n"
                "  John Smith, (555) 123-4567, CEO\n"
                "  5551234567"
            )
            return
        raw_text = " ".join(c.args)
        raw_lines = [n.strip() for n in raw_text.splitlines() if n.strip()]
        if not raw_lines:
            raw_lines = [n.strip() for n in raw_text.split(",") if n.strip()]
        await _process_contact_paste(raw_lines, u)

    async def on_plain_message(u, c):
        chat_id = u.effective_chat.id
        if not _tg_pending_paste.pop(chat_id, False):
            return
        text = u.message.text or ""
        raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
        await _process_contact_paste(raw_lines, u)

    async def clearnumbers_cmd(u, c):
        state.update({"numbers": [], "total": 0, "completed": 0, "failed": 0})
        _clear_numbers_file()
        await u.message.reply_text("\U0001f5d1 Numbers queue cleared.")

    async def lognumbers_cmd(u, c):
        entries = state.get("numbers", [])
        if not entries:
            await u.message.reply_text("Queue is empty."); return
        lines = []
        for e in entries[:30]:
            label = CONTACT_LABELS.get(e, e)
            lines.append(label)
        preview = "\n".join(lines)
        if len(entries) > 30:
            preview += f"\n...and {len(entries)-30} more"
        await u.message.reply_text(f"\U0001f4cb Queue ({len(entries)} contact(s)):\n{preview}")

    async def main_tg():
        app = (
            ApplicationBuilder()
            .token(bot_token)
            .connect_timeout(10)
            .read_timeout(15)
