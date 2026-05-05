# DEFAULT_BASE_BUILD: fixed46_press1_tg_filter_vm_fastfail_contacts_parser
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
    phraseCount: 0, inSpeech: false
  };

  window._gvStartClassify = function() {
    var s = window._gvCallState;
    s.totalSpeechMs = 0; s.silenceMs = 0;
    s.burstMs = 0; s.maxBurstMs = 0;
    s.phraseCount = 0; s.inSpeech = false;
    s.classifying = true;
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
          s.totalSpeechMs += TICK; s.burstMs += TICK;
          if (s.burstMs > s.maxBurstMs) s.maxBurstMs = s.burstMs;
          if (!s.inSpeech) { s.inSpeech = true; s.phraseCount++; }
        } else {
          s.silenceMs += TICK;
          if (s.inSpeech) s.inSpeech = false;
          s.burstMs = 0;
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

def _inject_audio_hook(driver):
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _WEBAUDIO_HOOK})
    except Exception as e:
        log_msg(f"[audio hook] CDP inject failed (non-fatal): {e}", "warning")

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


def classify_call(driver, classify_seconds=8):
    """Run the energy classifier for classify_seconds and return the verdict.
    FIX #62: Raised vm_action default to 'hangup' so the existing vm_hangup path fires.
    Returns 'voicemail', 'human', or 'unknown'.
    """
    try:
        driver.execute_script("if(window._gvStartClassify) window._gvStartClassify();")
    except Exception:
        return "unknown"

    time.sleep(classify_seconds)

    try:
        s = driver.execute_script("return window._gvCallState;")
    except Exception:
        return "unknown"

    if not s:
        return "unknown"

    total_ms  = s.get("totalSpeechMs", 0)
    silence   = s.get("silenceMs", 0)
    phrases   = s.get("phraseCount", 0)
    max_burst = s.get("maxBurstMs", 0)

    debug_msg(f"classify: totalSpeech={total_ms}ms silence={silence}ms phrases={phrases} maxBurst={max_burst}ms")

    if total_ms > 4000 and max_burst > 2500 and phrases <= 2:
        return "voicemail"
    if total_ms > 800 and phrases >= 2:
        return "human"
    if total_ms > 2000 and silence < 500:
        return "voicemail"
    return "unknown"


def _poll_for_dtmf(driver, timeout_s, number, account_email, contact_label=None):
    """
    Poll for a DTMF keypress for up to timeout_s seconds.
    FIX #60: Only triggers on the configured dtmf_key_to_detect, not any key.
    contact_label threads into the press1 Telegram notification.
    Returns the key string or None on timeout.
    """
    target_key = str(settings.get("dtmf_key_to_detect", "1"))
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

    audio_initial = os.path.join(AUDIO_D, audio_initial) if audio_initial else ""
    audio_screen  = os.path.join(AUDIO_D, audio_screen)  if audio_screen  else ""
    audio_press1  = os.path.join(AUDIO_D, audio_press1)  if audio_press1  else ""

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

            # FIX: unpack contact entry — dial_num for calling, contact_label for logging/notifications
            num, contact_label = _unpack_entry(entry)

            with lock:
                state["current_number"]  = num
                state["current_account"] = email

            log_msg(f"[{email}] \u2192 Dialing {num} [{contact_label}] ({state['completed']+state['failed']+1}/{state['total']})", "info")
            debug_msg(f"worker={email} number={num} screen_hangup={screen_hangup} dtmf={dtmf_enabled} headless={settings.get('headless')}")
            ok = make_call(driver, num, _account=account)

            if not ok:
                with lock:
                    state["failed"] += 1
                log_msg(f"[{email}] \u2717 Failed to connect: {num}", "error")
                time.sleep(max(5, delay // 3))
                continue

            dtmf_key = None

            if screen_hangup:
                time.sleep(3)
                verdict = classify_call(driver)
                debug_msg(f"screen verdict={verdict} for {num}")
                if verdict == "voicemail":
                    log_msg(f"[{email}] Screen: voicemail detected — {screen_action}", "info")
                    if screen_action == "play_audio" and audio_screen:
                        play_audio_in_tab(driver, audio_screen)
                    hang_up(driver)
                    with lock:
                        state["failed"] += 1
                    time.sleep(max(5, delay // 3))
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
        "log":             state["log"][-100:],
    })


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
    raw = [n.strip() for n in request.json.get("numbers", []) if n.strip()]
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
    return jsonify(settings)


@flask_app.route("/api/settings", methods=["POST"])
def api_save_settings():
    global settings
    new_settings = request.json
    settings = _deep_merge(DEFAULT_SETTINGS, new_settings)
    save_settings_to_disk(settings)
    return jsonify({"message": "Settings saved"})


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
def api_login():
    """FIX #56: serialize login_status writes with _login_status_lock."""
    data    = request.json or {}
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
        return jsonify(dict(state.get("login_status", {})))


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
        await app.updater.start_polling()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

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

    flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
