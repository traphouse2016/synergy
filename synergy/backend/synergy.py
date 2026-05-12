# DEFAULT_BASE_BUILD: fixed46_press1_tg_filter_vm_fastfail_contacts_parser_full_merge
#!/usr/bin/env python3
"""Synergy 1.0 — FFT-based DTMF detection via Web Audio CDP injection"""

import os, json, time, re, threading, logging, subprocess, asyncio, tempfile, shutil
from datetime import datetime
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.common.exceptions import WebDriverException

BASE_DIR   = os.path.join(os.path.expanduser("~"), "synergy")
SETTINGS_F = os.path.join(BASE_DIR, "settings.json")
NUMBERS_F  = os.path.join(BASE_DIR, "numbers.txt")
PROFILES_D = os.path.join(BASE_DIR, "profiles")
AUDIO_D    = os.path.join(BASE_DIR, "audio")
for _d in [BASE_DIR, PROFILES_D, AUDIO_D]:
    os.makedirs(_d, exist_ok=True)

# Bump this any time DEFAULT_SETTINGS keys/structure changes.
# Any saved settings.json with a different version is wiped on load.
SETTINGS_VERSION = 5

DEFAULT_SETTINGS = {
    "settings_version":      SETTINGS_VERSION,
    "accounts":              [{"email": "", "password": "", "profile": "profile_1"}],
    "telegram_bot_token":    "",
    "telegram_user_id":      "",
    "delay_between_calls":   45,
    "concurrent_limit":      1,
    "headless":              False,
    "rotate_accounts":       True,
    "vm_detection_enabled":  False,
    "vm_hangup":             True,
    "vm_action":             "hangup",
    "screen_hangup_enabled": False,
    "screen_calls_enabled":  False,
    "screen_hangup_action":  "hangup",  # hangup | play_audio
    "dtmf_enabled":          False,
    "dtmf_timeout":          20,
    "dtmf_key_to_detect":   "1",
    "audio_initial":         "",
    "audio_screen_bypass":  "",
    "audio_press1":          "",
    "verbose_debug":         False,
    "dtmf_level2_enabled":   False,
    "dtmf_level2_key":       "1",
    "audio_level2":          "",
}

def _deep_merge(base, override):
    """FIX #75: deep merge so nested keys in DEFAULT_SETTINGS auto-fill."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_settings():
    if os.path.exists(SETTINGS_F):
        try:
            with open(SETTINGS_F) as f:
                saved = json.load(f)
        except Exception:
            saved = {}
        saved_ver = saved.get("settings_version", 0)
        if saved_ver != SETTINGS_VERSION:
            fresh = dict(DEFAULT_SETTINGS)
            if "accounts" in saved and saved["accounts"]:
                fresh["accounts"] = saved["accounts"]
            _ver_msg = (f"[settings] Version mismatch (saved={saved_ver} current={SETTINGS_VERSION})"
                        f" -- settings reset to defaults. Accounts preserved.")
            print(_ver_msg)
            import logging as _logging; _logging.warning(_ver_msg)
            save_settings_to_disk(fresh)
            return fresh
        return _deep_merge(DEFAULT_SETTINGS, saved)
    save_settings_to_disk(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)

def save_settings_to_disk(s):
    s["settings_version"] = SETTINGS_VERSION
    with open(SETTINGS_F, "w") as f:
        json.dump(s, f, indent=2)

settings = load_settings()

_clear_numbers_file_sentinel = None

_login_status_lock = threading.Lock()

state = {
    "running": False,
    "paused": False,
    "login_status": {}, "numbers": [], "completed": 0, "failed": 0,
    "total": 0, "current_number": "", "current_account": "",
    "log": [], "_stop": False,
    "vm": 0,
}

def load_numbers_from_file():
    if os.path.exists(NUMBERS_F):
        with open(NUMBERS_F) as f:
            raw_lines = [l.strip() for l in f if l.strip()]
        entries, skipped = _parse_contact_lines(raw_lines)
        state.update({"numbers": entries, "total": len(entries), "completed": 0, "failed": 0})
        log_msg(f"Loaded {len(entries)} contact(s) from file ({skipped} skipped).")

def _clear_numbers_file():
    """Wipe numbers.txt on startup so old queues never auto-reload."""
    try:
        if os.path.exists(NUMBERS_F):
            open(NUMBERS_F, "w").close()
    except Exception:
        pass

_clear_numbers_file()  # FIX: wipe stale queue now that function is defined

# ── Contact parser helpers ────────────────────────────────────────────────────
def _extract_phone(line):
    """Extract the first valid 10-digit US phone number from any line format.
    Strips +1 prefix, handles parens/dashes/dots/spaces/semicolons.
    Skips ISO date/time sequences so 2021-02-16 07:00:45 is never misread."""
    # Remove ISO datetime / date patterns before extracting digits
    cleaned = re.sub(
        r'\b\d{4}[-/]\d{2}[-/]\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?\b',
        '', line
    )
    # Strip +1 country code in various spacings
    cleaned = re.sub(r'\+\s*1[\s\-.(]?', '', cleaned)
    # Collect all digit runs, find first 10-digit sequence
    digits = ''.join(re.findall(r'\d+', cleaned))
    m = re.search(r'\d{10}', digits)
    return m.group(0) if m else None


def _parse_contact_lines(lines):
    """Parse raw contact lines into pipe-delimited entries.
    Returns (entries, skipped_count).
    Each entry: '5551234567|original full line'.
    Deduplicates by extracted phone number."""
    entries = []
    seen    = set()
    skipped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        num = _extract_phone(line)
        if not num:
            skipped += 1
            continue
        if num in seen:
            skipped += 1
            continue
        seen.add(num)
        entries.append(f"{num}|{line}")
    return entries, skipped


def _unpack_entry(entry):
    """Unpack a contact entry back into (dial_number, contact_label).
    Backward-compatible with legacy bare-number entries (no pipe)."""
    if '|' in entry:
        num, label = entry.split('|', 1)
        return num.strip(), label.strip()
    return entry.strip(), entry.strip()


def log_msg(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    state["log"].append(entry)
    if len(state["log"]) > 500:
        state["log"] = state["log"][-500:]
    logging.info(msg)
    if level == "error" and not settings.get("verbose_debug", False):
        tg_notify(f"\u26a0\ufe0f ERROR\n{msg}")

def debug_msg(msg):
    if settings.get("verbose_debug", False):
        log_msg(f"[debug] {msg}", "info")

import requests as _tg_req
from urllib3.util.retry import Retry as _Retry
_tg_session = _tg_req.Session()
_tg_retry = _Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
    raise_on_status=False,
)
_tg_adapter = _tg_req.adapters.HTTPAdapter(
    pool_connections=8,
    pool_maxsize=32,
    max_retries=_tg_retry,
)
_tg_session.mount("https://", _tg_adapter)
_tg_session.mount("http://",  _tg_adapter)

import concurrent.futures as _cf
_tg_executor = _cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix='tg_notify')

def _tg_send(msg):
    token = settings.get('telegram_bot_token', '')
    uid   = settings.get('telegram_user_id', 0)
    if not token or not uid:
        return
    try:
        _tg_session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": uid, "text": msg}, timeout=8
        )
    except Exception:
        pass

def tg_notify(msg):
    _tg_executor.submit(_tg_send, msg)

def tg_notify_dtmf(number, key, account_email, contact_label=None):
    """Fire-and-forget DTMF/press1 Telegram notification with full contact line."""
    contact_line = contact_label if contact_label else number
    msg = (
        f"\U0001f7e2 PRESS {key} RECEIVED\n"
        f"Contact: {contact_line}\n"
        f"Number: {number}\n"
        f"Account: {account_email}"
    )
    _tg_executor.submit(_tg_send, msg)


def tg_notify_dtmf_level2(number, key, account_email, contact_label=None):
    """Fire-and-forget Level 2 DTMF Telegram notification."""
    contact_line = contact_label if contact_label else number
    msg = (
        f"\U0001f7e3 LEVEL 2 PRESS {key} CONFIRMED\n"
        f"Contact: {contact_line}\n"
        f"Number: {number}\n"
        f"Account: {account_email}"
    )
    _tg_executor.submit(_tg_send, msg)

_PLAY_AUDIO_JS = """
(async function(b64, mime) {
  try {
    if (!window._gvOutCtx) {
      window._gvOutCtx = new (window.AudioContext || window.webkitAudioContext)();
      window._gvOutDest = window._gvOutCtx.createMediaStreamDestination();
      window._gvOutStream = window._gvOutDest.stream;
      var pcs = window._gvPCs || [];
      for (var j = 0; j < pcs.length; j++) {
        var senders = pcs[j].getSenders ? pcs[j].getSenders() : [];
        for (var k = 0; k < senders.length; k++) {
          if (senders[k].track && senders[k].track.kind === 'audio') {
            try { senders[k].replaceTrack(window._gvOutStream.getAudioTracks()[0]); } catch(e2) {}
          }
        }
      }
    }

    var raw = atob(b64);
    var buf = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);

    var ctx = window._gvOutCtx;
    ctx.decodeAudioData(buf.buffer).then(function(audioBuf) {
      var src = ctx.createBufferSource();
      src.buffer = audioBuf;
      src.connect(window._gvOutDest);
      window._gvAudioSrc = src;
      window._gvAudioPlaying = true;
      src.onended = function() {
        window._gvAudioDone = true;
        window._gvAudioPlaying = false;
      };
      setTimeout(function() {
        window._gvAudioDone = false;
        src.start();
      }, 120);
    }).catch(function(e){
      console.error('GV audio decode error', e);
      window._gvAudioDone = true;
      window._gvAudioPlaying = false;
    });
  } catch(e) {
    window._gvAudioDone = true;
    window._gvAudioPlaying = false;
    console.error('GV audio inject error', e);
  }
})
"""
# ── DTMF REGRESSION GUARD ─────────────────────────────────────────────────────
# NOTE: This play_audio_in_tab() implementation is intentionally kept aligned
# with the fixed30 mid-prompt DTMF interrupt behavior.
#
# DO NOT refactor/remove the watcher-thread path below unless you test and
# confirm all of the following log sequence still appears when pressing 1 during
# the initial prompt:
#   [audio][dtmf] Prompt listener started — watching for key during audio
#   [audio][dtmf] Thread saw key '1' during prompt
#   [audio][dtmf] Interrupting prompt for key '1'
#   [audio] Playing press1 audio after DTMF interrupt
#
# Required behavior to preserve:
# - dedicated _dtmf_poll_thread when dtmf_interrupt=True
# - dtmf_event/dtmf_result/stop_poll control flow
# - immediate prompt stop via window._gvAudioSrc.stop() on key detection
# - tg_notify_dtmf(number, key, account_email) on interrupt
# - _stop_dtmf_listen(driver) only after prompt path completes
#
# If prompt playback or login flow needs changes, keep them outside this guarded
# control path unless the DTMF prompt-interrupt regression test is rerun.
# ───────────────────────────────────────────────────────────────────────────────
def play_audio_in_tab(driver, filepath, block=True, dtmf_interrupt=False,
                       press1_filepath=None, number=None, account_email=None, contact_label=None):
    """Inject and play audio directly into the GV WebRTC stream for this tab only.
    When dtmf_interrupt=True, a background thread watches window._gvDTMF.detected
    while the main thread waits for audio to finish. If a key is seen before the
    prompt ends, the prompt is stopped and press1_filepath plays immediately.
    """
    import base64
    if not filepath or not os.path.exists(filepath):
        log_msg(f"[audio] File not found: {filepath}", "warning"); return None

    detected_key  = None
    dtmf_event    = threading.Event()
    dtmf_result   = [None]
    stop_poll     = None

    def _dtmf_poll_thread():
        """Background DTMF watcher — never throws, never breaks early."""
        while not stop_poll.is_set():
            try:
                cur = driver.execute_script(
                    "return (window._gvDTMF && window._gvDTMF.detected) ? window._gvDTMF.detected : null;"
                )
                if cur is not None:
                    dtmf_result[0] = cur
                    dtmf_event.set()
                    log_msg(f"[audio][dtmf] Thread saw key '{cur}' during prompt", "info")
                    return
            except Exception:
                pass
            time.sleep(0.15)

    try:
        ext  = filepath.rsplit(".", 1)[-1].lower()
        mime = {"mp3": "audio/mpeg", "wav": "audio/wav",
                "ogg": "audio/ogg", "m4a": "audio/mp4"}.get(ext, "audio/wav")
        with open(filepath, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()

        log_msg(f"[audio] Injecting into tab: {os.path.basename(filepath)}", "info")
        driver.execute_script("window._gvAudioDone = false; window._gvAudioPlaying = false;")

        if dtmf_interrupt:
            stop_poll = threading.Event()
            driver.execute_script("""
                window._gvDTMF = window._gvDTMF || {};
                window._gvDTMF.detected   = null;
                window._gvDTMF.detectedAt = 0;
                window._gvDTMF.history    = [];
                window._gvDTMF.listening  = false;
            """)
            _start_dtmf_listen(driver)
            log_msg("[audio][dtmf] Prompt listener started — watching for key during audio", "info")
            poll_thread = threading.Thread(target=_dtmf_poll_thread, daemon=True)
            poll_thread.start()

        driver.execute_script(_PLAY_AUDIO_JS + f"('{b64}', '{mime}');")

        if block:
            time.sleep(0.25)
            for _ in range(600):
                audio_done = False
                try:
                    audio_done = driver.execute_script("return window._gvAudioDone === true;")
                except Exception:
                    pass

                if dtmf_interrupt and dtmf_event.is_set():
                    detected_key = dtmf_result[0]
                    log_msg(f"[audio][dtmf] Interrupting prompt for key '{detected_key}'", "success")
                    if number and account_email:
                        tg_notify_dtmf(number, detected_key, account_email, contact_label=contact_label)
                    try:
                        driver.execute_script("""
                            if (window._gvAudioSrc) {
                                try { window._gvAudioSrc.stop(); } catch(e2) {}
                            }
                            window._gvAudioDone = true;
                            window._gvAudioPlaying = false;
                        """)
                    except Exception:
                        pass
                    break

                if audio_done:
                    break

                time.sleep(0.15)

        if dtmf_interrupt and stop_poll is not None:
            stop_poll.set()
            _stop_dtmf_listen(driver)

        log_msg(f"[audio] Done: {os.path.basename(filepath)}", "info")

    except Exception as e:
        log_msg(f"[audio] Inject error: {e}", "error")
        if dtmf_interrupt and stop_poll is not None:
            stop_poll.set()

    return detected_key

def clear_cache():
    cleared = 0
    if os.path.isdir(PROFILES_D):
        for profile in os.listdir(PROFILES_D):
            for cache_dir in ["Cache", "Code Cache", "GPUCache", "Service Worker"]:
                path = os.path.join(PROFILES_D, profile, "Default", cache_dir)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True); cleared += 1
    log_msg(f"Cache cleared ({cleared} dirs removed).", "success")
    return cleared

def _find_chromium():
    candidates = [
        os.path.join(BASE_DIR, 'chromium', 'chrome-win64', 'chrome.exe'),
        os.path.join(BASE_DIR, 'chromium', 'chrome.exe'),
        os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles%\Chromium\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Chromium\Application\chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            log_msg(f"[driver] Using Chromium binary: {p}", "info")
            return p
    found = shutil.which('chromium') or shutil.which('chromium-browser')
    if found:
        log_msg(f"[driver] Using Chromium binary from PATH: {found}", "info")
        return found
    log_msg("[driver] No Chromium binary found. Run install.bat.", "error")
    return None


def make_options(profile_name, headless=False):
    opts = Options()
    profile_path = os.path.join(PROFILES_D, profile_name)
    os.makedirs(profile_path, exist_ok=True)

    for _stale in ["SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"]:
        _sp = os.path.join(profile_path, _stale)
        try:
            if os.path.exists(_sp):
                os.remove(_sp)
        except Exception:
            pass

    prefs = {
        "profile.default_content_setting_values.media_stream_mic": 1,
        "profile.default_content_setting_values.media_stream_camera": 1,
        "profile.default_content_setting_values.notifications": 1,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }

    args = [
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=Translate,AutomationControlled,RendererCodeIntegrity",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--disable-popup-blocking",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=1366,900",
        "--start-maximized",
        "--remote-debugging-pipe",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    for a in args:
        opts.add_argument(a)

    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")

    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("prefs", prefs)

    chromium = _find_chromium()
    if chromium:
        opts.binary_location = chromium

    return opts

# ─────────────────────────────────────────────────────────────────────────────
# Web Audio + DTMF hook (injected via CDP on every new document)
# ─────────────────────────────────────────────────────────────────────────────
_WEBAUDIO_HOOK = r"""
(function() {
  if (window._gvHooked) return;
  window._gvHooked = true;

  window._gvCallState = {
    connected: false, classifying: false,
    totalSpeechMs: 0, silenceMs: 0,
    energy: 0, burstMs: 0, maxBurstMs: 0,
    phraseCount: 0, inSpeech: false,
    // silence-triggered classification fields
    firstBurstMs: 0, firstBurstLocked: false,
    currentBurstMs: 0, silenceAfterFirst: 0,
    consecutiveSilenceMs: 0, speechStarted: false
  };

  window._gvStartClassify = function() {
    var s = window._gvCallState;
    s.totalSpeechMs = 0; s.silenceMs = 0;
    s.burstMs = 0; s.maxBurstMs = 0;
    s.phraseCount = 0; s.inSpeech = false;
    s.classifying = true;
    // reset silence-trigger fields
    s.firstBurstMs = 0; s.firstBurstLocked = false;
    s.currentBurstMs = 0; s.silenceAfterFirst = 0;
    s.consecutiveSilenceMs = 0; s.speechStarted = false;
  };

  window._gvDTMF = { detected: null, detectedAt: 0, history: [], listening: false };
  window._gvStartDTMF = function() {
    window._gvDTMF.detected   = null;
    window._gvDTMF.detectedAt = 0;
    window._gvDTMF.history    = [];
    window._gvDTMF.listening  = true;
  };
  window._gvStopDTMF = function() { window._gvDTMF.listening = false; };

  var ROW_FREQS  = [697, 770, 852, 941];
  var COL_FREQS  = [1209, 1336, 1477, 1633];
  var DTMF_TABLE = [
    ['1','2','3','A'],
    ['4','5','6','B'],
    ['7','8','9','C'],
    ['*','0','#','D']
  ];
  var SR       = 16000;
  var FFTSIZE  = 2048;
  var BIN_HZ   = SR / FFTSIZE;
  var THRESH   = 0.008;
  var DTMF_DB  = 55;
  var DTMF_THR = Math.round(DTMF_DB / 100 * 255);
  var DEBOUNCE = 300;

  function freqToBin(f) { return Math.round(f / BIN_HZ); }
  var rowBins = ROW_FREQS.map(freqToBin);
  var colBins = COL_FREQS.map(freqToBin);

  function attachTrack(track) {
    if (track.kind !== 'audio') return;
    window._gvCallState.connected = true;
    try {
      var ctx = new AudioContext({ sampleRate: SR });
      var src = ctx.createMediaStreamSource(new MediaStream([track]));

      var enAn = ctx.createAnalyser(); enAn.fftSize = 256;
      var enBuf = new Float32Array(enAn.fftSize);
      src.connect(enAn);

      var TICK = 40;
      setInterval(function() {
        enAn.getFloatTimeDomainData(enBuf);
        var rms = 0;
        for (var i = 0; i < enBuf.length; i++) rms += enBuf[i] * enBuf[i];
        rms = Math.sqrt(rms / enBuf.length);
        window._gvCallState.energy = rms;
        if (!window._gvCallState.classifying) return;
        var s = window._gvCallState;
        if (rms > THRESH) {
          s.totalSpeechMs += TICK;
          s.currentBurstMs += TICK;
          s.burstMs += TICK;
          s.consecutiveSilenceMs = 0;
          s.speechStarted = true;
          if (s.burstMs > s.maxBurstMs) s.maxBurstMs = s.burstMs;
          if (!s.firstBurstLocked) s.firstBurstMs = s.currentBurstMs;
          if (!s.inSpeech) { s.inSpeech = true; s.phraseCount++; }
        } else {
          s.silenceMs += TICK;
          s.consecutiveSilenceMs += TICK;
          if (s.speechStarted) s.silenceAfterFirst += TICK;
          if (s.inSpeech) {
            s.inSpeech = false;
            s.firstBurstLocked = true;  // first burst has ended — lock it
          }
          s.burstMs = 0;
          s.currentBurstMs = 0;
        }
      }, TICK);

      var dtAn = ctx.createAnalyser(); dtAn.fftSize = FFTSIZE;
      var dtBuf = new Uint8Array(dtAn.frequencyBinCount);
      src.connect(dtAn);

      setInterval(function() {
        if (!window._gvDTMF.listening) return;
        dtAn.getByteFrequencyData(dtBuf);

        var bestRow = -1, bestRowVal = 0;
        for (var r = 0; r < rowBins.length; r++) {
          var v = dtBuf[rowBins[r]];
          if (v > bestRowVal) { bestRowVal = v; bestRow = r; }
        }
        var bestCol = -1, bestColVal = 0;
        for (var c = 0; c < colBins.length; c++) {
          var v = dtBuf[colBins[c]];
          if (v > bestColVal) { bestColVal = v; bestCol = c; }
        }

        if (bestRowVal < DTMF_THR || bestColVal < DTMF_THR) return;

        var rowOk = true, colOk = true;
        for (var r2 = 0; r2 < rowBins.length; r2++) {
          if (r2 !== bestRow && dtBuf[rowBins[r2]] > bestRowVal * 0.75) { rowOk = false; break; }
        }
        for (var c2 = 0; c2 < colBins.length; c2++) {
          if (c2 !== bestCol && dtBuf[colBins[c2]] > bestColVal * 0.75) { colOk = false; break; }
        }
        if (!rowOk || !colOk) return;

        var key = DTMF_TABLE[bestRow][bestCol];
        var now = Date.now();
        var d   = window._gvDTMF;
        if (d.detected === key && (now - d.detectedAt) < DEBOUNCE) return;

        d.detected   = key;
        d.detectedAt = now;
        d.history.push({ key: key, at: now });
        console.log('[GV DTMF] Key detected:', key,
                    'row:', bestRowVal, 'col:', bestColVal);
      }, 40);

    } catch(e) {
      window._gvCallState._err = String(e);
    }
  }

  window._gvPCs = window._gvPCs || [];
  var _PC = window.RTCPeerConnection;
  function HPC() {
    var pc = new _PC(...arguments);
    window._gvPCs.push(pc);
    pc.addEventListener('track', function(e) { attachTrack(e.track); });
    return pc;
  }
  HPC.prototype = _PC.prototype;
  HPC.generateCertificate = _PC.generateCertificate;
  Object.defineProperty(HPC, 'name', { value: 'RTCPeerConnection' });
  window.RTCPeerConnection = HPC;
})();
"""

_GV_DIAL_JS = r"""
(function(number) {
  window._gvDialResult = null;
  window._gvDialError = null;
  window._gvDialDebug = [];

  function deepQ(root, sel) {
    var el = root.querySelector(sel); if (el) return el;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      if (all[i].shadowRoot) {
        var r = deepQ(all[i].shadowRoot, sel);
        if (r) return r;
      }
    }
    return null;
  }

  function fakeInput(el, val) {
    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

  async function waitFor(selList, tries, pause) {
    for (var i = 0; i < tries; i++) {
      for (var s of selList) {
        var el = deepQ(document, s);
        if (el) return el;
      }
      await sleep(pause);
    }
    return null;
  }

  function isEnabled(el) {
    if (!el) return false;
    if (el.disabled) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  }

  (async function() {
    var inputSelectors = [
      "input[placeholder='Enter a name or number']",
      "input[aria-label='Enter a name or number']",
      "input[aria-label*='name or number' i]",
      "gv-search-input input",
      "input[type='tel']",
      "input[type='text']"
    ];

    var buttonSelectors = [
      "[gv-test-id='new-call-button']",
      "button[aria-label='Call']",
      "button[aria-label*='Call' i]",
      "button[data-tooltip*='Call' i]"
    ];

    var liveCallSelectors = [
      "[gv-test-id='in-call-end-call']",
      "button[aria-label='Hang up call']",
      "button[aria-label='End call']",
      "gv-active-call"
    ];

    var inp = await waitFor(inputSelectors, 40, 250);
    if (!inp) {
      window._gvDialError = 'no_input';
      return;
    }

    inp.focus();
    fakeInput(inp, '');
    await sleep(150);
    fakeInput(inp, number);
    inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    inp.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    await sleep(1200);

    var callBtn = await waitFor(buttonSelectors, 30, 250);
    if (isEnabled(callBtn)) {
      callBtn.click();
    } else {
      inp.focus();
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
      inp.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    }

    for (var t = 0; t < 80; t++) {
      var live = await waitFor(liveCallSelectors, 1, 0);
      if (live) {
        window._gvDialResult = true;
        return;
      }
      await sleep(250);
    }

    window._gvDialDebug = {
      number: number,
      callButtonFound: !!callBtn,
      callButtonEnabled: isEnabled(callBtn),
      title: document.title,
      url: location.href,
      bodyText: (document.body && document.body.innerText ? document.body.innerText.slice(0, 1200) : '')
    };
    window._gvDialError = 'call_not_started';
  })();
})(arguments[0]);
"""

_GV_HANGUP_JS = r"""
(function() {
  function deepQ(root, sel) {
    var el = root.querySelector(sel); if (el) return el;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      if (all[i].shadowRoot) { var r = deepQ(all[i].shadowRoot, sel); if (r) return r; }
    } return null;
  }
  var btn = deepQ(document, '[gv-test-id="in-call-end-call"]') ||
            deepQ(document, 'button[aria-label="Hang up call"]') ||
            deepQ(document, 'button[aria-label="End call"]');
  if (btn) btn.click();
})();
"""


# ── Ban / suspension page detection ──────────────────────────────────────────
_BAN_PAGE_INDICATORS = [
    "this account has been disabled",
    "your account has been disabled",
    "account disabled",
    "account has been suspended",
    "this account has been suspended",
    "this google account has been disabled",
    "this account has been deactivated",
    "your google voice number has been disconnected",
    "google voice is not available",
    "not able to make calls",
    "this google voice number is no longer active",
    "verify your account",
    "we've detected unusual activity",
    "unusual activity on your account",
]

def _detect_ban(driver):
    """Check current page for Google ban/suspension indicators.
    Returns (is_banned: bool, reason: str|None)."""
    try:
        body = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        ) or ""
        title = ""
        try:
            title = driver.title or ""
        except Exception:
            pass
        content = (body + " " + title).lower()
        for indicator in _BAN_PAGE_INDICATORS:
            if indicator in content:
                return True, indicator
        return False, None
    except Exception:
        return False, None

def _inject_audio_hook(driver):
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _WEBAUDIO_HOOK})
    except Exception as e:
        log_msg(f"[audio hook] CDP inject failed (non-fatal): {e}", "warning")

def get_or_create_driver(account):
    key = profile_name_for_account(account)
    # FIX #61: derive account_index from position in settings accounts list
    try:
        acct_list = settings.get("accounts", [])
        account_index = next(
            (i for i, a in enumerate(acct_list) if a.get("email") == account.get("email")),
            0
        )
    except Exception:
        account_index = 0

    if key in _drivers:
        if _is_driver_alive(_drivers[key]):
            return _drivers[key]
        log_msg(f"[driver] Session dead for {key}; relaunching", "warning")
        try:
            _drivers[key].quit()
        except Exception:
            pass
        del _drivers[key]

    log_msg(f"[driver] Launching browser for {account['email']}", "info")
    acc = dict(account)
    acc['profile'] = key

    try:
        d = get_driver(key, headless=False)
    except Exception as e:
        log_msg(f"[driver] Visible launch failed for {account['email']}: {e}", "error")
        raise

    try:
        if not ensure_voice_ready(d, account_index=account_index):
            raise RuntimeError('voice_preflight_failed')
    except Exception as e:
        try:
            d.quit()
        except Exception:
            pass
        log_msg(f"[driver] Session unstable before login for {account['email']}: {e}", "error")
        raise

    already_logged_in = gv_login(d, acc)

    if settings.get('headless') and already_logged_in:
        log_msg(f"[driver] Relaunching headless for {key}", "info")
        try:
            d.quit()
        except Exception:
            pass
        try:
            d = get_driver(key, headless=True)
            if not ensure_voice_ready(d, account_index=account_index):
                raise RuntimeError('voice_preflight_failed_headless')
        except Exception as e:
            log_msg(f"[driver] Headless relaunch failed for {account['email']}: {e}", "warning")
            try:
                d.quit()
            except Exception:
                pass
            d = get_driver(key, headless=False)
            if not ensure_voice_ready(d, account_index=account_index):
                raise RuntimeError('voice_preflight_failed_visible_fallback')
            gv_login(d, acc)

    _drivers[key] = d
    return d


def release_drivers():
    for d in list(_drivers.values()):
        try: d.quit()
        except Exception: pass
    _drivers.clear()

# ── VM / screening detection ──────────────────────────────────────────────────
_VM_MAXBURST_MS       = 3500   # ms continuous burst = voicemail monologue
_SCREEN_SPEECH_MS     = 1000   # ms first burst = screener question
_HUMAN_FIRST_BURST_MS = 900    # ms first burst ceiling = human "Hello?"
_SILENCE_TO_CLASSIFY  = 1800   # ms consecutive silence = caller done speaking
_SILENCE_AFTER_AUDIO  = 2000   # ms silence to wait before playing bypass audio
_PICKUP_TIMEOUT_S     = 55
_SEL_ENDED            = ["[aria-label*='Call ended' i]"]

_VM_PHRASES = [
    "leave a message", "after the beep", "record your message",
    "not available", "please leave", "sorry", "unavailable",
    "please try again", "not able to come to the phone",
    "cannot take your call", "is not available",
    "voicemail", "leave a detailed message",
]

_SCREEN_PHRASES = [
    "call assistant", "google call screening", "screening your call",
    "state your name", "reason for calling", "who's calling",
    "who is calling", "what's this regarding", "what is this regarding",
    "i can connect you", "i'll let them know", "let them know you called",
    "handle calls", "i'm a google assistant", "call screener",
    "screen your calls", "asking for",
]


def get_driver(profile_name, headless=False):
    opts = make_options(profile_name, headless=headless)
    binary = _find_chromium()
    if not binary:
        raise RuntimeError("Chromium binary not found")
    opts.binary_location = binary

    last_err = None
    try:
        service = Service(ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install())
        d = webdriver.Chrome(service=service, options=opts)
        try:
            d.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        except Exception:
            pass
        _inject_audio_hook(d)
        return d
    except Exception as e:
        last_err = e
        log_msg(f"[driver] Chromium launch attempt failed: {e}", "warning")

    raise last_err


def gv_login(driver, account):
    """Navigate to GV login page and auto-fill credentials."""
    login_url = (
        "https://accounts.google.com/signin/v2/identifier?continue="
        "https%3A%2F%2Fvoice.google.com%2F&service=grandcentral&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
    )
    driver.get(login_url)
    time.sleep(1.2)

    # BAN CHECK: detect suspension before attempting login
    is_banned, ban_reason = _detect_ban(driver)
    if is_banned:
        log_msg(f"[login] \u26d4 BAN DETECTED for {account['email']}: {ban_reason}", "error")
        tg_notify(f"\u26d4 BANNED: {account['email']}\nReason: {ban_reason}")
        return False

    try:
        if "voice.google.com" in driver.current_url and "accounts.google.com" not in driver.current_url:
            log_msg(f"Already logged in: {account['email']}", "success")
            return True
    except Exception:
        pass

    try:
        ef = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email'], input[name='identifier']"))
        )
        try:
            ef.clear()
        except Exception:
            pass
        ef.send_keys(account["email"])
        try:
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "identifierNext"))
            ).click()
        except Exception:
            ef.send_keys(Keys.RETURN)

        pf = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password'], input[name='Passwd'], input[name='password']"))
        )
        try:
            pf.click()
        except Exception:
            pass
        try:
            pf.clear()
        except Exception:
            pass

        pw = account.get("password", "") or ""
        wrote = False
        try:
            pf.send_keys(pw)
            wrote = True
        except Exception:
            wrote = False

        if not wrote:
            try:
                driver.execute_script("""
                    const el = arguments[0], val = arguments[1];
                    el.focus();
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                """, pf, pw)
                wrote = True
            except Exception:
                wrote = False

        time.sleep(0.5)
        try:
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "passwordNext"))
            ).click()
        except Exception:
            try:
                pf.send_keys(Keys.RETURN)
            except Exception:
                pass

    except Exception as e:
        log_msg(f"Auto-login error: {e} — complete manually", "warning")

    log_msg(f"Waiting for login: {account['email']} — complete 2FA in the browser", "warning")
    for _ in range(120):
        try:
            cur = driver.current_url
            if "challenge" in cur:
                log_msg(f"Google login challenge for {account['email']} — complete manually once; session will persist after that", "warning")
            elif "voice.google.com" in cur and "accounts.google.com" not in cur:
                log_msg(f"Login successful: {account['email']}", "success")
                return True
        except Exception:
            pass
        time.sleep(1)
    log_msg(f"Login timeout: {account['email']}", "error")
    return False

_drivers = {}
drivers = _drivers


def _is_driver_alive(driver):
    try: _ = driver.current_url; return True
    except Exception: return False


def profile_name_for_account(account):
    import re as _re2
    explicit = account.get("profile", "").strip()
    _auto = _re2.match(r"^profile[_]?\d+$", explicit, _re2.IGNORECASE)
    if explicit and not _auto:
        return explicit
    email = account.get("email", "").strip()
    if email:
        name = _re2.sub(r"[^a-zA-Z0-9]", "", email.split("@")[0])
        return f"gvbot_{name}"
    return explicit or "gvbot_default"


def safe_get(driver, url, retries=2):
    """FIX #29: broadened catch to cover ConnectionError, TimeoutError, WebDriverException."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            driver.get(url)
            return True
        except (ConnectionError, TimeoutError, WebDriverException) as e:
            last_err = e
            log_msg(f"[driver] Transient error navigating to {url}; retry {attempt + 1}/{retries + 1}: {e}", "warning")
            time.sleep(1.5)
            continue
        except Exception as e:
            raise
    raise last_err


_ERROR_PAGE_INDICATORS = [
    "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED", "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED", "ERR_CONNECTION_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE",
    "This site can't be reached", "No internet", "DNS_PROBE",
]

def ensure_voice_ready(driver, account_index=0):
    """FIX #61: derive /u/{index}/calls from account_index.
    FIX #30: check for error page indicators before returning True.
    """
    url = f"https://voice.google.com/u/{account_index}/calls"
    try:
        safe_get(driver, url)
        WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)
        try:
            body_text = driver.execute_script("return document.body ? document.body.innerText : '';")
            for indicator in _ERROR_PAGE_INDICATORS:
                if indicator in (body_text or ""):
                    log_msg(f"[driver] Error page detected: {indicator}", "error")
                    return False
            # BAN CHECK: detect account suspension/disabled pages
            is_banned, ban_reason = _detect_ban(driver)
            if is_banned:
                log_msg(f"[driver] \u26d4 BAN PAGE DETECTED: {ban_reason}", "error")
                return False
        except Exception:
            pass
        return True
    except Exception as e:
        log_msg(f"[driver] ensure_voice_ready failed: {e}", "error")
        return False


def _start_dtmf_listen(driver):
    try:
        driver.execute_script("if(window._gvStartDTMF) window._gvStartDTMF();")
    except Exception:
        pass

def _stop_dtmf_listen(driver):
    try:
        driver.execute_script("if(window._gvStopDTMF) window._gvStopDTMF();")
    except Exception:
        pass


def make_call(driver, number, _account=None):
    """Dial number via GV JS injection. Returns True if call connected."""
    try:
        driver.execute_script(_GV_DIAL_JS, number)
    except Exception as e:
        log_msg(f"[call] JS inject error: {e}", "error")
        return False

    for _ in range(80):
        try:
            result = driver.execute_script("return window._gvDialResult;")
            error  = driver.execute_script("return window._gvDialError;")
        except Exception:
            return False

        if result:
            log_msg(f"[call] Call connected: {number}", "success")
            return True
        if error:
            if error == "call_not_started":
                try:
                    dbg = driver.execute_script("return window._gvDialDebug;")
                    log_msg(f"[call] call_not_started debug: {dbg}", "warning")
                except Exception:
                    pass
            log_msg(f"[call] Dial error for {number}: {error}", "error")
            return False
        time.sleep(0.25)

    log_msg(f"[call] Dial timeout: {number}", "error")
    return False


def hang_up(driver):
    try:
        driver.execute_script(_GV_HANGUP_JS)
    except Exception as e:
        log_msg(f"[hangup] JS error: {e}", "warning")



# ── VM / screening detection ──────────────────────────────────────────────────
_VM_MAXBURST_MS       = 3500   # ms continuous burst = voicemail monologue
_SCREEN_SPEECH_MS     = 1000   # ms first burst = screener question
_HUMAN_FIRST_BURST_MS = 900    # ms first burst ceiling = human "Hello?"
_SILENCE_TO_CLASSIFY  = 1800   # ms consecutive silence = caller done speaking
_SILENCE_AFTER_AUDIO  = 2000   # ms silence to wait before playing bypass audio
_PICKUP_TIMEOUT_S     = 55
_SEL_ENDED            = ["[aria-label*='Call ended' i]"]

_VM_PHRASES = [
    "leave a message", "after the beep", "record your message",
    "not available", "please leave", "sorry", "unavailable",
    "please try again", "not able to come to the phone",
    "cannot take your call", "is not available",
    "voicemail", "leave a detailed message",
]

_SCREEN_PHRASES = [
    "call assistant", "google call screening", "screening your call",
    "state your name", "reason for calling", "who's calling",
    "who is calling", "what's this regarding", "what is this regarding",
    "i can connect you", "i'll let them know", "let them know you called",
    "handle calls", "i'm a google assistant", "call screener",
    "screen your calls", "asking for",
]

def _dom_has(driver, selectors):
    for sel in selectors:
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel): return True
        except Exception: pass
    return False

# FIX #70: removed dom_has() public alias — call _dom_has() directly

def _get_call_timer(driver):
    try:
        texts = driver.execute_script("""
            var out = [];
            document.querySelectorAll('*').forEach(function(el) {
              el.childNodes.forEach(function(n) {
                if (n.nodeType === 3) out.push(n.nodeValue.trim());
              });
            });
            return out;
        """)
        pat = re.compile(r'^\d{1,2}:\d{2}$')
        for t in texts:
            if t and pat.match(t): return t
    except Exception: pass
    return None

# FIX #70: removed get_call_timer() public alias — call _get_call_timer() directly

def _reset_classify(driver):
    try: driver.execute_script("if(window._gvStartClassify) window._gvStartClassify();")
    except Exception: pass

def _get_call_state(driver):
    """Silent wrapper — never throws, suppresses CDP/analyzer startup spam."""
    try:
        return driver.execute_script("return window._gvCallState || null;")
    except Exception:
        return None

# Alias used throughout classify paths
_get_call_state_safe = _get_call_state

def _dom_classify(driver, check_screen=True):
    """Shadow DOM traversal to detect VM or screener phrases.
    Returns 'voicemail', 'screening', or 'human'."""
    try:
        result = driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": """
                (function() {
                  var vmPhrases = """ + json.dumps(_VM_PHRASES) + """;
                  var screenPhrases = """ + json.dumps(_SCREEN_PHRASES) + """;
                  function getText(root) {
                    var text = '';
                    try { text += (root.innerText || root.textContent || '').toLowerCase(); } catch(e) {}
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i = 0; i < all.length; i++) {
                      if (all[i].shadowRoot) text += getText(all[i].shadowRoot);
                    }
                    return text;
                  }
                  var fullText = getText(document);
                  for (var p = 0; p < vmPhrases.length; p++) {
                    if (fullText.indexOf(vmPhrases[p]) !== -1) return 'voicemail';
                  }
                  for (var s = 0; s < screenPhrases.length; s++) {
                    if (fullText.indexOf(screenPhrases[s]) !== -1) return 'screening';
                  }
                  return 'human';
                })()
            """,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "human")
    except Exception:
        pass
    # fallback to shallow DOM scan if CDP fails
    scope = ""
    for sel in ["gv-call-widget", "gv-active-call", "mat-dialog-container"]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els: scope += els[0].get_attribute("innerHTML").lower()
        except Exception: pass
    if not scope:
        try: scope = driver.page_source.lower()
        except Exception: pass
    if any(p in scope for p in _VM_PHRASES):    return "voicemail"
    if check_screen and any(p in scope for p in _SCREEN_PHRASES): return "screening"
    return "human"

def _classify_audio_on_silence(cs):
    """
    Called ONLY after consecutive silence >= _SILENCE_TO_CLASSIFY ms.
    Uses the firstBurstMs (locked at end of first phrase) as primary signal.
    Returns: 'voicemail' | 'screening' | 'human' | 'dom_fallback'
    """
    first_burst  = cs.get("firstBurstMs", 0)
    max_burst    = cs.get("maxBurstMs", 0)
    total_speech = cs.get("totalSpeechMs", 0)
    phrase_count = cs.get("phraseCount", 0)

    # No speech heard at all → DOM is the only option
    if total_speech == 0:
        return "dom_fallback"

    # Long unbroken monologue = voicemail greeting
    if max_burst >= _VM_MAXBURST_MS:
        return "voicemail"

    # Many short choppy phrases = screener reading a script (e.g. Google screener)
    # A real human "Hello?" is 1-3 phrases max before waiting.
    # 5+ phrases with any meaningful total speech = screener, not human.
    if phrase_count >= 5 and total_speech >= 800:
        return "screening"

    # High total speech with multiple phrases = screener talking at length
    if phrase_count >= 3 and total_speech >= 2000:
        return "screening"

    # Short first burst + few phrases + little total speech = human "Hello?"
    if first_burst <= _HUMAN_FIRST_BURST_MS and total_speech < 1500 and phrase_count <= 3:
        return "human"

    # Medium-to-long first burst (900ms – 3500ms) = screener asking a question
    if _SCREEN_SPEECH_MS <= first_burst < _VM_MAXBURST_MS:
        return "screening"

    # Ambiguous — let DOM decide
    return "dom_fallback"


# Keep old name as thin shim so nothing else breaks
def _classify_audio(cs, elapsed_ms):
    """Legacy shim — new code uses _classify_audio_on_silence via _wait_for_pickup_and_classify."""
    return _classify_audio_on_silence(cs)

def _wait_for_silence(driver, silence_needed_ms=1800, timeout_s=35, label=""):
    """
    Block until consecutive audio silence >= silence_needed_ms OR call ends.
    Returns True when silence achieved, False on call end or timeout.
    Ensures bypass audio is NEVER played while the caller/screener is still talking.
    """
    deadline     = time.time() + timeout_s
    _last_warn   = 0.0
    while time.time() < deadline:
        if _dom_has(driver, _SEL_ENDED):
            log_msg(f"[vm] {label}silence-wait: call ended", "info")
            return False
        try:
            consec = driver.execute_script(
                "return (window._gvCallState && window._gvCallState.consecutiveSilenceMs != null)"
                " ? window._gvCallState.consecutiveSilenceMs : -1;"
            )
        except Exception:
            consec = -1
        if consec >= silence_needed_ms:
            log_msg(f"[vm] {label}silence achieved ({consec:.0f}ms) — proceeding", "info")
            return True
        # throttled log: every 5s remind us we're still waiting
        now = time.time()
        if now - _last_warn >= 5.0:
            log_msg(f"[vm] {label}still waiting for silence... ({consec:.0f}ms / {silence_needed_ms}ms)", "info")
            _last_warn = now
        time.sleep(0.08)
    log_msg(f"[vm] {label}silence timeout after {timeout_s}s — forcing classify", "warning")
    return True   # timeout: attempt classification anyway


def _classify_post_screen(driver):
    """
    Called after screener detection.
    Step 1: wait for screener to go fully silent (up to 35s — screener monologue can be 4-15s).
    Step 2: reset classifier, wait for human response OR more audio.
    Step 3: silence-triggered classification.
    Human NEVER hears bypass audio during the screener's message.
    """
    log_msg("[vm] Post-screen: waiting for screener to finish speaking...", "info")

    # ── Phase 1: wait for screener to stop talking ──────────────────────────
    ok = _wait_for_silence(driver, silence_needed_ms=_SILENCE_AFTER_AUDIO,
                           timeout_s=35, label="post-screen P1 ")
    if not ok:
        return "no_answer"
    if _dom_has(driver, _SEL_ENDED):
        return "no_answer"

    log_msg("[vm] Post-screen: screener done — ready for human response", "info")

    # ── Phase 2: reset + wait for human to respond or stay silent ──────────
    _reset_classify(driver)
    classify_start = time.time()
    RESPONSE_TIMEOUT = 12.0   # if nobody speaks within 12s after screener → no_answer

    while True:
        if _dom_has(driver, _SEL_ENDED):
            log_msg("[vm] Post-screen: call ended during response wait", "info")
            return "no_answer"

        elapsed = time.time() - classify_start
        cs = _get_call_state(driver)

        if cs:
            consec_sil = cs.get("consecutiveSilenceMs", 0)
            speech     = cs.get("speechStarted", False)
            total_sp   = cs.get("totalSpeechMs", 0)
            max_burst  = cs.get("maxBurstMs", 0)

            # If speech has started and then gone silent again → classify now
            if speech and consec_sil >= _SILENCE_TO_CLASSIFY:
                result = _classify_audio_on_silence(cs)
                if result == "dom_fallback":
                    result = _dom_classify(driver)
                log_msg(
                    f"[vm] Post-screen result: {result} "
                    f"(speech={total_sp}ms burst={max_burst}ms "
                    f"elapsed={elapsed*1000:.0f}ms)",
                    "info"
                )
                return result

            # VM monologue detected mid-stream — don't wait for full silence
            if max_burst >= _VM_MAXBURST_MS:
                log_msg(f"[vm] Post-screen: voicemail burst mid-stream ({max_burst}ms)", "info")
                return "voicemail"

        # Nobody spoke within response timeout → no_answer
        if elapsed >= RESPONSE_TIMEOUT:
            # Final DOM check
            dom = _dom_classify(driver)
            log_msg(f"[vm] Post-screen: response timeout → {dom}", "info")
            return dom if dom != "human" else "no_answer"

        time.sleep(0.08)


def _wait_for_pickup_and_classify(driver):
    """
    Wait for call timer (pickup confirmed), then use silence-triggered
    classification. Never classifies mid-sentence.
    """
    log_msg("[vm] Waiting for pickup...", "info")
    deadline = time.time() + _PICKUP_TIMEOUT_S
    while time.time() < deadline:
        if _dom_has(driver, _SEL_ENDED): return "no_answer"
        if _get_call_timer(driver):      break
        time.sleep(0.15)
    else:
        return "no_answer"

    # Log ring duration
    try:
        _ring_start = getattr(driver, "_ring_start_time", None)
        ring_ms = int((time.time() - _ring_start) * 1000) if _ring_start else None
        if ring_ms is not None:
            log_msg(f"[vm] Ring duration before pickup: {ring_ms}ms", "info")
    except Exception:
        ring_ms = None
    driver._last_ring_ms = ring_ms

    log_msg("[vm] Pickup confirmed — classifying (silence-triggered)...", "info")
    _reset_classify(driver)

    # ── Wait for first silence after speech starts ──────────────────────────
    # This ensures we never classify mid-sentence (screener still talking, VM
    # still playing its greeting, or human still saying their first "Hello?")
    classify_start = time.time()
    CLASSIFY_HARD_TIMEOUT = 25.0  # absolute ceiling in case silence never comes

    while True:
        if _dom_has(driver, _SEL_ENDED): return "no_answer"

        elapsed = time.time() - classify_start
        cs      = _get_call_state(driver)

        if cs and cs.get("classifying"):
            consec_sil  = cs.get("consecutiveSilenceMs", 0)
            speech_seen = cs.get("speechStarted", False)
            max_burst   = cs.get("maxBurstMs", 0)

            # VM monologue detected inline — no need to wait for full silence
            if max_burst >= _VM_MAXBURST_MS:
                dom_confirm = _dom_classify(driver)
                result = "voicemail" if dom_confirm == "voicemail" else "voicemail"
                log_msg(
                    f"[vm] Result: voicemail (burst={max_burst}ms ring={ring_ms}ms)",
                    "info"
                )
                return result

            # Silence achieved after speech — ready to classify
            if speech_seen and consec_sil >= _SILENCE_TO_CLASSIFY:
                result = _classify_audio_on_silence(cs)
                if result == "dom_fallback":
                    result = _dom_classify(driver)
                log_msg(
                    f"[vm] Result: {result} "
                    f"(speech={cs.get('totalSpeechMs',0)}ms "
                    f"firstBurst={cs.get('firstBurstMs',0)}ms "
                    f"maxBurst={max_burst}ms ring={ring_ms}ms)",
                    "info"
                )
                # Always confirm human/screening against DOM
                if result in ("human", "screening"):
                    dom = _dom_classify(driver)
                    if dom == "voicemail":
                        log_msg(
                            f"[vm] Audio={result} but DOM=voicemail — upgrading",
                            "info"
                        )
                        return "voicemail"
                return result

        # Hard timeout — nobody spoke or silence never came
        if elapsed >= CLASSIFY_HARD_TIMEOUT:
            dom = _dom_classify(driver)
            log_msg(f"[vm] Classify timeout → {dom} (ring={ring_ms}ms)", "warning")
            return dom

        time.sleep(0.08)


def classify_call(driver, classify_seconds=8):
    """
    Silence-triggered classifier. Waits for the first audio silence after
    speech starts, then classifies using firstBurstMs + DOM phrase check.
    classify_seconds is kept as param for API compat but is now the hard timeout.
    Returns 'voicemail', 'screening', 'human', or 'unknown'.
    """
    try:
        driver.execute_script("if(window._gvStartClassify) window._gvStartClassify();")
    except Exception:
        return "unknown"

    deadline  = time.time() + max(classify_seconds, 25)
    _last_log = 0.0

    while time.time() < deadline:
        try:
            cs = driver.execute_script("return window._gvCallState;")
        except Exception:
            return "unknown"

        if not cs:
            time.sleep(0.1)
            continue

        total_ms       = cs.get("totalSpeechMs", 0)
        consec_sil     = cs.get("consecutiveSilenceMs", 0)
        max_burst      = cs.get("maxBurstMs", 0)
        phrases        = cs.get("phraseCount", 0)
        speech_started = cs.get("speechStarted", False)
        first_burst    = cs.get("firstBurstMs", 0)

        now = time.time()
        if now - _last_log >= 2.0:
            debug_msg(
                f"classify: totalSpeech={total_ms}ms consecutiveSilence={consec_sil}ms "
                f"firstBurst={first_burst}ms maxBurst={max_burst}ms phrases={phrases}"
            )
            _last_log = now

        # VM monologue — long burst, classify immediately without waiting for silence
        if max_burst >= _VM_MAXBURST_MS:
            debug_msg(f"classify: voicemail (burst={max_burst}ms)")
            return "voicemail"

        # Silence-triggered: speech started and went quiet — now safe to classify
        if speech_started and consec_sil >= _SILENCE_TO_CLASSIFY:
            result = _classify_audio_on_silence(cs)
            if result == "dom_fallback":
                result = _dom_classify(driver)
            if result in ("human", "screening"):
                dom = _dom_classify(driver)
                if dom == "voicemail":
                    debug_msg(f"classify: audio={result} DOM=voicemail -> voicemail")
                    return "voicemail"
                if dom == "screening":
                    result = "screening"
            debug_msg(
                f"classify: {result} "
                f"(firstBurst={first_burst}ms maxBurst={max_burst}ms "
                f"speech={total_ms}ms phrases={phrases})"
            )
            return result

        time.sleep(0.08)

    # Hard timeout fallback
    try:
        cs = driver.execute_script("return window._gvCallState;")
    except Exception:
        cs = {}
    if cs:
        result = _classify_audio_on_silence(cs)
        if result == "dom_fallback":
            result = _dom_classify(driver)
        debug_msg(f"classify: timeout fallback -> {result}")
        return result
    return "unknown"


def _poll_for_dtmf(driver, timeout_s, number, account_email, contact_label=None, target_key=None):
    """
    Poll for a DTMF keypress for up to timeout_s seconds.
    FIX #60: Only triggers on the configured dtmf_key_to_detect, not any key.
    contact_label threads into the press1 Telegram notification.
    target_key: override the key to listen for (used for level 2).
    Returns the key string or None on timeout.
    """
    target_key = str(target_key if target_key is not None else settings.get("dtmf_key_to_detect", "1"))
    deadline   = time.time() + timeout_s
    _start_dtmf_listen(driver)
    while time.time() < deadline:
        try:
            key = driver.execute_script(
                "return (window._gvDTMF && window._gvDTMF.detected) ? window._gvDTMF.detected : null;"
            )
        except Exception:
            break
        if key is not None:
            if key == target_key:
                log_msg(f"[dtmf] Key '{key}' detected for {number}", "success")
                tg_notify_dtmf(number, key, account_email, contact_label=contact_label)
                return key
            else:
                debug_msg(f"[dtmf] Ignored key '{key}' (target='{target_key}')")
                try:
                    driver.execute_script("window._gvDTMF.detected = null;")
                except Exception:
                    pass
        time.sleep(0.15)
    return None


# ── Campaign worker ───────────────────────────────────────────────────────────
def campaign_worker():
    """Top-level campaign thread — spawns per-account worker threads."""
    import queue as _queue
    numbers  = list(state["numbers"])
    accounts = settings.get("accounts", [])
    if not numbers:
        log_msg("No numbers loaded.", "error"); state["running"] = False; return
    if not accounts or not accounts[0].get("email"):
        log_msg("No accounts configured.", "error"); state["running"] = False; return

    num_queue = _queue.Queue()
    for n in numbers:
        num_queue.put(n)

    concurrent = max(1, min(int(settings.get("concurrent_limit", 1)), len(accounts)))
    lock       = threading.Lock()

    log_msg(f"Campaign started — {len(numbers)} contacts, {concurrent} worker(s)", "success")
    tg_notify(f"\U0001f7e2 Campaign started — {len(numbers)} contacts")

    threads = []
    for i in range(concurrent):
        account = accounts[i % len(accounts)]
        t = threading.Thread(
            target=_account_worker,
            args=(account, num_queue, lock),
            daemon=True
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    state["running"] = False
    total = state["completed"] + state["failed"]
    log_msg(f"Campaign finished — {state['completed']}/{total} completed", "success")
    tg_notify(f"\U0001f3c1 Campaign finished — {state['completed']}/{total} completed")


def _account_worker(account, num_queue, lock):
    """Per-account worker thread — dials numbers from the shared queue."""
    email    = account.get("email", "")
    password = account.get("password", "")
    profile  = profile_name_for_account(account)

    headless         = settings.get("headless", False)
    delay            = int(settings.get("delay_between_calls", 45))
    vm_enabled       = settings.get("vm_detection_enabled", False)
    vm_action        = settings.get("vm_action", "hangup")
    screen_hangup    = settings.get("screen_hangup_enabled", False)
    screen_action    = settings.get("screen_hangup_action", "hangup")
    dtmf_enabled     = settings.get("dtmf_enabled", False)
    dtmf_timeout     = int(settings.get("dtmf_timeout", 20))
    audio_initial    = settings.get("audio_initial", "")
    audio_screen     = settings.get("audio_screen_bypass", "")
    audio_press1     = settings.get("audio_press1", "")

    dtmf_level2_enabled = settings.get("dtmf_level2_enabled", False)
    dtmf_level2_key     = str(settings.get("dtmf_level2_key", "1"))
    audio_level2        = settings.get("audio_level2", "")

    audio_initial = os.path.join(AUDIO_D, audio_initial) if audio_initial else ""
    audio_screen  = os.path.join(AUDIO_D, audio_screen)  if audio_screen  else ""
    audio_press1  = os.path.join(AUDIO_D, audio_press1)  if audio_press1  else ""
    audio_level2  = os.path.join(AUDIO_D, audio_level2)  if audio_level2  else ""

    driver = None
    try:
        driver = get_driver(profile, headless=headless)
        with _login_status_lock:
            state["login_status"][email] = "logging_in"
        logged_in = gv_login(driver, account)
        if not logged_in:
            log_msg(f"[{email}] Login failed — skipping account", "error")
            with _login_status_lock:
                state["login_status"][email] = "failed"
            return
        with _login_status_lock:
            state["login_status"][email] = "logged_in"

        account_index = 0
        all_accounts  = settings.get("accounts", [])
        for idx, acc in enumerate(all_accounts):
            if acc.get("email") == email:
                account_index = idx
                break

        if not ensure_voice_ready(driver, account_index=account_index):
            is_banned, ban_reason = _detect_ban(driver)
            if is_banned:
                log_msg(f"[{email}] \u26d4 BAN DETECTED: {ban_reason}", "error")
                tg_notify(f"\u26d4 BAN DETECTED\nAccount: {email}\nReason: {ban_reason}")
                with _login_status_lock:
                    state["login_status"][email] = "banned"
            else:
                log_msg(f"[{email}] Voice page not ready — skipping account", "error")
            return

        while not state.get("_stop"):
            while state.get("paused") and not state.get("_stop"):
                time.sleep(0.5)
            if state.get("_stop"):
                break

            with lock:
                if num_queue.empty():
                    break
                entry = num_queue.get()
                # FIX: unpack contact entry
                num, contact_label = _unpack_entry(entry)
                state["current_number"]  = num
                state["current_account"] = email

            log_msg(f"[{email}] \u2192 Dialing {num} [{contact_label}] ({state['completed']+state['failed']+1}/{state['total']})", "info")
            debug_msg(f"worker={email} number={num} screen_hangup={screen_hangup} dtmf={dtmf_enabled} headless={settings.get('headless')}")
            ok = make_call(driver, num, _account=account)

            if not ok:
                with lock:
                    state["failed"] += 1
                log_msg(f"[{email}] \u2717 Failed to connect: {num}", "error")
                time.sleep(delay)
                continue

            dtmf_key = None

            if screen_hangup:
                time.sleep(3)
                verdict = classify_call(driver)
                debug_msg(f"screen verdict={verdict} for {num}")

                if verdict in ("voicemail", "screening"):
                    if verdict == "voicemail":
                        log_msg(f"[{email}] Screen: voicemail — hanging up", "info")
                        hang_up(driver)
                        with lock:
                            state["vm"] = state.get("vm", 0) + 1
                        time.sleep(delay)
                        continue

                    elif verdict == "screening":
                        log_msg(f"[{email}] Screen: screener detected — waiting for silence before bypass", "info")
                        # CRITICAL: wait for screener to fully stop talking before
                        # playing bypass audio. Screener monologue is 4-15s.
                        if screen_action == "play_audio" and audio_screen:
                            ok = _wait_for_silence(driver, silence_needed_ms=_SILENCE_AFTER_AUDIO,
                                                   timeout_s=35, label="screener-bypass ")
                            if not ok or _dom_has(driver, _SEL_ENDED):
                                hang_up(driver)
                                time.sleep(delay)
                                continue
                            play_audio_in_tab(driver, audio_screen)
                            # Post-screen: classify what happens after bypass plays
                            post_verdict = _classify_post_screen(driver)
                            log_msg(f"[{email}] Post-screen: {post_verdict} for {num}", "info")
                            if post_verdict in ("no_answer", "voicemail"):
                                hang_up(driver)
                                if post_verdict == "voicemail":
                                    with lock:
                                        state["vm"] = state.get("vm", 0) + 1
                                time.sleep(delay)
                                continue
                            # post_verdict == "human" → fall through to play initial audio
                        elif screen_action == "hangup":
                            hang_up(driver)
                            time.sleep(delay)
                            continue

            if dtmf_enabled and audio_initial:
                if screen_hangup and settings.get("screen_calls_enabled"):
                    pass
                else:
                    dtmf_key = play_audio_in_tab(
                        driver, audio_initial,
                        block=True,
                        dtmf_interrupt=True,
                        press1_filepath=audio_press1 if audio_press1 else None,
                        number=num,
                        account_email=email,
                        contact_label=contact_label
                    )

                    if dtmf_key is None:
                        # No DTMF during audio — start polling
                        _start_dtmf_listen(driver)
                        dtmf_key = _poll_for_dtmf(driver, dtmf_timeout, num, email, contact_label=contact_label)
                        _stop_dtmf_listen(driver)

                    if dtmf_key:
                        log_msg(f"[{email}] Press1 confirmed: {num} [{contact_label}]", "success")
                        if audio_press1 and os.path.exists(audio_press1):
                            log_msg(f"[audio] Playing press1 audio after DTMF interrupt", "info")
                            play_audio_in_tab(driver, audio_press1,
                                             number=num, account_email=email,
                                             contact_label=contact_label)

                        # Level 2: poll for a second DTMF key after press1 audio
                        if dtmf_level2_enabled and audio_level2 and os.path.exists(audio_level2):
                            log_msg(f"[{email}] Level 2 — listening for press '{dtmf_level2_key}'", "info")
                            _start_dtmf_listen(driver)
                            dtmf_key2 = _poll_for_dtmf(
                                driver, dtmf_timeout, num, email,
                                contact_label=contact_label,
                                target_key=dtmf_level2_key
                            )
                            _stop_dtmf_listen(driver)
                            if dtmf_key2:
                                log_msg(f"[{email}] Level 2 press confirmed: {num} [{contact_label}]", "success")
                                tg_notify_dtmf_level2(num, dtmf_key2, email, contact_label=contact_label)
                                play_audio_in_tab(driver, audio_level2,
                                                  number=num, account_email=email,
                                                  contact_label=contact_label)
                        time.sleep(1)

            hang_up(driver)

            with lock:
                state["completed"] += 1
                pct = state["completed"] / state["total"] * 100
            log_msg(f"[{email}] \u2713 Done {num} [{contact_label}] \u2014 {state['completed']}/{state['total']} ({pct:.1f}%)", "success")

            if state.get("_stop"):
                break
            time.sleep(delay)

    except Exception as e:
        log_msg(f"[{email}] Worker crash: {e}", "error")
        import traceback; log_msg(traceback.format_exc(), "error")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
        with _login_status_lock:
            state["login_status"][email] = "logged_out"


# ── Flask app ─────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
CORS(flask_app)


@flask_app.route("/api/state")
def api_state():
    with _login_status_lock:
        login_status_copy = dict(state.get("login_status", {}))
    return jsonify({
        "running":         state["running"],
        "paused":          state.get("paused", False),
        "login_status":    login_status_copy,
        "numbers":         len(state["numbers"]),
        "completed":       state["completed"],
        "failed":          state["failed"],
        "total":           state["total"],
        "current_number":  state["current_number"],
        "current_account": state["current_account"],
        "vm":              state.get("vm", 0),
        "log":             state["log"][-100:],
    })



@flask_app.route("/api/telegram/send", methods=["POST"])
def api_telegram_send():
    data = request.get_json(silent=True) or {}
    msg  = data.get("message", "").strip()
    if not msg:
        return jsonify({"message": "No message provided"}), 400
    tg_notify(msg)
    return jsonify({"message": "Sent"})


@flask_app.route("/api/start", methods=["POST"])
def api_start():
    if state["running"]:
        return jsonify({"message": "Already running"}), 400
    state.update({"running": True, "_stop": False, "paused": False,
                  "completed": 0, "failed": 0})
    threading.Thread(target=campaign_worker, daemon=True).start()
    return jsonify({"message": "Campaign started"})


@flask_app.route("/api/stop", methods=["POST"])
def api_stop():
    state["_stop"]  = True
    state["paused"] = False
    return jsonify({"message": "Stop signal sent"})


@flask_app.route("/api/pause", methods=["POST"])
def api_pause():
    if not state["running"]:
        return jsonify({"message": "Not running"}), 400
    state["paused"] = not state.get("paused", False)
    return jsonify({"message": "Paused" if state["paused"] else "Resumed",
                    "paused": state["paused"]})


@flask_app.route("/api/numbers/load", methods=["POST"])
def api_load_numbers():
    """FIX: accepts full contact lines — parses phone, stores full line as label."""
    _req_data = request.get_json(silent=True) or {}
    raw = [n.strip() for n in _req_data.get("numbers", []) if n.strip()]
    entries, skipped = _parse_contact_lines(raw)
    state.update({"numbers": entries, "total": len(entries), "completed": 0, "failed": 0})
    with open(NUMBERS_F, "w") as f: f.write("\n".join(entries))
    msg = f"Loaded {len(entries)} contact(s)"
    if skipped:
        msg += f" ({skipped} skipped \u2014 no valid number)"
    return jsonify({"message": msg})


@flask_app.route("/api/numbers/loadfile", methods=["POST"])
def api_load_numbers_from_file():
    load_numbers_from_file()
    return jsonify({"message": f"Loaded {state['total']} contact(s) from file"})


@flask_app.route("/api/numbers/clear", methods=["POST"])
def api_clear_numbers():
    state.update({"numbers": [], "total": 0, "completed": 0, "failed": 0})
    _clear_numbers_file()
    return jsonify({"message": "Numbers cleared"})


@flask_app.route("/api/settings", methods=["GET"])
def api_get_settings():
    # FIX #66: redact passwords in GET response
    safe = dict(settings)
    safe_accounts = []
    for a in safe.get("accounts", []):
        ac = dict(a)
        if ac.get("password"):
            ac["password"] = "***"
        safe_accounts.append(ac)
    safe["accounts"] = safe_accounts
    return jsonify(safe)



@flask_app.route("/api/settings", methods=["POST"])
def api_save_settings():
    global settings
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"message": "Invalid settings payload"}), 400

        payload_accounts = payload.get("accounts", [])
        if not isinstance(payload_accounts, list):
            payload_accounts = []

        existing_accounts = {a.get("email"): a for a in settings.get("accounts", [])}

        normalized_accounts = []
        for i, a in enumerate(payload_accounts):
            if not isinstance(a, dict):
                continue
            email = str(a.get("email", "") or "").strip()
            incoming_pw = str(a.get("password", "") or "")
            if incoming_pw == "***" and email in existing_accounts:
                password = existing_accounts[email].get("password", "")
            else:
                password = incoming_pw

            existing_profile = existing_accounts.get(email, {}).get("profile", "")
            incoming_profile = str(a.get("profile", "") or "").strip()
            if incoming_profile:
                profile = incoming_profile
            elif existing_profile:
                profile = existing_profile
            else:
                profile = ""  # profile_name_for_account() derives from email

            normalized_accounts.append({
                "email":   email,
                "password": password,
                "profile": profile,
            })

        concurrent = int(payload.get("concurrent_limit", 1))
        if concurrent > 10:
            log_msg(f"[settings] concurrent_limit clamped from {concurrent} to 10", "warning")
            concurrent = 10
        payload["concurrent_limit"] = concurrent

        settings = _deep_merge(DEFAULT_SETTINGS, {**payload, "accounts": normalized_accounts})
        save_settings_to_disk(settings)
        log_msg(f"[settings] Saved {len(normalized_accounts)} account(s)", "success")
        return jsonify({"message": "Settings saved!"})
    except Exception as e:
        log_msg(f"[settings] Save failed: {e}", "error")
        return jsonify({"message": f"Save failed: {e}"}), 500


@flask_app.route("/api/audio/list")
def api_list_audio():
    files = []
    if os.path.isdir(AUDIO_D):
        for fn in os.listdir(AUDIO_D):
            if fn.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
                files.append(fn)
    return jsonify({"files": sorted(files)})


@flask_app.route("/api/audio/upload", methods=["POST"])
def api_upload_audio():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    safe_name = os.path.basename(f.filename)
    dest = os.path.join(AUDIO_D, safe_name)
    f.save(dest)
    return jsonify({"message": f"Uploaded {safe_name}"})


@flask_app.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    cleared = clear_cache()
    return jsonify({"message": f"Cleared {cleared} cache directories"})


@flask_app.route("/api/login", methods=["POST"])
@flask_app.route("/api/login/<path:profile_slug>", methods=["POST"])
def api_login(profile_slug=None):
    """FIX #56: serialize login_status writes with _login_status_lock."""
    data    = request.get_json(silent=True) or {}
    email   = data.get("email", "")
    password = data.get("password", "")
    if not email:
        return jsonify({"error": "email required"}), 400

    account  = {"email": email, "password": password}
    profile  = profile_name_for_account(account)
    headless = settings.get("headless", False)

    def _do_login():
        with _login_status_lock:
            state["login_status"][email] = "logging_in"
        try:
            driver = get_driver(profile, headless=headless)
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
        snapshot = dict(state["login_status"])
    return jsonify(snapshot)

@flask_app.route("/api/recover", methods=["POST"])
def api_recover():
    """Open browser for account, run ban detection, attempt re-login.
    Used by the UI \'Recover\' button on the accounts page."""
    data     = request.get_json(silent=True) or {}
    email    = data.get("email", "")
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
            _drivers[email] = driver

            # Navigate to GV and check for ban first
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

            # Attempt login
            ok = gv_login(driver, account)

            # Post-login ban check
            if ok:
                is_banned, reason = _detect_ban(driver)
                if is_banned:
                    log_msg(f"[recover] \u26d4 POST-LOGIN BAN for {email}: {reason}", "error")
                    tg_notify(f"\u26d4 BAN DETECTED\nAccount: {email}\nReason: {reason}")
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

    with _login_status_lock:
        return jsonify(dict(state.get("login_status", {})))



@flask_app.route("/api/test_call", methods=["POST"])
def api_test_call():
    number = (request.get_json(silent=True) or {}).get("number", "")
    if not number: return jsonify({"message": "No number provided"})
    if state["running"]: return jsonify({"message": "Campaign already running"})
    def do_test():
        s = load_settings(); accounts = s.get("accounts", [])
        if not accounts: log_msg("No accounts for test call", "error"); return
        account = accounts[0]
        log_msg(f"TEST CALL to {number}", "warning")
        try:
            d = get_or_create_driver(account)
            ok = make_call(d, number, _account=account)
            if ok:
                call_type = getattr(d, "_last_call_type", "unknown")
                audio_bypass = s.get("audio_screen_bypass", "")
                audio_initial = s.get("audio_initial", "")
                # FIX: same screening fix as campaign worker — wait for human to accept after bypass
                if call_type == "screening" and s.get("screen_hangup_enabled") and s.get("screen_hangup_action") == "play_audio":
                    bypass_file = audio_bypass if audio_bypass else audio_initial
                    if bypass_file:
                        log_msg(f"[test] Screen call — playing bypass audio: {number}", "warning")
                        play_audio_in_tab(d, bypass_file)
                        log_msg(f"[test] Waiting for human to accept screen call...", "info")
                        try:
                            post_type = _classify_post_screen(d)
                            d._last_call_type = post_type
                            log_msg(f"[test] Post-screen result: {post_type}", "info")
                            if post_type in ("no_answer", "voicemail"):
                                hang_up(d); return
                            call_type = post_type
                        except Exception as _se:
                            log_msg(f"[test] Post-screen wait error: {_se}", "warning")
                key = None
                if audio_initial:  # always play initial after screening is passed or if not screening
                    if s.get("dtmf_enabled"):
                        key = play_audio_in_tab(
                            d, audio_initial,
                            block=True,
                            dtmf_interrupt=True,
                            press1_filepath=None,  # FIX: caller handles press1, not play_audio_in_tab
                            number=number,
                            account_email=account["email"]
                        )
                    else:
                        play_audio_in_tab(d, audio_initial)
                if key:
                    # Small settle delay: let browser audio pipeline clear after stopping initial audio
                    time.sleep(0.4)
                    # Mid-audio DTMF: play press1 fully here before hanging up
                    if s.get("audio_press1") and os.path.exists(s["audio_press1"]):
                        log_msg(f"[test] Playing press1 goodbye audio", "info")
                        play_audio_in_tab(d, s["audio_press1"], block=True)
                        log_msg(f"[test] Press1 audio done", "info")
                elif s.get("dtmf_enabled"):
                    _start_dtmf_listen(d)
                    key = _poll_for_dtmf(d, int(s.get("dtmf_timeout", 20)), number, account["email"])
                    _stop_dtmf_listen(d)
                    if key and s.get("audio_press1") and os.path.exists(s["audio_press1"]):
                        log_msg(f"[test] Playing press1 goodbye audio (post-audio poll)", "info")
                        play_audio_in_tab(d, s["audio_press1"], block=True)
                        log_msg(f"[test] Press1 audio done", "info")
                hang_up(d)
            log_msg(f"Test call to {number} complete", "success")
        except Exception as e:
            log_msg(f"Test call error: {e}", "error")
    threading.Thread(target=do_test, daemon=True).start()
    return jsonify({"message": f"Test call started to {number}"})


@flask_app.route("/api/audio/play", methods=["POST"])
def api_audio_play():
    data = request.get_json(silent=True) or {}
    filepath  = data.get("filepath", "")
    acct_key  = data.get("account_key", "")
    block     = data.get("block", True)

    if not filepath:
        return jsonify({"message": "No filepath provided"}), 400
    if not os.path.exists(filepath):
        return jsonify({"message": f"File not found: {filepath}"}), 404

    driver = _drivers.get(acct_key) if acct_key else None
    if driver is None and _drivers:
        driver = next(iter(_drivers.values()))
    if driver is None:
        return jsonify({"message": "No active browser session"}), 503

    audio_finished = [False]

    def _do_play():
        play_audio_in_tab(driver, filepath, block=block)
        audio_finished[0] = True

    if block:
        t = threading.Thread(target=_do_play, daemon=True)
        t.start()
        t.join(timeout=65)
        # FIX #64: verify audio actually finished before returning 200
        if not audio_finished[0]:
            return jsonify({"message": "Audio timed out — may not have finished"}), 504
        return jsonify({"message": "Done"})
    else:
        threading.Thread(target=_do_play, daemon=True).start()
        return jsonify({"message": "Playing"})

@flask_app.route("/api/audio/status", methods=["GET"])
def api_audio_status():
    acct_key = request.args.get("account_key", "")
    driver = _drivers.get(acct_key) if acct_key else None
    if driver is None and _drivers:
        driver = next(iter(_drivers.values()))
    if driver is None:
        return jsonify({"playing": False, "error": "No active session"})
    try:
        playing = driver.execute_script("return window._gvAudioPlaying === true;")
        done    = driver.execute_script("return window._gvAudioDone === true;")
        return jsonify({"playing": playing, "done": done})
    except Exception as e:
        return jsonify({"playing": False, "error": str(e)})


@flask_app.route("/api/kill_browsers", methods=["POST"])
def api_kill_browsers():
    # FIX #59: removed redundant per-request import of subprocess/sys
    killed = []
    targets = ["chromium", "chromium-browser", "chrome", "chromedriver"]
    try:
        if os.name == "nt":
            for name in targets:
                r = subprocess.run(
                    ["taskkill", "/F", "/IM", f"{name}.exe", "/T"],
                    capture_output=True, text=True
                )
                if "SUCCESS" in r.stdout or "success" in r.stdout.lower():
                    killed.append(name)
        else:
            for name in targets:
                r = subprocess.run(["pkill", "-9", "-f", name], capture_output=True)
                if r.returncode == 0:
                    killed.append(name)
    except Exception as e:
        log_msg(f"[kill] Error during force-kill: {e}", "error")

    for d in list(_drivers.values()):
        try: d.quit()
        except Exception: pass
    _drivers.clear()

    msg = f"Force-killed: {', '.join(killed) if killed else 'none found'} — driver registry cleared"
    log_msg(f"[kill] {msg}", "warning")
    return jsonify({"message": msg, "killed": killed})

@flask_app.route("/api/close_browsers", methods=["POST"])
def api_close_browsers():
    release_drivers(); log_msg("All browser windows closed.", "info")
    return jsonify({"message": "Browsers closed"})

@flask_app.route("/api/clearcache", methods=["POST"])
def api_clearcache():
    n = clear_cache()
    return jsonify({"message": f"Cache cleared ({n} dirs)"})




def run_telegram_bot():
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters as tg_filters
    import asyncio

    bot_token = settings.get("telegram_bot_token", "")
    if not bot_token:
        log_msg("[tg] No bot token — Telegram bot not started", "warning"); return

    # pending-paste state: chat_id -> True when bot is waiting for the user to paste contacts
    _tg_pending_paste: dict = {}

    async def _process_contact_paste(raw_lines, update):
        """Parse raw contact lines, add new entries to state, confirm to Telegram."""
        existing_nums = {_unpack_entry(e)[0] for e in state["numbers"]}
        entries, skipped = _parse_contact_lines(raw_lines)
        new_entries = [e for e in entries if _unpack_entry(e)[0] not in existing_nums]
        state["numbers"].extend(new_entries)
        state["total"] = len(state["numbers"])
        with open(NUMBERS_F, "w") as f:
            f.write("\n".join(state["numbers"]))
        msg = f"\u2705 Added {len(new_entries)} contact(s). Total: {state['total']}"
        if skipped:
            msg += f"\n\u26a0\ufe0f {skipped} line(s) skipped (no valid number found)"
        debug_msg(f"telegram contacts added={len(new_entries)} skipped={skipped} total={state['total']}")
        await update.message.reply_text(msg)

    async def start_cmd(u, c):
        msg = (
            "\U0001f916 Synergy 1.0 online!\n\n"
            "Commands:\n"
            "/start \u2014 show this message\n"
            "/call \u2014 start campaign\n"
            "/stop \u2014 stop campaign\n"
            "/pause \u2014 pause / resume\n"
            "/status \u2014 current stats\n"
            "/addnumbers \u2014 add contacts (inline or paste)\n"
            "/clearnumbers \u2014 clear queue\n"
            "/lognumbers \u2014 list current queue"
        )
        await u.message.reply_text(msg)

    async def call_cmd(u, c):
        if state["running"]:
            await u.message.reply_text("Already running!"); return
        state.update({"running": True, "_stop": False, "paused": False, "completed": 0, "failed": 0})
        threading.Thread(target=campaign_worker, daemon=True).start()
        debug_msg("telegram /call invoked")
        await u.message.reply_text(f"Campaign started \u2014 {state['total']} numbers.")

    async def stop_cmd(u, c):
        state["_stop"] = True; state["paused"] = False
        debug_msg("telegram /stop invoked")
        await u.message.reply_text("\U0001f6d1 Stop signal sent.")

    async def pause_cmd(u, c):
        if not state["running"]:
            await u.message.reply_text("Not running."); return
        state["paused"] = not state.get("paused", False)
        debug_msg(f"telegram /pause invoked -> paused={state['paused']}")
        await u.message.reply_text("\u23f8 Paused." if state["paused"] else "\u25b6\ufe0f Resumed.")

    async def status_cmd(u, c):
        pct = (state["completed"] / state["total"] * 100) if state["total"] else 0
        status = "Running" if state["running"] else "Stopped"
        if state.get("paused"): status += " (paused)"
        msg = (f"{status}\nTotal: {state['total']}  Done: {state['completed']}  "
               f"Failed: {state['failed']}\n{pct:.1f}%")
        await u.message.reply_text(msg)

    async def addnumbers_cmd(u, c):
        """
        /addnumbers [contact lines...]
        - With inline args: parse + add immediately.
        - With no args: prompt the user to paste contacts; on_plain_message handles the reply.
        Accepts any format: full CRM export lines, bare numbers, +1 prefix, etc.
        """
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
        # Inline: treat entire args string as newline/comma-separated contact lines
        raw_text = " ".join(c.args)
        raw_lines = [n.strip() for n in raw_text.splitlines() if n.strip()]
        if not raw_lines:
            raw_lines = [n.strip() for n in raw_text.split(",") if n.strip()]
        await _process_contact_paste(raw_lines, u)

    async def on_plain_message(u, c):
        """Catches plain-text messages \u2014 used for the two-step /addnumbers paste flow."""
        chat_id = u.effective_chat.id
        if not _tg_pending_paste.pop(chat_id, False):
            return  # not waiting for a paste \u2014 ignore
        text = u.message.text or ""
        raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
        await _process_contact_paste(raw_lines, u)

    async def clearnumbers_cmd(u, c):
        state.update({"numbers": [], "total": 0, "completed": 0, "failed": 0})
        _clear_numbers_file()
        debug_msg("telegram /clearnumbers invoked")
        await u.message.reply_text("\U0001f5d1 Numbers queue cleared.")

    async def lognumbers_cmd(u, c):
        entries = state.get("numbers", [])
        if not entries:
            await u.message.reply_text("Queue is empty."); return
        lines = []
        for e in entries[:30]:
            _, label = _unpack_entry(e)
            lines.append(label)
        preview = "\n".join(lines)
        if len(entries) > 30:
            preview += f"\n...and {len(entries)-30} more"
        await u.message.reply_text(f"\U0001f4cb Queue ({len(entries)} contact(s)):\n{preview}")

    async def main():
        app = (ApplicationBuilder().token(bot_token)
               .connect_timeout(10).read_timeout(15).write_timeout(15)
               .pool_timeout(10).build())
        app.add_handler(CommandHandler("start",        start_cmd))
        app.add_handler(CommandHandler("call",         call_cmd))
        app.add_handler(CommandHandler("stop",         stop_cmd))
        app.add_handler(CommandHandler("pause",        pause_cmd))
        app.add_handler(CommandHandler("status",       status_cmd))
        app.add_handler(CommandHandler("addnumbers",   addnumbers_cmd))
        app.add_handler(CommandHandler("clearnumbers", clearnumbers_cmd))
        app.add_handler(CommandHandler("lognumbers",   lognumbers_cmd))
        # FIX: register plain-message handler so two-step /addnumbers paste is caught
        app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, on_plain_message))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    import time as _time

    # Guard: only one bot thread should ever run. If token is blank, skip entirely.
    token_check = settings.get("telegram_bot_token", "").strip()
    if not token_check:
        return

    # Delete any existing webhook + drop pending updates before starting polling.
    # This clears stale sessions from previous runs without needing a full restart.
    try:
        import urllib.request as _ur
        _ur.urlopen(
            f"https://api.telegram.org/bot{token_check}/deleteWebhook"
            f"?drop_pending_updates=true", timeout=8
        ).read()
    except Exception:
        pass

    # Small startup delay so Telegram's server expires any lingering long-poll
    # from the previous process (Telegram enforces ~1-2s before new getUpdates).
    _time.sleep(4)

    try:
        asyncio.run(main())
    except Exception as e:
        logging.warning(f"Telegram bot error: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # FIX #47: restore numbers from previous session on startup
    load_numbers_from_file()

    tg_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    tg_thread.start()

    flask_app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
