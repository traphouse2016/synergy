const { app, BrowserWindow } = require('electron');

// #27 FIX: window-all-closed must be registered at top level, NOT inside whenReady.
// When registered inside whenReady it fires after the window is already gone on
// some Linux/Windows builds and app.quit() is never called, leaving the process
// hanging indefinitely.
app.on('window-all-closed', () => app.quit());

app.whenReady().then(() => {
  const w = new BrowserWindow({
    width: 1200, height: 780, minWidth: 900, minHeight: 600,
    backgroundColor: '#0c0c0c', title: 'Synergy',
    webPreferences: {
      // #26 FIX: contextIsolation:false + nodeIntegration:true is the correct
      // setup for this app (no contextBridge needed). With contextIsolation:true
      // window.electronAPI would be undefined unless a preload exposes it.
      // Keeping false so require('http') works directly in ui.html.
      contextIsolation: false,
      nodeIntegration: true,
      webSecurity: false
    }
  });
  w.loadFile('ui.html');
  w.setMenuBarVisibility(false);
  // Uncomment to debug:
  // w.webContents.openDevTools();
});
