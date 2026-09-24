'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {EventEmitter}=require('node:events');const {PassThrough}=require('node:stream');
const filename=path.join(__dirname,'../title-reader.cjs');
test('supervised title reader exists',()=>assert.ok(fs.existsSync(filename)));
if(fs.existsSync(filename)){
  const {TitleReader}=require(filename);
  const {TitleObserver}=require('../title-observer.cjs');
  function fixture(timeout=100){let spawns=0,kills=0;const child=new EventEmitter();child.stdout=new PassThrough();child.stdin=new PassThrough();child.exitCode=null;child.kill=()=>kills++;
    const observer=new TitleObserver();const reader=new TitleReader({root:'/synthetic',parentPid:123,observer,spawn:()=>{spawns++;return child},timeout});
    return {reader,observer,child,counts:()=>({spawns,kills})};}
  test('missing/invalid region never starts capture',async()=>{const f=fixture();assert.equal((await f.reader.sample()).state,'unavailable');assert.equal(f.counts().spawns,0);assert.throws(()=>f.reader.setRegion({x:0,y:0,width:2000,height:1000}));});
  test('one pending capture shared, title needs stable frames',async()=>{const f=fixture();f.reader.setRegion({x:300,y:25,width:200,height:35});
    const a=f.reader.sample(),b=f.reader.sample();assert.equal(f.counts().spawns,1);
    f.child.stdout.write(JSON.stringify({state:'observed',source:'local_ocr',target:'test-window',title:'TEST FRIEND'})+'\n');
    assert.equal((await a).state,'unavailable');assert.equal((await b).state,'unavailable');
    const c=f.reader.sample();f.child.stdout.write(JSON.stringify({state:'observed',source:'local_ocr',target:'test-window',title:'TEST FRIEND'})+'\n');
    assert.equal((await c).title,'TEST FRIEND');f.reader.stop();});
  test('timeout kills only owned worker and resolves unavailable',async()=>{const f=fixture(15);f.reader.setRegion({x:300,y:25,width:200,height:35});assert.equal((await f.reader.sample()).state,'unavailable');assert.equal(f.counts().kills,1);});
  test('stopping rejects late private frames',async()=>{const f=fixture();f.reader.setRegion({x:300,y:25,width:200,height:35});const p=f.reader.sample();f.reader.stop('TITLE_PAUSED');await p;
    f.child.stdout.write(JSON.stringify({state:'observed',source:'local_ocr',target:'test-window',title:'LATE'})+'\n');assert.equal(f.observer.snapshot().title,'');});
}
