'use strict';
// Only the two windows created here are moved/minimized. No WeChat interaction.
const {app,BrowserWindow,screen,nativeImage}=require('electron');
const assert=require('node:assert/strict');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const {WindowController}=require('../window-controller.cjs');
const {trayBitmap}=require('../icon.cjs');
const root=path.resolve(__dirname,'../../..');
app.setPath('userData',path.join(root,'scripts','tmp','copilot-native-fixture'));
const probe=win=>JSON.parse(execFileSync(path.join(root,'.venv','Scripts','python.exe'),[
  '-X','utf8',path.join(root,'tests','copilot_native_probe.py'),'--hwnd',String(win.getNativeWindowHandle().readBigUInt64LE())
],{cwd:root,windowsHide:true,encoding:'utf8'}));
const wait=()=>new Promise(resolve=>setTimeout(resolve,120));
app.whenReady().then(async()=>{
 let target,panel;const checked=[];
 try{
  const svg='<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#247361"/></svg>';
  const svgImage=nativeImage.createFromDataURL('data:image/svg+xml;base64,'+Buffer.from(svg).toString('base64'));
  assert.equal(nativeImage.createFromBitmap(trayBitmap(),{width:32,height:32}).isEmpty(),false);checked.push('tray-bitmap-supported');
  target=new BrowserWindow({x:60,y:80,width:600,height:450,show:false,title:'Copilot synthetic target',webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
  panel=new BrowserWindow({x:700,y:80,width:360,height:340,show:false,frame:false,focusable:false,skipTaskbar:true,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
  await target.loadURL('data:text/html,<html lang="en"><title>Synthetic test window</title><body>Entirely synthetic geometry fixture. No chat content.</body></html>');
  await panel.loadURL('data:text/html,<html><body>Copilot synthetic panel</body></html>');
  target.showInactive();await wait();
  let observed=probe(target);
  const controller=new WindowController({view:{hide:()=>panel.hide(),showInactive:()=>panel.showInactive(),setBounds:r=>panel.setBounds(r,false)},
    ownPid:process.pid,workArea:r=>screen.getDisplayMatching(r).workArea,toDip:r=>screen.screenToDipRect(null,r)});
  const frame=()=>{const p=probe(target);return {version:1,state:'bound',target:'synthetic-only',rect:p.geometry.rect,visible:p.geometry.visible&&!p.geometry.cloaked,minimized:p.geometry.minimized,foreground:true,foreground_pid:process.pid,candidate_count:1}};
  const foreground=observed.foreground_hwnd;
  controller.update(frame());await wait();assert.equal(panel.isVisible(),true);assert.equal(probe(target).foreground_hwnd,foreground);checked.push('show-does-not-activate');
  const before=panel.getBounds();target.setBounds({x:100,y:120,width:650,height:460},false);await wait();controller.update(frame());await wait();
  assert.notDeepEqual(panel.getBounds(),before);assert.equal(probe(target).foreground_hwnd,foreground);checked.push('moves-and-resizes-with-target');
  target.minimize();await wait();controller.update(frame());assert.equal(panel.isVisible(),false);checked.push('minimize-hides');
  target.restore();target.showInactive();await wait();const foregroundAfterRestore=probe(target).foreground_hwnd;controller.update(frame());await wait();
  assert.equal(panel.isVisible(),true);assert.equal(probe(target).foreground_hwnd,foregroundAfterRestore);checked.push('restore-panel-does-not-activate');
  controller.update({...frame(),foreground:false,foreground_pid:0});assert.equal(panel.isVisible(),false);checked.push('other-app-hides');
  controller.update(frame());controller.fail('WINDOW_WATCH_STOPPED');assert.equal(panel.isVisible(),false);checked.push('bridge-loss-hides');
  console.log(JSON.stringify({status:'passed',checks:checked,count:checked.length,real_wechat_manipulated:false}));
  panel.destroy();target.destroy();app.exit(0);
 }catch(error){console.log(JSON.stringify({status:'failed',completed:checked,error: String(error.name)}));panel?.destroy();target?.destroy();app.exit(1)}
});
