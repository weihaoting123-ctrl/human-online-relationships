'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const filename=path.join(__dirname,'../auto-calibration.cjs');
test('automatic calibration target guard exists',()=>assert.ok(fs.existsSync(filename)));
if(fs.existsSync(filename)){
 const {AutoCalibration}=require(filename);
 function fixture(){let target={id:'synthetic',x:10,y:20,width:1000,height:700},resolve;const saved=[],reasons=[];
  const calibration=new AutoCalibration({target:()=>target,read:()=>new Promise(done=>resolve=done),stop:reason=>reasons.push(reason),save:region=>saved.push(region)});
  return {calibration,saved,reasons,change:value=>target=value,done:value=>resolve(value)};
 }
 const result={state:'calibrated',region:{x:365,y:30,width:110,height:28}};
 test('stable single operation saves only numeric ROI',async()=>{const f=fixture();const p=f.calibration.run();assert.equal(f.calibration.active,true);f.done(result);await p;assert.deepEqual(f.saved,[result.region]);assert.equal(f.calibration.active,false);});
 test('switch/hidden/moved target and cancellation discard late success',async()=>{
  for(const target of [null,{id:'other',x:10,y:20,width:1000,height:700},{id:'synthetic',x:20,y:20,width:1000,height:700}]){
   const f=fixture(),p=f.calibration.run();f.change(target);f.calibration.check();f.done(result);await p;assert.deepEqual(f.saved,[]);
  }
  const f=fixture(),p=f.calibration.run();f.calibration.cancel();f.done(result);await p;assert.deepEqual(f.saved,[]);
 });
 test('unavailable start does not read and failed worker does not save',async()=>{const f=fixture();f.change(null);await f.calibration.run();assert.equal(f.calibration.active,false);assert.deepEqual(f.saved,[]);const g=fixture(),p=g.calibration.run();g.done({state:'unavailable',reason:'TITLE_OCCLUDED'});await p;assert.deepEqual(g.saved,[]);assert.equal(g.reasons.at(-1),'TITLE_OCCLUDED');});
 test('repeated action cancels instead of making a second request',async()=>{const f=fixture(),p=f.calibration.run();await f.calibration.run();f.done(result);await p;assert.deepEqual(f.saved,[]);});
 test('invalid geometry and save failures never report success',async()=>{const f=fixture(),p=f.calibration.run();f.done({...result,region:{...result.region,y:200}});await p;assert.deepEqual(f.saved,[]);assert.notEqual(f.reasons.at(-1),'TITLE_CALIBRATION_SAVED');
  const g=new AutoCalibration({target:()=>({id:'test',width:1000,height:700}),read:async()=>result,save:()=>{throw Error('disk')},stop:reason=>f.reasons.push(reason)});await g.run();assert.equal(f.reasons.at(-1),'TITLE_SAVE_FAILED');});
 test('geometry outside unchanged target is not saved',async()=>{const f=fixture(),p=f.calibration.run();f.done({...result,region:{x:950,y:30,width:110,height:28}});await p;assert.deepEqual(f.saved,[]);});
}
