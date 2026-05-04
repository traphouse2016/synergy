Synergy 1.0
==========

QUICK START
-----------
1. Right-click install.bat -> Run as Administrator
   Installs: Python 3.11, pip packages, Node.js 20, Electron, Chromium

2. Double-click launch.bat (or the Desktop shortcut)

3. Settings tab:
   - Add Google Voice accounts
   - Add Telegram bot token + user ID
   - Set audio file paths
   - Enable DTMF Detection

4. Numbers tab -> either select a .txt batch file or paste numbers manually -> Load to Queue

5. Dashboard -> Start Campaign


CALL WORKFLOW (DTMF enabled)
-----------------------------
  Ring -> Pickup -> Play audio -> Wait for keypress
  -> Telegram: "PRESS 1 RECEIVED from +1310..." -> Hang up


FILES
-----
  install.bat     One-time setup (run as Admin)
  launch.bat      Start the bot daily
  stop.bat        Force-quit everything
  backend/        Python Flask server
  electron/       Electron UI


DATA LOCATION
-------------
  %USERPROFILE%\gv_bot\settings.json
  %USERPROFILE%\gv_bot
umbers.txt
  %USERPROFILE%\gv_botudio\     <- put MP3/WAV files here


TELEGRAM SETUP
--------------
  1. Message @BotFather -> /newbot -> copy token
  2. Message @userinfobot -> copy your user ID
  3. Paste both in Settings tab


AUDIO (VB-Cable)
----------------
  https://vb-audio.com/Cable/
  Set VB-Cable as default Windows playback device.
  Google Voice picks up audio through the virtual mic.


Sanitized release notes:
- No saved accounts are prefilled by default.
- No Telegram bot token or Telegram user ID is prefilled.
- No compiled __pycache__ files are included.
- Browser sessions are only created after you log in manually on this machine.


DEFAULT BASE: This fixed37 build is the default base for future edits. It includes the guarded fixed30 mid-audio DTMF interrupt path in both the main live-call flow and the test-call flow.


DEFAULT BASE: fixed39_default_base_press1_safe. Mid-audio DTMF now skips extra post-audio waiting, but still honors press1 playback before hangup.


DEFAULT BASE: fixed41_persistent_profiles_loginfix. Real Chromium launches now use persistent profiles/, Login sends account email correctly, and the UI/logs expose the actual profile path.


DEFAULT BASE: fixed44_auto_login_restored_persistent_profiles. Restores auto-login via saved settings credentials while keeping persistent Chromium profiles and the fixed UI startup path.
