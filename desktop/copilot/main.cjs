'use strict';
// Controls only its own windows; optional OCR reads a user-calibrated header patch.
const {app,BrowserWindow,Menu,Tray,nativeImage,ipcMain,screen,shell,session,clipboard,globalShortcut}=require('electron');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const {launchOptions,trustedSender,windowCommand,allowedResource}=require('./security.cjs');
const {trayBitmap}=require('./icon.cjs');
const {WindowController}=require('./window-controller.cjs');
const {TitleObserver,validCalibration}=require('./title-observer.cjs');
const {TitleReader}=require('./title-reader.cjs');
const {TitleCalibration}=require('./title-calibration.cjs');
const {AutoCalibration}=require('./auto-calibration.cjs');

const root=path.resolve(__dirname,'../..');
let options;
try{options=launchOptions(process.argv.slice(2))}catch{console.error('COPILOT_INVALID_ARGUMENT');app.exit(2)}
if(process.platform!=='win32'){console.error('COPILOT_WINDOWS_REQUIRED');app.exit(2)}
app.setPath('userData',path.join(root,'data','private','copilot','desktop-profile'));
app.setAppUserModelId('HumanOnlineRelationships.Copilot');
app.commandLine.appendSwitch('disable-http-cache');
const lock=app.requestSingleInstanceLock();
if(!lock)app.quit();

let win,tray,controller,watcher,tickTimer,pageReady=false,quitting=false,editMode=false;
let dragTimer;
let titleObserver,titleReader,titleCalibration,titleAuto,titleTimer,recognitionEnabled=true;
const safeSend=state=>{if(win&&!win.isDestroyed()&&pageReady)win.webContents.send('copilot:state',state)};
const sendConversation=state=>{if(win&&!win.isDestroyed()&&pageReady)win.webContents.send('copilot:conversation',state)};
function titleTarget(){
  const frame=controller?.frame;
  if(!pageReady||controller.paused||frame?.state!=='bound'||!frame.visible||frame.minimized
    ||(!frame.foreground&&frame.foreground_pid!==process.pid))return null;
  return {id:frame.target,...screen.screenToDipRect(null,frame.rect),
    scale:screen.getDisplayMatching(screen.screenToDipRect(null,frame.rect)).scaleFactor};
}
function sampleTitle(fresh=false){
  if(!titleObserver)return Promise.resolve(null);
  if(titleAuto?.active){titleAuto.check();return Promise.resolve(titleObserver.snapshot())}
  if(titleCalibration?.active)return Promise.resolve(titleObserver.snapshot());
  if(!recognitionEnabled||!titleTarget()){
    titleReader.stop(controller?.paused?'TITLE_PAUSED':'TITLE_INACTIVE');return Promise.resolve(titleObserver.snapshot());
  }
  return fresh?titleReader.refresh():titleReader.sample();
}
function setupTitleReader(){
  titleObserver=new TitleObserver({onState:sendConversation});
  titleReader=new TitleReader({root,parentPid:process.pid,observer:titleObserver});
  const regionFile=path.join(root,'data','private','copilot','header-region.json');
  try{
    const stat=fs.lstatSync(regionFile);
    if(stat.isSymbolicLink()||stat.size>1024)throw new Error('invalid');
    const saved=JSON.parse(fs.readFileSync(regionFile,'utf8'));
    if(saved.profile==='wechat-4.1.13'&&validCalibration(saved.region))titleReader.setRegion(saved.region);
  }catch{/* Missing or invalid numeric-only preferences require user calibration. */}
  titleCalibration=new TitleCalibration({shortcuts:globalShortcut,target:titleTarget,cursor:()=>screen.getCursorScreenPoint(),
    notify:code=>titleReader.stop(code),save:region=>{
      fs.mkdirSync(path.dirname(regionFile),{recursive:true});
      if(fs.existsSync(regionFile)&&fs.lstatSync(regionFile).isSymbolicLink())throw new Error('invalid');
      const temp=regionFile+'.'+require('node:crypto').randomUUID()+'.tmp';
      fs.writeFileSync(temp,JSON.stringify({profile:'wechat-4.1.13',region}),{flag:'wx'});
      fs.renameSync(temp,regionFile);titleReader.setRegion(region);
    }});
  titleAuto=new AutoCalibration({target:titleTarget,read:()=>titleReader.calibrate(),
    stop:reason=>titleReader.stop(reason),save:region=>titleCalibration.save(region)});
  titleTimer=setInterval(()=>{titleObserver.tick();sampleTitle().catch(()=>titleReader.stop('TITLE_READER_UNAVAILABLE'))},1000);
}

function stopWatcher(){
  const child=watcher;watcher=null;
  if(child){child.stdin.end();setTimeout(()=>{if(child.exitCode===null)child.kill()},1000).unref()}
}
function startWatcher(){
  if(watcher||quitting)return;
  const python=path.join(root,'.venv','Scripts','python.exe');
  const script=path.join(root,'scripts','copilot_window_watch.py');
  if(!fs.existsSync(python)||!fs.existsSync(script)){controller.fail('WINDOW_WATCH_NOT_INSTALLED');return}
  const child=spawn(python,['-X','utf8',script,'--parent-pid',String(process.pid)],{
    cwd:root,windowsHide:true,stdio:['pipe','pipe','ignore'],shell:false,
    env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}
  });
  watcher=child;let buffer='';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data',chunk=>{
    if(watcher!==child)return;
    buffer+=chunk;
    if(buffer.length>32768){controller.fail('WINDOW_PROTOCOL_ERROR');stopWatcher();return}
    let newline;
    while((newline=buffer.indexOf('\n'))!==-1){
      const line=buffer.slice(0,newline).trim();buffer=buffer.slice(newline+1);if(!line)continue;
      try{controller.update(JSON.parse(line));titleAuto?.check()}catch{controller.fail('WINDOW_PROTOCOL_ERROR');stopWatcher();return}
    }
  });
  child.on('error',()=>{if(watcher===child){watcher=null;controller.fail('WINDOW_WATCH_FAILED')}});
  child.on('exit',()=>{if(watcher===child){watcher=null;controller.fail('WINDOW_WATCH_STOPPED')}});
}
function pause(value){
  if(value){if(titleAuto?.active)titleAuto.cancel('TITLE_PAUSED');if(titleCalibration?.active)titleCalibration.cancel('TITLE_PAUSED');titleReader?.stop('TITLE_PAUSED')}
  controller.pause(value);
  if(value)stopWatcher();else startWatcher();
  buildMenu();
}
function buildMenu(){
  if(!tray)return;
  tray.setContextMenu(Menu.buildFromTemplate([
    {label:'对话副驾 · 1.0 候选版',enabled:false},
    {label:controller?.paused?'恢复窗口跟随':'暂停窗口跟随',click:()=>pause(!controller.paused)},
    {label:'吸附微信边缘',type:'radio',checked:controller?.mode==='dock',click:()=>controller.setMode('dock')},
    {label:'自由悬浮',type:'radio',checked:controller?.mode==='float',click:()=>controller.setMode('float')},
    {type:'separator'},
    {label:'在浏览器打开副驾设置',click:()=>shell.openExternal(options.url)},
    {label:'重新连接本机页面',click:()=>loadPage()},
    {label:'退出副驾（不关闭微信）',click:()=>app.quit()}
  ]));
}
async function loadPage(){
  if(!win||win.isDestroyed())return;
  pageReady=false;win.hide();controller.visible=false;
  try{
    await win.loadURL(options.url);
    if(win.webContents.getURL()!==options.url)throw new Error('UNTRUSTED_PAGE');
    pageReady=true;safeSend(controller.snapshot());if(titleObserver)sendConversation(titleObserver.snapshot());controller.render();
  }
  catch{pageReady=false;win.hide();tray?.setToolTip('对话副驾：本机控制台未就绪，可从菜单重新连接')}
}
function create(){
  session.defaultSession.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
  session.defaultSession.setPermissionCheckHandler(()=>false);
  // No direct cloud, local files, arbitrary localhost ports, or external frames.
  session.defaultSession.webRequest.onBeforeRequest((details,callback)=>{
    callback({cancel:!allowedResource(details,options.url)});
  });
  win=new BrowserWindow({width:360,height:340,show:false,frame:false,transparent:false,
    backgroundColor:'#f7f8f5',resizable:false,minimizable:false,maximizable:false,
    skipTaskbar:true,focusable:false,alwaysOnTop:true,hasShadow:true,autoHideMenuBar:true,
    title:'对话副驾',webPreferences:{preload:path.join(__dirname,'preload.cjs'),
      nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true,
      spellcheck:false,backgroundThrottling:true,devTools:false}});
  win.setMenu(null);
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  win.webContents.on('will-navigate',(event,url)=>{if(url!==options.url)event.preventDefault()});
  win.webContents.on('will-redirect',(event,url)=>{if(url!==options.url)event.preventDefault()});
  win.webContents.on('will-attach-webview',event=>event.preventDefault());
  win.webContents.on('render-process-gone',()=>{pageReady=false;win.hide();controller.fail('WINDOW_RENDERER_STOPPED')});
  controller=new WindowController({
    view:{hide:()=>win.hide(),showInactive:()=>{if(pageReady)win.showInactive()},setBounds:r=>win.setBounds(r,false)},
    ownPid:process.pid,ready:()=>pageReady,toDip:r=>screen.screenToDipRect(null,r),
    workArea:r=>screen.getDisplayMatching(r).workArea,onState:safeSend
  });
  setupTitleReader();
  win.on('blur',()=>{if(editMode){editMode=false;win.setFocusable(false)}});
  win.on('moved',()=>{
    clearTimeout(dragTimer);dragTimer=setTimeout(()=>{
      if(win.isDestroyed())return;
      controller.onUserMove(win.getBounds());
      buildMenu();
    },120);
  });
  win.on('closed',()=>app.quit());
  ipcMain.handle('copilot:command',(event,command,value)=>{
    if(!trustedSender(event,win.webContents,options.url))throw new Error('COPILOT_UNTRUSTED_FRAME');
    windowCommand(command,value);
    if(command==='conversationState')return titleObserver.snapshot();
    if(command==='refreshConversation')return sampleTitle(true);
    if(command==='calibrateTitle'){if(titleAuto.active)titleAuto.cancel();titleCalibration.toggle();return titleObserver.snapshot()}
    if(command==='autoCalibrateTitle'){
      if(!recognitionEnabled){titleReader.stop('TITLE_MANUAL');return titleObserver.snapshot()}
      if(titleCalibration.active)titleCalibration.cancel();
      return titleAuto.run().then(()=>titleObserver.snapshot()).catch(()=>titleObserver.clear('TITLE_AUTO_FAILED'));
    }
    if(command==='recognition'){
      recognitionEnabled=value;
      if(!value){if(titleAuto.active)titleAuto.cancel();if(titleCalibration.active)titleCalibration.cancel();titleReader.stop('TITLE_MANUAL')}
      return titleObserver.snapshot();
    }
    if(command==='mode'){controller.setMode(value);buildMenu()}
    if(command==='size')controller.setSize(value);
    if(command==='pause')pause(value);
    if(command==='edit'){editMode=value;win.setFocusable(value);if(value)win.focus()}
    if(command==='copy')clipboard.writeText(value);
    if(command==='close')app.quit();
    return controller.snapshot();
  });
  tray=new Tray(nativeImage.createFromBitmap(trayBitmap(),{width:32,height:32}));
  tray.setToolTip('对话副驾 · 本机标题识别，不读取正文或发送消息');
  tray.on('double-click',()=>shell.openExternal(options.url));
  buildMenu();
  screen.on('display-metrics-changed',()=>controller.render());
  screen.on('display-removed',()=>controller.render());
  tickTimer=setInterval(()=>controller.tick(),500);
  loadPage().then(startWatcher);
}
app.on('second-instance',()=>{}); // An accidental second launch must not steal focus.
app.on('before-quit',()=>{quitting=true;clearInterval(tickTimer);clearInterval(titleTimer);clearTimeout(dragTimer);if(titleAuto?.active)titleAuto.cancel();if(titleCalibration?.active)titleCalibration.cancel();titleReader?.stop();stopWatcher();tray?.destroy()});
app.on('window-all-closed',()=>app.quit());
if(lock)app.whenReady().then(create).catch(()=>{console.error('COPILOT_START_FAILED');app.exit(1)});
