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
