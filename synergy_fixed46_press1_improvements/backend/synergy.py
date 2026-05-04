# DEFAULT_BASE_BUILD: fixed46_press1_tg_filter_vm_fastfail
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

DEFAULT_SETTINGS = {
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
        with open(SETTINGS_F) as f:
            saved = json.load(f)
        return _deep_merge(DEFAULT_SETTINGS, saved)
    save_settings_to_disk(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)

def save_settings_to_disk(s):
    with open(SETTINGS_F, "w") as f:
        json.dump(s, f, indent=2)

settings = load_settings()

# FIX #23: login_status initialized at module level so /api/state never KeyErrors on cold start
# FIX #56: _login_status_lock protects all login_status reads/writes across route + login threads
_login_status_lock = threading.Lock()

state = {
    "running": False,
    "paused": False,
    "login_status": {}, "numbers": [], "completed": 0, "failed": 0,
    "total": 0, "current_number": "", "current_account": "",
    "log": [], "_stop": False,
}

# FIX #47: restore numbers from file on startup
def load_numbers_from_file():
    if os.path.exists(NUMBERS_F):
        with open(NUMBERS_F) as f:
            nums = [l.strip() for l in f if l.strip()]
        state.update({"numbers": nums, "total": len(nums), "completed": 0, "failed": 0})
        log_msg(f"Loaded {len(nums)} numbers from file.")

def log_msg(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    state["log"].append(entry)
    # FIX #67: trim on every append, not periodically
    if len(state["log"]) > 500:
        state["log"] = state["log"][-500:]
    logging.info(msg)
    # FIX #19: verbose logic was inverted — only send error TG notify when NOT in verbose mode
    if level == "error" and not settings.get("verbose_debug", False):
        tg_notify(f"\u26a0\ufe0f ERROR\n{msg}")

def debug_msg(msg):
    # FIX #19: only log debug messages when verbose_debug IS enabled
    if settings.get("verbose_debug", False):
        log_msg(f"[debug] {msg}", "info")

# Persistent session for Telegram
import requests as _tg_req
_tg_session = _tg_req.Session()
_tg_session.mount("https://", _tg_req.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))

def tg_notify(msg):
    token = settings.get("telegram_bot_token", "")
    uid   = settings.get("telegram_user_id", 0)
    if not token or not uid:
        return
    try:
        _tg_session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": uid, "text": msg}, timeout=5
        )
    except Exception:
        pass

def tg_notify_dtmf(number, key, account_email):
    token = settings.get("telegram_bot_token", "")
    uid   = settings.get("telegram_user_id", 0)
    if not token or not uid:
        return
    msg = (
        f"\U0001f7e2 PRESS {key} RECEIVED\n"
        f"Number: {number}\n"
        f"Account: {account_email}"
    )
    try:
        _tg_session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": uid, "text": msg}, timeout=5
        )
    except Exception:
        pass

_PLAY_AUDIO_JS = """
(async function(b64, mime) {
  try {
    // ensure outbound graph exists once per tab
    if (!window._gvOutCtx) {
      window._gvOutCtx = new (window.AudioContext || window.webkitAudioContext)();
      window._gvOutDest = window._gvOutCtx.createMediaStreamDestination();
      window._gvOutStream = window._gvOutDest.stream;
      // hook persistent outbound stream into any existing audio sender
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
      window._gvAudioDone = false;
      window._gvAudioPlaying = true;
      src.onended = function() {
        window._gvAudioDone = true;
        window._gvAudioPlaying = false;
      };
      src.start();
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
                       press1_filepath=None, number=None, account_email=None):
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
    # FIX #33: only allocate stop_poll inside dtmf_interrupt block
    stop_poll     = None

    # FIXED30_GUARDED_PATH: do not remove watcher-thread prompt interrupt logic
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
            # FIX #33: allocate stop_poll only when dtmf_interrupt=True
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
            for _ in range(600):  # up to 60s
                audio_done = False
                try:
                    audio_done = driver.execute_script("return window._gvAudioDone === true;")
                except Exception:
                    pass

                if dtmf_interrupt and dtmf_event.is_set():
                    detected_key = dtmf_result[0]
                    log_msg(f"[audio][dtmf] Interrupting prompt for key '{detected_key}'", "success")
                    if number and account_email:
                        tg_notify_dtmf(number, detected_key, account_email)
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

        # If we detected a key and press1 audio is provided, play it immediately
        if detected_key and press1_filepath and os.path.exists(press1_filepath):
            log_msg("[audio] Playing press1 audio after DTMF interrupt", "info")
            play_audio_in_tab(driver, press1_filepath, block=True)

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

  /* ── shared state ─────────────────────────────────── */
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

  /* ── DTMF state ───────────────────────────────────── */
  window._gvDTMF = { detected: null, detectedAt: 0, history: [], listening: false };
  window._gvStartDTMF = function() {
    window._gvDTMF.detected   = null;
    window._gvDTMF.detectedAt = 0;
    window._gvDTMF.history    = [];
    window._gvDTMF.listening  = true;
  };
  window._gvStopDTMF = function() { window._gvDTMF.listening = false; };

  /* ── DTMF frequency table ─────────────────────────── */
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

  /* ── attach to WebRTC audio track ────────────────── */
  function attachTrack(track) {
    if (track.kind !== 'audio') return;
    window._gvCallState.connected = true;
    try {
      var ctx = new AudioContext({ sampleRate: SR });
      var src = ctx.createMediaStreamSource(new MediaStream([track]));

      /* — energy analyser (VM / screening) — */
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

      /* — DTMF FFT analyser — */
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

  /* ── intercept RTCPeerConnection ─────────────────── */
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

# ── Driver helpers ────────────────────────────────────────────────────────────
# FIX #43: _make_driver_opts() was dead code duplicating make_options() — deleted.

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
    """Navigate to GV login page and auto-fill credentials.
    FIX #20: Stop navigating away from GV mid-auth once login page is confirmed loaded.
    The polling loop no longer calls driver.get() — it only reads the current URL.
    """
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
    # FIX #20: Poll current_url only — do NOT call driver.get() here.
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

# FIX #71: removed is_driver_alive() public alias — call _is_driver_alive() directly

# FIX #5/#44: Deleted _profile_name_for() and profile_name() — both were divergent
# duplicates with subtly different stripping logic, causing driver key mismatches.
# Use profile_name_for_account() everywhere.

def profile_name_for_account(account):
    explicit = account.get("profile", "").strip()
    if explicit and explicit not in ("profile_1", "profile1", ""):
        return explicit
    import re as _re2
    name = _re2.sub(r"[^a-zA-Z0-9]", "", account.get("email", "default").split("@")[0])
    return f"gvbot_{name}"


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
    """FIX #61: derive /u/{index}/calls from account_index instead of hardcoding /u/0/calls.
    FIX #30: check for error page indicators before returning True.
    """
    url = f"https://voice.google.com/u/{account_index}/calls"
    try:
        safe_get(driver, url)
        WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)
        # FIX #30: detect error pages
        try:
            body_text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
            page_src  = driver.page_source or ""
            combined  = body_text + page_src
            for indicator in _ERROR_PAGE_INDICATORS:
                if indicator in combined:
                    log_msg(f"[driver] Error page detected after navigation to {url}: {indicator}", "warning")
                    return False
        except Exception:
            pass
        return True
    except Exception as e:
        log_msg(f"[driver] Voice preflight failed: {e}", "warning")
        return False


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
_VM_MAXBURST_MS    = 3500
_SCREEN_SPEECH_MS  = 2000
_HUMAN_SPEECH_MS   = 1500
_PICKUP_TIMEOUT_S  = 55
_CLASSIFY_WINDOW_S = 9
_SEL_ENDED         = ["[aria-label*='Call ended' i]"]
_VM_PHRASES        = ["leave a message", "after the beep", "record your message",
                      "not available", "please leave"]

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
    try: return driver.execute_script("return window._gvCallState || null;")
    except Exception: return None

def _dom_classify(driver):
    """FIX #62: use Shadow DOM traversal via CDP Runtime.evaluate with pierce to find VM phrases."""
    try:
        result = driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": """
                (function() {
                  var phrases = """ + json.dumps(_VM_PHRASES) + """;
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
                  for (var p = 0; p < phrases.length; p++) {
                    if (fullText.indexOf(phrases[p]) !== -1) return 'voicemail';
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
    if any(p in scope for p in _VM_PHRASES): return "voicemail"
    return "human"

def _classify_audio(cs, elapsed_ms):
    max_burst    = cs.get("maxBurstMs", 0)
    total_speech = cs.get("totalSpeechMs", 0)
    if max_burst >= _VM_MAXBURST_MS:                             return "voicemail"
    if total_speech >= _SCREEN_SPEECH_MS and max_burst < _VM_MAXBURST_MS: return "screening"
    if elapsed_ms >= 4000 and 100 < total_speech < _HUMAN_SPEECH_MS:      return "human"
    if elapsed_ms >= (_CLASSIFY_WINDOW_S * 1000):
        if total_speech == 0:                  return "dom_fallback"
        if total_speech >= _SCREEN_SPEECH_MS:  return "screening"
        if max_burst >= _VM_MAXBURST_MS:       return "voicemail"
        return "human"
    return None

def _wait_for_pickup_and_classify(driver):
    log_msg("[vm] Waiting for pickup...", "info")
    deadline = time.time() + _PICKUP_TIMEOUT_S
    while time.time() < deadline:
        if _dom_has(driver, _SEL_ENDED): return "no_answer"
        if _get_call_timer(driver): break
        time.sleep(0.15)
    else:
        return "no_answer"
    log_msg("[vm] Classifying call...", "info")
    _reset_classify(driver)
    classify_start = time.time()
    # FIX #35: classified flag — only fire dom_fallback once per call
    _classified = False
    while True:
        if _dom_has(driver, _SEL_ENDED): return "no_answer"
        elapsed_ms = (time.time() - classify_start) * 1000
        cs = _get_call_state(driver)
        if cs and cs.get("classifying"):
            result = _classify_audio(cs, elapsed_ms)
            if result == "dom_fallback" and not _classified:
                _classified = True
                return _dom_classify(driver)
            if result is not None and result != "dom_fallback":
                log_msg(f"[vm] Result: {result} "
                        f"(speech={cs.get('totalSpeechMs',0)}ms "
                        f"burst={cs.get('maxBurstMs',0)}ms)", "info")
                try:
                    ring_ms = getattr(driver, "_last_ring_ms", None)
                except Exception:
                    ring_ms = None
                if result == "screening" and ring_ms is not None and ring_ms >= 25000:
                    log_msg(f"[vm] Upgrading screening->voicemail (ring={ring_ms}ms)", "info")
                    result = "voicemail"
                return result
        if elapsed_ms > (_CLASSIFY_WINDOW_S * 1000) + 1000 and not _classified:
            _classified = True
            return _dom_classify(driver)
        time.sleep(0.15)

# FIX #70: removed wait_for_pickup_and_classify() public alias — call _wait_for_pickup_and_classify() directly

# ── DTMF polling ──────────────────────────────────────────────────────────────
def _start_dtmf_listen(driver):
    try:
        driver.execute_script("if(window._gvStartDTMF) window._gvStartDTMF();")
    except Exception as e:
        log_msg(f"[dtmf] Start error: {e}", "warning")

def _stop_dtmf_listen(driver):
    try:
        driver.execute_script("if(window._gvStopDTMF) window._gvStopDTMF();")
    except Exception: pass

def _poll_for_dtmf(driver, timeout_s, number, account_email):
    """
    Poll for a DTMF keypress for up to timeout_s seconds.
    FIX #60: Only triggers on the configured dtmf_key_to_detect, not any key.
    Returns the key string or None on timeout.
    """
    key_to_detect = str(settings.get("dtmf_key_to_detect", "1")).strip()
    log_msg(f"[dtmf] Listening for key '{key_to_detect}' ({timeout_s}s)...", "info")
    deadline = time.time() + timeout_s
    last_key  = None
    while time.time() < deadline:
        if state["_stop"]: break
        try:
            dtmf = driver.execute_script("return window._gvDTMF || null;")
            if dtmf and dtmf.get("detected") and dtmf["detected"] != last_key:
                key = dtmf["detected"]
                last_key = key
                if key == key_to_detect:
                    log_msg(f"[dtmf] Key '{key}' received from {number}!", "success")
                    tg_notify_dtmf(number, key, account_email)
                    return key
                else:
                    log_msg(f"[dtmf] Key '{key}' ignored (watching for '{key_to_detect}')", "info")
        except Exception: pass
        time.sleep(0.2)
    log_msg(f"[dtmf] No key '{key_to_detect}' in {timeout_s}s from {number}", "info")
    return None

# ── make_call ─────────────────────────────────────────────────────────────────
def _maybe_click_chromium_profile(driver):
    try:
        js = """
          var btn = Array.from(document.querySelectorAll('button, div'))
            .find(el => /continue as/i.test(el.textContent||''));
          if (btn) { btn.click(); return true; }
          var link = Array.from(document.querySelectorAll('a'))
            .find(el => /use chromium without an account/i.test(el.textContent||''));
          if (link) { link.click(); return true; }
          return false;
        """
        driver.execute_script(js)
    except Exception:
        pass

def make_call(driver, number, _account=None, _retry=False, _depth=0):
    """FIX #32: added _depth counter to prevent infinite recursion."""
    if _depth > 1:
        log_msg(f"[call] Max retry depth reached for {number}", "error")
        return False
    if not _is_driver_alive(driver):
        if _account and not _retry:
            driver = get_or_create_driver(_account)
            return make_call(driver, number, _account=_account, _retry=True, _depth=_depth + 1)
        return False
    try:
        driver.get("https://voice.google.com/calls")
        for _pdismiss in range(6):
            try:
                _maybe_click_chromium_profile(driver)
            except Exception:
                pass
            time.sleep(0.5)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(1)
        driver.execute_script(_GV_DIAL_JS, number)
        log_msg(f"[call] Dialing {number}...", "info")
        # FIX #58: ring_start set only AFTER _gvDialResult == true is confirmed
        ring_start = None
        for _ in range(60):
            result = driver.execute_script("return window._gvDialResult;")
            err    = driver.execute_script("return window._gvDialError;")
            if err:
                debug = driver.execute_script("return window._gvDialDebug || [];")
                if debug: log_msg(f"[call] Debug info: {debug[:10]}", "warning")
                log_msg(f"[call] Dial error: {err} — {number}", "error"); return False
            if result is True:
                ring_start = time.time()
                break
            time.sleep(0.2)
        else:
            log_msg(f"[call] Dial timed out: {number}", "error"); return False
        try:
            driver._last_ring_ms = max(0, int((time.time() - ring_start) * 1000)) if ring_start else None
        except Exception:
            driver._last_ring_ms = None
        call_type = _wait_for_pickup_and_classify(driver)
        driver._last_call_type = call_type
        if call_type == "no_answer":
            log_msg(f"[call] No answer — {number}", "warning"); return False
        log_msg(f"[call] {number} answered — type: {call_type}", "success")
        return True
    except Exception as e:
        log_msg(f"[call] Error: {e}", "error"); return False

def hang_up(driver):
    try: driver.execute_script(_GV_HANGUP_JS)
    except Exception: pass

# ── Campaign worker ───────────────────────────────────────────────────────────

# FIX #4: hangup() was calling _hangup() which doesn't exist — replaced with hang_up(driver)
def hangup(driver):
    hang_up(driver)

def _account_worker(account, num_queue, drivers, settings, lock):
    """Single-account worker — pulls numbers from shared queue and calls them."""
    acc_key = profile_name_for_account(account)
    email   = account["email"]

    driver = drivers.get(acc_key)
    if not driver:
        log_msg(f"[{email}] No browser — worker exiting.", "error")
        return

    vm_enabled    = settings.get("vm_detection_enabled", False)
    vm_hangup     = settings.get("vm_hangup", True)
    screen_hangup = settings.get("screen_hangup_enabled", False)
    dtmf_enabled  = settings.get("dtmf_enabled", False)
    dtmf_timeout  = int(settings.get("dtmf_timeout", 20))
    audio_screen_bypass = settings.get("audio_screen_bypass", "")
    audio_initial = settings.get("audio_initial", "")
    audio_press1  = settings.get("audio_press1", "")
    delay         = max(5, int(settings.get("delay_between_calls", 45)))

    while not state["_stop"]:
        while state.get("paused") and not state["_stop"]:
            time.sleep(0.5)
        if state["_stop"]:
            break
        with lock:
            if num_queue.empty():
                break
            num = num_queue.get()

        with lock:
            state["current_number"]  = num
            state["current_account"] = email

        log_msg(f"[{email}] → Dialing {num} ({state['completed']+state['failed']+1}/{state['total']})", "info")
        debug_msg(f"worker={email} number={num} screen_hangup={screen_hangup} dtmf={dtmf_enabled} headless={settings.get('headless')}")
        ok = make_call(driver, num, _account=account)

        if ok:
            call_type = getattr(driver, "_last_call_type", "unknown")
            debug_msg(f"classified call {num} as {call_type}")
            if call_type == "voicemail" and vm_enabled and vm_hangup:
                log_msg(f"[{email}] Voicemail — skipping {num}", "warning")
                hang_up(driver)
                with lock: state["failed"] += 1
                time.sleep(3); continue

            sc_action = settings.get("screen_hangup_action", "hangup")
            if call_type == "screening" and screen_hangup:
                if sc_action == "play_audio" and audio_screen_bypass:
                    log_msg(f"[{email}] Screen call detected — playing BYPASS audio: {num}", "warning")
                    time.sleep(0.5)
                    play_audio_in_tab(driver, audio_screen_bypass)
                    debug_msg(f"screening bypass finished for {num}; waiting for live call before initial audio")
                elif sc_action == "play_audio" and audio_initial:
                    log_msg(f"[{email}] Screen call detected — fallback using initial audio to pass screening: {num}", "warning")
                    time.sleep(0.5)
                    play_audio_in_tab(driver, audio_initial)
                else:
                    log_msg(f"[{email}] Screen call — hanging up: {num}", "warning")
                    hang_up(driver)
                    with lock: state["failed"] += 1
                    time.sleep(3); continue

            dtmf_key = None
            if audio_initial:
                log_msg(f"[{email}] [audio] Playing initial message...", "info")
                time.sleep(1)
                if dtmf_enabled:
                    dtmf_key = play_audio_in_tab(
                        driver, audio_initial,
                        block=True,
                        dtmf_interrupt=True,
                        press1_filepath=audio_press1 if audio_press1 else None,
                        number=num,
                        account_email=email
                    )
                    if dtmf_key:
                        log_msg(f"[{email}] DTMF '{dtmf_key}' handled mid-audio for {num}", "success")
                else:
                    play_audio_in_tab(driver, audio_initial)

            if dtmf_key:
                log_msg(f"[{email}] DTMF mid-audio confirmed — press1 already honored, hanging up now", "info")
            elif dtmf_enabled:
                time.sleep(0.3)
                log_msg(f"[{email}] [dtmf] Post-audio listen window {dtmf_timeout}s...", "info")
                _start_dtmf_listen(driver)
                dtmf_key = _poll_for_dtmf(driver, dtmf_timeout, num, email)
                _stop_dtmf_listen(driver)
                if dtmf_key and audio_press1:
                    play_audio_in_tab(driver, audio_press1, block=True)
            elif not audio_initial:
                log_msg(f"[{email}] Call live — holding {delay}s...")
                for _ in range(delay):
                    if state["_stop"]: break
                    time.sleep(1)

            hang_up(driver)
            with lock:
                state["completed"] += 1
                pct = state["completed"] / state["total"] * 100
            log_msg(f"[{email}] ✓ Done {num} — {state['completed']}/{state['total']} ({pct:.1f}%)", "success")
        else:
            with lock: state["failed"] += 1
            log_msg(f"[{email}] ✗ Failed {num}", "error")

    log_msg(f"[{email}] Worker done.", "info")


def campaign_worker():
    global settings
    settings = load_settings()
    state["paused"] = False
    accounts = settings.get("accounts", [])
    if not accounts:
        log_msg("No accounts configured.", "error"); state["running"] = False; return

    numbers = list(state["numbers"])
    if not numbers:
        log_msg("No numbers queued.", "error"); state["running"] = False; return

    concurrent = max(1, min(int(settings.get("concurrent_limit", 1)), len(accounts)))
    active_accs = accounts[:concurrent]

    log_msg(f"Campaign starting — {len(numbers)} numbers, {concurrent} concurrent account(s)", "info")

    for acc in active_accs:
        key = profile_name_for_account(acc)
        log_msg(f"[preflight] Initializing {acc['email']}...", "info")
        try:
            _drivers[key] = get_or_create_driver(acc)
            log_msg(f"[preflight] ✓ {acc['email']} ready", "success")
        except Exception as e:
            log_msg(f"[preflight] ✗ {acc['email']} failed: {e}", "error")

    ready = [a for a in active_accs if profile_name_for_account(a) in _drivers]
    if not ready:
        log_msg("No accounts initialized — aborting.", "error")
        state["running"] = False; return
    log_msg(f"[preflight] {len(ready)}/{concurrent} accounts ready — launching workers", "success")

    import queue as _queue
    num_q = _queue.Queue()
    for n in numbers:
        num_q.put(n)

    lock    = threading.Lock()
    workers = []
    for acc in ready:
        t = threading.Thread(target=_account_worker,
                             args=(acc, num_q, _drivers, settings, lock),
                             daemon=True)
        t.start()
        workers.append(t)

    for t in workers:
        t.join()

    for key in [profile_name_for_account(a) for a in ready]:
        d = _drivers.pop(key, None)
        if d:
            try: d.quit()
            except: pass

    log_msg(f"Campaign done — {state['completed']} done, {state['failed']} failed", "success")
    state["running"] = False
    state["paused"] = False


# ── Flask ─────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
CORS(flask_app, origins='*', supports_credentials=False)


@flask_app.route("/api/login/<profile>", methods=["POST"])
def login_profile(profile):
    global settings
    settings = load_settings()
    email = request.json.get("email") if request.is_json else None
    if not email:
        return jsonify({"message": "Missing email for profile login"}), 400

    account = next((a for a in settings.get("accounts", []) if a.get("email") == email), None)
    if not account:
        return jsonify({"message": f"No account found for {email}"}), 404

    key = profile_name_for_account(account)
    profile_path = os.path.join(PROFILES_D, key)
    os.makedirs(profile_path, exist_ok=True)

    def _do_login():
        try:
            with _login_status_lock:
                state["login_status"][key] = "pending"
            log_msg(f"[login] Starting auto-login for {email} using profile: {profile_path}", "info")
            d = get_or_create_driver(account)
            _drivers[key] = d
            with _login_status_lock:
                state["login_status"][key] = "ok"
            log_msg(f"[login] {email} ready using profile: {profile_path}", "success")
        except Exception as e:
            with _login_status_lock:
                state["login_status"][key] = "failed"
            log_msg(f"[login] {email} failed using profile {profile_path}: {e}", "error")

    with _login_status_lock:
        state["login_status"][key] = "pending"
    threading.Thread(target=_do_login, daemon=True).start()
    return jsonify({
        "message": f"Opening Chromium for {email}...",
        "profile": key,
        "profile_path": profile_path,
    })

@flask_app.route("/api/state")
def api_state():
    with _login_status_lock:
        login_status_snapshot = dict(state["login_status"])
    s = {**state, "log": state["log"][-60:], "login_status": login_status_snapshot}
    try:
        s["profiles_dir"] = PROFILES_D
        s["accounts"] = [a.get("email", "") for a in settings.get("accounts", []) if a.get("email")]
    except Exception:
        pass
    return jsonify(s)

@flask_app.route("/api/start", methods=["POST"])
def api_start():
    if state["running"]: return jsonify({"message": "Already running"})
    state.update({"running": True, "_stop": False, "completed": 0, "failed": 0})
    threading.Thread(target=campaign_worker, daemon=True).start()
    return jsonify({"message": "Campaign started"})

@flask_app.route("/api/stop", methods=["POST"])
def api_stop():
    state["_stop"] = True
    state["paused"] = False
    return jsonify({"message": "Stop signal sent"})

@flask_app.route("/api/pause", methods=["POST"])
def api_pause():
    if not state.get("running"):
        return jsonify({"message": "Not running", "paused": False})
    state["paused"] = not state.get("paused", False)
    msg = "Paused" if state["paused"] else "Resumed"
    log_msg(f"Campaign {msg.lower()}.", "warning")
    return jsonify({"message": msg, "paused": state["paused"]})

@flask_app.route("/api/numbers/load", methods=["POST"])
def api_load_numbers():
    nums = [n.strip() for n in request.json.get("numbers", []) if n.strip()]
    state.update({"numbers": nums, "total": len(nums), "completed": 0, "failed": 0})
    with open(NUMBERS_F, "w") as f: f.write("\n".join(nums))
    return jsonify({"message": f"Loaded {len(nums)} numbers"})

@flask_app.route("/api/numbers/clear", methods=["POST"])
def api_clear():
    state.update({"numbers": [], "total": 0, "completed": 0, "failed": 0})
    return jsonify({"message": "Queue cleared"})

@flask_app.route("/api/numbers/loadfile", methods=["POST"])
def api_loadfile():
    load_numbers_from_file()
    return jsonify({"message": f"Loaded {len(state['numbers'])} numbers from file"})

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
                profile = f"profile{i+1}"

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

@flask_app.route("/api/telegram/send", methods=["POST"])
def api_tg_send():
    tg_notify(request.json.get("message", ""))
    return jsonify({"message": "Sent to Telegram"})

@flask_app.route("/api/test_call", methods=["POST"])
def api_test_call():
    number = request.json.get("number", "")
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
                if call_type == "screening" and s.get("screen_hangup_enabled") and s.get("screen_hangup_action") == "play_audio" and audio_bypass:
                    log_msg(f"[test] Screen call detected — playing BYPASS audio: {number}", "warning")
                    play_audio_in_tab(d, audio_bypass)
                key = None
                if audio_initial:
                    if s.get("dtmf_enabled"):
                        key = play_audio_in_tab(
                            d, audio_initial,
                            block=True,
                            dtmf_interrupt=True,
                            press1_filepath=s.get("audio_press1") if s.get("audio_press1") else None,
                            number=number,
                            account_email=account["email"]
                        )
                    else:
                        play_audio_in_tab(d, audio_initial)
                if s.get("dtmf_enabled") and not key:
                    _start_dtmf_listen(d)
                    key = _poll_for_dtmf(d, int(s.get("dtmf_timeout", 20)), number, account["email"])
                    _stop_dtmf_listen(d)
                    if key and s.get("audio_press1"):
                        play_audio_in_tab(d, s["audio_press1"])
                # FIX #63: always hang up immediately in test call
                hang_up(d)
            log_msg(f"Test call to {number} complete", "success")
        except Exception as e:
            log_msg(f"Test call error: {e}", "error")
    threading.Thread(target=do_test, daemon=True).start()
    return jsonify({"message": f"Test call started to {number}"})


@flask_app.route("/api/audio/play", methods=["POST"])
def api_audio_play():
    data = request.json or {}
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
    # FIX #57: read settings live inside each handler so new tokens saved via API take effect
    def _get_token():
        return load_settings().get("telegram_bot_token", "")

    token = _get_token()
    if not token:
        return
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler
        import asyncio

        async def start_cmd(u, c):
            msg = (
                "Synergy 1.0 online!\n"
                "/call - start campaign\n"
                "/stop - stop campaign\n"
                "/pause - pause/resume\n"
                "/status - current stats\n"
                "/addnumbers 555-555-0001,555-555-0002\n"
                "/clearnumbers - clear queue"
            )
            await u.message.reply_text(msg)

        async def call_cmd(u, c):
            if state["running"]:
                await u.message.reply_text("Already running!")
                return
            state.update({"running": True, "_stop": False, "paused": False, "completed": 0, "failed": 0})
            threading.Thread(target=campaign_worker, daemon=True).start()
            debug_msg("telegram /call invoked")
            await u.message.reply_text(f"Campaign started — {state['total']} numbers.")

        async def stop_cmd(u, c):
            state["_stop"] = True
            state["paused"] = False
            debug_msg("telegram /stop invoked")
            await u.message.reply_text("Stop signal sent.")

        async def pause_cmd(u, c):
            if not state["running"]:
                await u.message.reply_text("Not running.")
                return
            state["paused"] = not state.get("paused", False)
            debug_msg(f"telegram /pause invoked -> paused={state['paused']}")
            await u.message.reply_text("Paused." if state["paused"] else "Resumed.")

        async def status_cmd(u, c):
            # FIX #57: read live settings for each status call
            _s = load_settings()
            pct = (state["completed"] / state["total"] * 100) if state["total"] else 0
            status = "Running" if state["running"] else "Stopped"
            if state.get("paused"):
                status += " (paused)"
            msg = f"{status}\nTotal: {state['total']}  Done: {state['completed']}  Failed: {state['failed']}\n{pct:.1f}%"
            await u.message.reply_text(msg)

        async def addnumbers_cmd(u, c):
            raw = [n.strip() for arg in c.args for n in arg.split(',') if n.strip()]
            existing = set(state["numbers"])
            nums = []
            seen = set()
            for n in raw:
                if n not in existing and n not in seen:
                    nums.append(n)
                    seen.add(n)
            state["numbers"].extend(nums)
            state["total"] = len(state["numbers"])
            debug_msg(f"telegram /addnumbers added={len(nums)} total={state['total']}")
            await u.message.reply_text(f"Added {len(nums)}. Total: {state['total']}")

        async def clearnumbers_cmd(u, c):
            state.update({"numbers": [], "total": 0, "completed": 0, "failed": 0})
            debug_msg("telegram /clearnumbers invoked")
            await u.message.reply_text("Queue cleared.")

        async def main():
            live_token = _get_token()
            app = ApplicationBuilder().token(live_token).build()
            app.add_handler(CommandHandler("start", start_cmd))
            app.add_handler(CommandHandler("call", call_cmd))
            app.add_handler(CommandHandler("stop", stop_cmd))
            app.add_handler(CommandHandler("pause", pause_cmd))
            app.add_handler(CommandHandler("status", status_cmd))
            app.add_handler(CommandHandler("addnumbers", addnumbers_cmd))
            app.add_handler(CommandHandler("clearnumbers", clearnumbers_cmd))
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

        asyncio.run(main())
    except Exception as e:
        logging.warning(f"Telegram bot error: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # FIX #45: run_telegram_bot() started in daemon thread at startup
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    # FIX #47: restore numbers from previous session on startup
    load_numbers_from_file()
    log_msg("Synergy 1.0 backend running on http://localhost:5050", "success")
    flask_app.run(host="0.0.0.0", port=5050, debug=False)
