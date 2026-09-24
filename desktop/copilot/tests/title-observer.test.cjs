'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const filename=path.join(__dirname,'../title-observer.cjs');
test('local title observer exists',()=>assert.ok(fs.existsSync(filename)));
if(fs.existsSync(filename)){
  const {TitleObserver,validCalibration,storedCalibration,REGION_PROFILE}=require(filename);
  const frame=title=>({state:'observed',title,target:'synthetic-window',source:'local_ocr'});
  test('stable title requires two observations; change immediately clears',()=>{
    const observer=new TitleObserver();
    observer.accept(frame('Example A'));assert.equal(observer.snapshot().state,'unavailable');
    observer.accept(frame('Example A'));assert.equal(observer.snapshot().title,'Example A');
    observer.accept(frame('Example B'));assert.equal(observer.snapshot().state,'unavailable');
    assert.equal(observer.snapshot().title,'');
    observer.accept(frame('Example B'));assert.equal(observer.snapshot().title,'Example B');
  });
  test('heartbeat sequence advances without losing stable label',()=>{
    const observer=new TitleObserver();observer.accept(frame('Example'));observer.accept(frame('Example'));
    const before=observer.snapshot();observer.accept(frame('Example'));const after=observer.snapshot();
    assert.ok(after.seq>before.seq);assert.equal(after.title,before.title);
  });
  test('A B A must stabilize again, malformed/control/ellipsis never observed',()=>{
    const observer=new TitleObserver();observer.accept(frame('A'));observer.accept(frame('A'));
    observer.accept(frame('B'));observer.accept(frame('A'));assert.equal(observer.snapshot().state,'unavailable');
    for(const title of ['', 'Example…', 'bad\nname', 'a'.repeat(201), 'bad\u202ename', 'bad\u200bname', 'bad\u22efname']){
      observer.accept(frame(title));observer.accept(frame(title));assert.equal(observer.snapshot().state,'unavailable');
    }
  });
  test('expiry and explicit clear erase private text and require restabilizing',()=>{
    let now=0;const observer=new TitleObserver({clock:()=>now});
    observer.accept(frame('Example'));observer.accept(frame('Example'));now=5000;observer.tick();
    assert.equal(observer.snapshot().state,'unavailable');assert.equal(observer.snapshot().title,'');
    observer.accept(frame('Example'));assert.equal(observer.snapshot().state,'unavailable');
    observer.accept(frame('Example'));observer.clear('TITLE_PAUSED');assert.equal(observer.snapshot().title,'');
  });
  test('calibration only permits a bounded top-header rectangle',()=>{
    assert.ok(validCalibration({x:300,y:25,width:250,height:40}));
    for(const roi of [{x:0,y:0,width:2000,height:1000},{x:100,y:200,width:200,height:40},{x:300,y:25,width:250,height:NaN},{x:300,y:25,width:250,height:40,extra:1}])assert.equal(validCalibration(roi),false);
  });
  test('stored calibration requires the explicit visual-frame coordinate profile',()=>{
    const region={x:300,y:25,width:250,height:40};
    assert.equal(REGION_PROFILE,'wechat-4.1.13-visual-v2');
    assert.equal(storedCalibration({profile:'wechat-4.1.13',region}),null);
    assert.equal(storedCalibration({profile:'unknown',region}),null);
    assert.deepEqual(storedCalibration({profile:REGION_PROFILE,region}),region);
    const restored=storedCalibration({profile:REGION_PROFILE,region});
    restored.x=400;assert.equal(region.x,300);
  });
  test('stored calibration rejects invalid or non-numeric preferences',()=>{
    const region={x:300,y:25,width:250,height:40};
    for(const saved of [null,[],{}, {profile:REGION_PROFILE},
      {profile:REGION_PROFILE,region:{...region,x:'300'}},
      {profile:REGION_PROFILE,region:{...region,title:'Synthetic'}},
      {profile:REGION_PROFILE,region,title:'Synthetic'},
      {profile:REGION_PROFILE,region:{...region,y:200}}])assert.equal(storedCalibration(saved),null);
  });
}
