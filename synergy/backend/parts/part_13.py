rite_timeout(15)
               .pool_timeout(10).build()
        )
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("call", call_cmd))
        app.add_handler(CommandHandler("stop", stop_cmd))
        app.add_handler(CommandHandler("pause", pause_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("addnumbers", addnumbers_cmd))
        app.add_handler(CommandHandler("clearnumbers", clearnumbers_cmd))
        app.add_handler(CommandHandler("lognumbers", lognumbers_cmd))
        app.add_handler(MessageHandler(
            tg_filters.TEXT & ~tg_filters.COMMAND, on_plain_message
        ))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    try:
        asyncio.run(main_tg())
    except Exception as e:
        logging.warning(f"Telegram bot error: {e}")


# ---- Entry point -----------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_numbers_from_file()
    tg_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    tg_thread.start()
    flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
