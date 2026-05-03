const {app,BrowserWindow}=require('electron')
const path=require('path')

// FIX #27: window-all-closed moved to top-level app scope (not inside whenReady)
app.on('window-all-closed',()=>app.quit())

app.whenReady().then(()=>{
  const w=new BrowserWindow({
    width:1200,height:780,minWidth:900,minHeight:600,
    backgroundColor:'#0c0c0c',title:'GV Bot',
    // FIX #26: contextIsolation:true so contextBridge works correctly
    // FIX #72: removed webSecurity:false — unnecessary and disables CORS/mixed-content protection
    // FIX #73: nodeIntegration:false — prevents full RCE if XSS occurs; use contextBridge instead
    webPreferences:{
      contextIsolation:true,
      nodeIntegration:false,
      preload:path.join(__dirname,'preload.js')
    }
  })
  w.loadFile('ui.html')
  w.setMenuBarVisibility(false)
  // uncomment below to debug:
  // w.webContents.openDevTools()
})
