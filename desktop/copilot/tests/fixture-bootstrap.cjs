'use strict';
// Test-only entrypoint copied into a synthetic root; never used by the launcher.
// Playwright's debugger switches are not accepted by the production CLI.
process.argv=[process.argv[0],__filename,...process.argv.filter(value=>/^--port=\d+$/.test(value)||value==='--show')];
const fs=require('node:fs');const {PassThrough}=require('node:stream');const {EventEmitter}=require('node:events');
const childProcess=require('node:child_process');const exists=fs.existsSync;
const {app,BrowserWindow,globalShortcut,screen}=require('electron');
globalThis.__fixtureWatchStarts=0;
globalThis.__fixtureWatchActive=0;
globalThis.__fixtureSecondArgs=[];app.on('second-instance',(_event,args)=>{globalThis.__fixtureSecondArgs=args});
globalThis.__fixtureFocusCalls=0;
const focus=BrowserWindow.prototype.focus;
BrowserWindow.prototype.focus=function(...args){globalThis.__fixtureFocusCalls++;return focus.apply(this,args)};
let mark;let cursor={x:0,y:0};
globalShortcut.register=(_key,callback)=>{mark=callback;return true};
globalShortcut.unregister=()=>{mark=null};
app.whenReady().then(()=>{screen.getCursorScreenPoint=()=>cursor});
globalThis.__fixtureTitle='合成青禾';
globalThis.__fixtureCaptures=0;
globalThis.__fixtureMark=(x,y)=>{
 const rect=screen.screenToDipRect(null,{x:80,y:80,width:500,height:500});
 cursor={x:rect.x+x,y:rect.y+y};if(mark)mark();
};
fs.existsSync=file=>String(file).endsWith('Scripts\\python.exe')||String(file).endsWith('copilot_window_watch.py')||exists(file);
childProcess.spawn=(_exe,args)=>{
 const child=new EventEmitter();child.stdin=new PassThrough();child.stdout=new PassThrough();child.exitCode=null;
 if(args.some(value=>String(value).endsWith('copilot_title_read.ps1'))){
  child.stdin.on('data',chunk=>{
   globalThis.__fixtureCaptures++;
   const request=JSON.parse(String(chunk));
   const result=request.operation==='calibrate'
     ?{state:'calibrated',region:{x:340,y:16,width:100,height:48},target:'a'.repeat(64),source:'local_ocr'}
     :{state:'observed',title:globalThis.__fixtureTitle,target:'synthetic-ocr-window',source:'local_ocr'};
   setImmediate(()=>{if(child.exitCode===null)child.stdout.write(JSON.stringify(result)+'\n')});
  });
  child.kill=()=>{child.exitCode=0;child.emit('exit',0)};
  child.stdin.on('finish',()=>child.kill());return child;
 }
 const frame={version:1,state:'bound',target:'synthetic-window',rect:{x:80,y:80,width:500,height:500},visible:true,minimized:false,foreground:true,foreground_pid:process.pid,candidate_count:1};
 globalThis.__fixtureWatchStarts++;globalThis.__fixtureWatchActive++;
 const timer=setInterval(()=>child.stdout.write(JSON.stringify(frame)+'\n'),100);
 child.kill=()=>{if(child.exitCode!==null)return;clearInterval(timer);globalThis.__fixtureWatchActive--;child.exitCode=0;child.emit('exit',0)};
 child.stdin.on('finish',()=>child.kill());return child;
};
require('./main.cjs');
if(process.env.COPILOT_FIXTURE_QUEUED_SHOW==='1')app.emit('second-instance',{},[...process.argv,'--show'],process.cwd());
