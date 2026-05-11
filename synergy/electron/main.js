const { app, BrowserWindow } = require('electron')
const path   = require('path')
const { spawn } = require('child_process')
const http   = require('http')

app.on('window-all-closed', () => app.quit())

// ── Launch Python backend ─────────────────────────────────────────────────────
let pyProc = null

function startBackend() {
  // Try `python` then `python3`
  const candidates = ['python', 'python3']
  const backendPath = path.join(__dirname, 'synergy.py')

  function tryNext(i) {
    if (i >= candidates.length) {
      console.error('[main] Could not launch Python backend')
      return
    }
    const proc = spawn(candidates[i], [backendPath], {
      cwd: __dirname,
      stdio: ['ignore', 'pipe', 'pipe']
    })
    proc.on('error', () => tryNext(i + 1))
    proc.stdout.on('data', d => process.stdout.write('[py] ' + d))
    proc.stderr.on('data', d => process.stderr.write('[py] ' + d))
    proc.on('exit', code => console.log('[main] Backend exited:', code))
    pyProc = proc
  }
  tryNext(0)
}

// ── Wait for backend to be ready, then load UI ───────────────────────────────
function waitForBackend(win, retries = 40) {
  http.get('http://localhost:5050/api/state', res => {
    // Backend is up — load the real UI
    win.loadFile('ui.html')
  }).on('error', () => {
    if (retries <= 0) {
      // Timed out — load anyway and let UI show its own error
      win.loadFile('ui.html')
      return
    }
    setTimeout(() => waitForBackend(win, retries - 1), 500)
  })
}

app.whenReady().then(() => {
  // Start backend first
  startBackend()

  const w = new BrowserWindow({
    width: 1200, height: 780, minWidth: 900, minHeight: 600,
    backgroundColor: '#0c0c0c',
    title: 'Synergy 1.0',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js')
    }
  })
  w.setMenuBarVisibility(false)

  // Show a loading screen while backend boots
  w.loadURL('data:text/html,<html style="background:#0c0c0c;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><p style="color:#4f98a3;font-family:sans-serif;font-size:18px;letter-spacing:2px">Starting backend...</p></html>')

  // Poll until backend responds, then swap to ui.html
  setTimeout(() => waitForBackend(w), 800)

  // Clean up backend on close
  w.on('closed', () => {
    if (pyProc) { try { pyProc.kill() } catch(e) {} }
  })
})

app.on('before-quit', () => {
  if (pyProc) { try { pyProc.kill() } catch(e) {} }
})
