tected_key  = None
    dtmf_event    = threading.Event()
    dtmf_lock     = threading.Lock()

flask_app = Flask(__name__)
CORS(flask_app)

_drivers     = {}
_drivers_lock = threading.Lock()


def _inject_audio_hook(driver):
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
(function() {
  const _origGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = function(constraints) {
    return _origGetUserMedia(constraints).then(stream => {
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        window._gvAnalyser = analyser;
        window._gvAudioCtx = ctx;
        window._callStartTime = null;
        window._totalSpeechMs = 0;
        window._silenceMs = 0;
        window._maxBurstMs = 0;
        window._phraseCount = 0;
        window._lastSpeech = false;
        window._burstStart = null;
        let _lastEnergy = 0;
        const buf = new Float32Array(analyser.frequencyBinCount);
        function tick() {
          analyser.getFloatFrequencyData(buf);
          const energy = buf.reduce((s,v)=>s+Math.pow(10,(v/10)),0)/buf.length;
          const speaking = energy > 1e-5;
          const now = performance.now();
          if (window._callStartTime === null && speaking) {
            window._callStartTime = now;
          }
          if (speaking) {
            if (!window._lastSpeech) {
              window._phraseCount++;
              window._burstStart = now;
            }
            window._totalSpeechMs += 16;
            const burstDur = now - (window._burstStart || now);
            if (burstDur > window._maxBurstMs) window._maxBurstMs = burstDur;
          } else {
            window._silenceMs += 16;
          }
          window._lastSpeech = speaking;
          window._gvLastEnergy = energy;
          setTimeout(tick, 16);
        }
        tick();
      } catch(e) {
        console.warn('Audio hook error:', e);
      }
      return stream;
    });
  };
  window._gvAudioPlaying = false;
  window._gvAudioDone    = false;
  window._gvAudioError   = null;

  window._gvPlayAudio = function(url) {
    window._gvAudioPlaying = true;
    window._gvAudioDone    = false;
    window._gvAudioError   = null;
    const a = new Audio(url);
    a.onended  = () => { window._gvAudioPlaying=false; window._gvAudioDone=true; };
    a.onerror  = (e) => { window._gvAudioPlaying=false; window._gvAudioError=String(e); window._gvAudioDone=true; };
    a.play().catch(e => { window._gvAudioError=String(e); window._gvAudioDone=true; });
    window._gvCurrentAudio = a;
  };

  window._gvStopAudio = function() {
    if (window._gvCurrentAudio) {
      window._gvCurrentAudio.pause();
      window._gvCurrentAudio.currentTime = 0;
    }
    window._gvAudioPlaying = false;
    window._gvAudioDone    = true;
  };

  const _origRTCPeerConnection = window.RTCPeerConnection;
  window.RTCPeerConnection = function(config, ...rest) {
    const pc = new _origRTCPeerConnection(config, ...rest);
    window._gvPC = pc;
    return pc;
  };
  Object.setPrototypeOf(window.RTCPeerConnection, _origRTCPeerConnection);

  window._gvCallState = 'idle';
  const _origFetch = window.fetch;
  window.fetch = function(url, opts) {
    const u = String(url);
    if (u.includes('/calls') || u.includes('voice.google.com')) {
      if (opts && opts.method === 'POST') window._gvCallState = 'call_started';
      if (u.includes('hangup') || u.includes('end'))  window._gvCallState = 'call_ended';
    }
    return _origFetch(url, opts);
  };

  Object.defineProperty(window, '_gvCallActive', {
    get() {
      if (window._gvCallState === 'call_ended') return false;
      if (window._gvPC) {
        const s = window._gvPC.connectionState;
        if (s === 'failed' || s === 'closed' || s === 'disconnected') return false;
      }
      return window._gvCallState !== 'idle';
    }
  });

  // Expose compact call-stats snapshot
  window._gvStats = function() {
    return {
      totalSpeechMs : window._totalSpeechMs  || 0,
      silenceMs     : window._silenceMs       || 0,
      maxBurstMs    : window._maxBurstMs      || 0,
      phraseCount   : window._phraseCount     || 0,
      callStartTime : window._callStartTime   || null,
      lastEnergy    : window._gvLastEnergy    || 0,
      callState     : window._gvCallState     || 'idle',
    };
  };

  // --- DTMF FFT detector ---
  const DTMF_ROWS = [697,770,852,941];
  const DTMF_COLS = [1209,1336,1477,1633];
  const DTMF_MAP  = [
    ['1','2','3','A'], ['4','5','6','B'],
    ['7','8','9','C'], ['*','0','#','D']
  ];
  window._dtmfDetected    = null;
  window._dtmfListening   = false;
  window._dtmfTargetKey   = null;
  window._dtmfConfirmed   = false;
  window._call_not_started = false;

  window._startDtmfListen = function(targetKey) {
    window._dtmfListening = true;
    window._dtmfDetected  = null;
    window._dtmfTargetKey = targetKey || null;
    window._dtmfConfirmed = false;
    if (!window._gvAnalyser) { return; }
    const sampleRate = window._gvAudioCtx.sampleRate;
    const fftSize = 4096;
    const dtmfAn  = window._gvAudioCtx.createAnalyser();
    dtmfAn.fftSize = fftSize;
    if (window._gvAnalyser.context === window._gvAudioCtx) {
      try { window._gvAnalyser.connect(dtmfAn); } catch(e) {}
    }
    const buf2 = new Uint8Array(dtmfAn.frequencyBinCount);
    const freqRes = sampleRate / fftSize;
    function freqBin(f) { return Math.round(f / freqRes); }
    function tickDtmf() {
      if (!window._dtmfListening) return;
      dtmfAn.getByteFrequencyData(buf2);
      function peak(f) {
        const b = freqBin(f);
        return Math.max(
          buf2[Math.max(0,b-1)], buf2[b],
          b+1 < buf2.length ? buf2[b+1] : 0
        );
      }
      const rowPeaks = DTMF_ROWS.map(peak);
      const colPeaks = DTMF_COLS.map(peak);
      const row = rowPeaks.indexOf(Math.max(...rowPeaks));
      const col = colPeaks.indexOf(Math.max(...colPeaks));
      const threshold = 30;
      if (rowPeaks[row] > threshold && colPeaks[col] > threshold) {
        const key = DTMF_MAP[row][col];
        if (!window._dtmfTargetKey || key === window._dtmfTargetKey) {
          window._dtmfDetected  = key;
          window._dtmfConfirmed = true;
          window._dtmfListening = false;
          return;
        }
      }
      setTimeout(tickDtmf, 30);
    }
    tickDtmf();
  };

  window._stopDtmfListen = function() {
    window._dtmfListening = false;
  };

  window._markCallNotStarted = function() {
    window._call_not_started = true;
  };

  window._checkCallNotStarted = function() {
    const r = window._call_not_started;
    window._call_not_started = false;
    return r;
  };

  window._getCallTimer = function() {
    const el = document.querySelector('.EtbHpd');
    return el ? el.innerText.trim() : null;
  };

  window._gvDTMFActive = false;

  window._getCallState = function() {
    if (document.querySelector('.Jyj8Xc') || document.querySelector('[data-call-ended]')) return 'ended';
    if (document.querySelector('.EtbHpd')) return 'in_call';
    return 'idle';
  };

})();
"""        })
    except Exception as e:
        log_msg(f"CDP hook inject error: {e}", "warning")


def get_driver(profile_name, headless=False):
    opts = make_options(profile_name, headless)
    try:
        service = Service(ChromeDriverManager().install())
        driver  = webdriver.Chrome(service=service, options=opts)
    except Exception:
        chromium_path = _find_chromium()
        if chromium_path:
            service = Service(ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install())
            driver  = webdriver.Chrome(service=service, options=opts)
        else:
            raise
    _inject_audio_hook(driver)
    return driver


def gv_login(driver, account):
    """Navigate to GV and attempt login if needed."""
    try:
        driver.get("https://voice.google.com")
        time.sleep(3)
    except Exception as e:
        log_msg(f"GV navigate error: {e}", "warning")
        return False
    if "voice.google.com" in driver.current_url:
        return True
    # Try to log in
    try:
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
        )
        email_input.send_keys(account["email"])
        email_input.send_keys(Keys.RETURN)
        time.sleep(2)
        pwd_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
        pwd_input.send_keys(account["password"])
        pwd_input.send_keys(Keys.RETURN)
        time.sleep(5)
    except Exception as e:
        log_msg(f"Login flow error: {e}", "warning")
        return False
    return "voice.google.com" in driver.current_url
