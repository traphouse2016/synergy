const {app,BrowserWindow}=require('electron')
app.whenReady().then(()=>{
  const w=new BrowserWindow({
    width:1200,height:780,minWidth:900,minHeight:600,
    backgroundColor:'#0c0c0c',title:'GV Bot',
    webPreferences:{contextIsolation:false,nodeIntegration:true,webSecurity:false}
  })
  w.loadFile('ui.html')
  w.setMenuBarVisibility(false)
  // uncomment below to debug:
  // w.webContents.openDevTools()
  app.on('window-all-closed',()=>app.quit())
})
