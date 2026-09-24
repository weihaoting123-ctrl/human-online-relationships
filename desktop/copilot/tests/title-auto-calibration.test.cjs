'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');const {PassThrough}=require('node:stream');
const {TitleReader}=require('../title-reader.cjs');
const {TitleObserver}=require('../title-observer.cjs');
const region={x:365,y:30,width:110,height:28};
function fixture(){const child=new EventEmitter();child.stdout=new PassThrough();child.stdin=new PassThrough();child.exitCode=null;child.kill=()=>{};
 const observer=new TitleObserver(),reader=new TitleReader({root:'/synthetic',parentPid:123,observer,spawn:()=>child,timeout:50});return {reader,observer,child};}
test('explicit auto calibration returns only numeric geometry, never an observed title',async()=>{
 const f=fixture();assert.equal(typeof f.reader.calibrate,'function');
 const p=f.reader.calibrate();assert.equal(f.child.stdin.read().toString(),'{"operation":"calibrate"}\n');
 f.child.stdout.write(JSON.stringify({state:'calibrated',source:'local_ocr',target:'a'.repeat(64),region})+'\n');
 const result=await p;assert.equal(result.state,'calibrated');assert.deepEqual(result.region,region);
 assert.equal(f.observer.snapshot().title,'');assert.equal(f.reader.region,null);f.reader.stop();
});
test('auto calibration rejects text-bearing or malformed replies and preserves previous region',async()=>{
 for(const extra of [{title:'synthetic'},{region:{...region,y:200}},{target:'invalid'}]){
  const f=fixture();assert.equal(typeof f.reader.calibrate,'function');f.reader.setRegion(region);
  const p=f.reader.calibrate();f.child.stdout.write(JSON.stringify({state:'calibrated',source:'local_ocr',target:'a'.repeat(64),region,...extra})+'\n');
  assert.equal((await p).state,'unavailable');assert.deepEqual(f.reader.region,region);assert.equal(f.observer.snapshot().title,'');f.reader.stop();
 }
});
test('stopped automatic calibration cannot accept late geometry',async()=>{
 const f=fixture();assert.equal(typeof f.reader.calibrate,'function');const p=f.reader.calibrate();f.reader.stop('TITLE_PAUSED');
 assert.equal((await p).state,'unavailable');f.child.stdout.write(JSON.stringify({state:'calibrated',source:'local_ocr',target:'a'.repeat(64),region})+'\n');assert.equal(f.reader.region,null);
});
test('auto calibration times out without changing saved geometry',async()=>{
 const f=fixture();assert.equal(typeof f.reader.calibrate,'function');const value=await f.reader.calibrate();assert.equal(value.state,'unavailable');assert.equal(f.reader.region,null);
});
test('extra protocol lines reject the entire calibration reply',async()=>{
 const f=fixture(),p=f.reader.calibrate();f.child.stdout.write(JSON.stringify({state:'calibrated',source:'local_ocr',target:'a'.repeat(64),region})+'\n{}\n');
 assert.equal((await p).state,'unavailable');assert.equal(f.reader.region,null);f.reader.stop();
});
test('calibration preserves fixed capture failure codes without exposing arbitrary errors',async()=>{
 for(const reason of ['TITLE_DPI_UNAVAILABLE','TITLE_CAPTURE_UNAVAILABLE','private-synthetic-detail']){
  const f=fixture(),p=f.reader.calibrate();
  f.child.stdout.write(JSON.stringify({state:'unavailable',source:'local_ocr',reason})+'\n');
  assert.equal((await p).reason,reason.startsWith('TITLE_')?reason:'TITLE_AUTO_FAILED');
  assert.equal(f.reader.region,null);f.reader.stop();
 }
});
