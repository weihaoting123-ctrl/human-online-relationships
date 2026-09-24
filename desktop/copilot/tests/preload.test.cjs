'use strict';
const test=require('node:test');const assert=require('node:assert/strict');
const vm=require('node:vm');const fs=require('node:fs');const path=require('node:path');
test('native copy requires transient user activation before any IPC',async()=>{
 let bridge;const calls=[];const navigator={userActivation:{isActive:false}};
 const electron={contextBridge:{exposeInMainWorld:(_name,value)=>{bridge=value}},
  ipcRenderer:{invoke:(...args)=>{calls.push(args);return Promise.resolve({})}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../preload.cjs'),'utf8'),{navigator,require:()=>electron});
 await assert.rejects(bridge.copy('synthetic'),/USER_GESTURE_REQUIRED/);assert.equal(calls.length,0);
 navigator.userActivation.isActive=true;await bridge.copy('synthetic');
 assert.equal(calls.length,1);assert.deepEqual(calls[0],['copilot:command','copy','synthetic']);
 assert.equal(bridge.readClipboard,undefined);
});
test('conversation bridge is read-only and calibration requires activation',async()=>{
 let bridge;const calls=[],listeners=new Map();const navigator={userActivation:{isActive:false}};
 const electron={contextBridge:{exposeInMainWorld:(_name,value)=>{bridge=value}},ipcRenderer:{invoke:(...args)=>{calls.push(args);return Promise.resolve({})},on:(name,fn)=>listeners.set(name,fn),removeListener:name=>listeners.delete(name)}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../preload.cjs'),'utf8'),{navigator,require:()=>electron});
 assert.equal(typeof bridge.conversationState,'function');assert.equal(typeof bridge.refreshConversation,'function');
 await assert.rejects(bridge.calibrateTitle(),/USER_GESTURE_REQUIRED/);assert.equal(calls.length,0);
 navigator.userActivation.isActive=true;await bridge.calibrateTitle();assert.equal(calls[0][1],'calibrateTitle');
 let observed;const unsubscribe=bridge.onConversation(value=>observed=value);listeners.get('copilot:conversation')({}, {seq:1});assert.equal(observed.seq,1);unsubscribe();assert.equal(listeners.size,0);
 assert.equal(bridge.captureScreen,undefined);assert.equal(bridge.setTitle,undefined);
});
test('automatic calibration needs a fresh gesture and accepts no arbitrary crop',async()=>{
 let bridge;const calls=[];const navigator={userActivation:{isActive:false}};
 const electron={contextBridge:{exposeInMainWorld:(_name,value)=>bridge=value},ipcRenderer:{invoke:(...args)=>{calls.push(args);return Promise.resolve({})}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../preload.cjs'),'utf8'),{navigator,require:()=>electron});
 assert.equal(typeof bridge.autoCalibrateTitle,'function');await assert.rejects(bridge.autoCalibrateTitle(),/USER_GESTURE_REQUIRED/);assert.equal(calls.length,0);
 navigator.userActivation.isActive=true;await bridge.autoCalibrateTitle({x:0,y:0,width:2000,height:2000});
 assert.deepEqual(calls[0],['copilot:command','autoCalibrateTitle',undefined]);
});
