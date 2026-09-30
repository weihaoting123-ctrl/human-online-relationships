'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const policyPath=path.join(__dirname,'../focus-policy.cjs');
const policy=fs.existsSync(policyPath)?require(policyPath):{};

function fixture(){
  return {pageReady:true,recognitionEnabled:true,expectedTarget:'synthetic-window',ownPid:99,now:12000,
    controller:{paused:false,error:null,lastSeen:11000,
      frame:{version:1,state:'bound',target:'synthetic-window',candidate_count:1,
        rect:{x:100,y:100,width:700,height:600},visible:true,minimized:false,
        foreground:false,foreground_pid:20}}};
}

test('main treats verified temporary focus loss separately from an inactive identity',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../main.cjs'),'utf8');
  const body=source.slice(source.indexOf('function sampleTitle('),source.indexOf('function setupTitleReader('));
  const settings=fixture();
  let state={state:'observed',target:'synthetic-ocr-target'};
  let samples=0;
  const context={...settings,process:{pid:settings.ownPid},Date:{now:()=>settings.now},
    lastSamplingTarget:settings.expectedTarget,titleAuto:null,titleCalibration:null,
    inactiveTitleReason:policy.inactiveTitleReason,
    titleTarget:()=>null,titleObserver:{snapshot:()=>({...state})},
    titleReader:{stop:reason=>{state={state:'unavailable',target:null,reason}},
      sample:()=>{samples++},refresh:()=>{samples++}}};
  vm.createContext(context);
  vm.runInContext(body,context);
  const result=await context.sampleTitle();
  assert.equal(result.reason,'TITLE_FOCUS_LOST');
  assert.equal(result.target,null);
  assert.equal(samples,0,'focus loss must not start title capture');
  assert.equal((await context.sampleTitle(true)).reason,'TITLE_FOCUS_LOST');
});

test('pure focus-loss policy is available',()=>assert.equal(typeof policy.inactiveTitleReason,'function'));

if(policy.inactiveTitleReason){
  const decide=policy.inactiveTitleReason;
  test('only a fresh visible same target with known other-app foreground gets focus loss',()=>{
    assert.equal(decide(fixture()),'TITLE_FOCUS_LOST');
    const f=fixture();f.now=f.controller.lastSeen+2000;
    assert.equal(decide(f),'TITLE_FOCUS_LOST');
  });
  test('lock screen, unknown foreground, own process and missing boolean flags fail closed',()=>{
    for(const changes of [
      {foreground_pid:0},{foreground_pid:-1},{foreground_pid:null},{foreground_pid:undefined},
      {foreground_pid:'20'},{foreground_pid:20.5},{foreground_pid:99},{foreground:true},
      {foreground:undefined},{visible:undefined},{visible:1},{minimized:undefined},
    ]){
      const f=fixture();Object.assign(f.controller.frame,changes);
      assert.equal(decide(f),'TITLE_INACTIVE',JSON.stringify(changes));
    }
  });
  test('target change, loss, ambiguity, invalid geometry and minimized states are not mere focus loss',()=>{
    for(const changes of [
      {target:'other-window'},{target:null},{target:''},{state:'waiting'},
      {state:'error'},{state:'ambiguous'},{candidate_count:2},{visible:false},{minimized:true},
      {rect:null},{version:2},
    ]){
      const f=fixture();Object.assign(f.controller.frame,changes);
      assert.equal(decide(f),'TITLE_INACTIVE',JSON.stringify(changes));
    }
    const f=fixture();f.controller.frame=null;
    assert.equal(decide(f),'TITLE_INACTIVE');
  });
  test('manual pause takes precedence and missing readiness or expected identity cannot preserve authorization',()=>{
    const paused=fixture();paused.controller.paused=true;
    assert.equal(decide(paused),'TITLE_PAUSED');
    for(const changes of [{pageReady:false},{pageReady:undefined},{recognitionEnabled:false},
      {recognitionEnabled:undefined},{expectedTarget:null},{expectedTarget:''},
      {ownPid:0},{ownPid:undefined},{controller:null}]){
      assert.equal(decide({...fixture(),...changes}),'TITLE_INACTIVE');
    }
    for(const changes of [{paused:undefined},{error:'WINDOW_PROTOCOL_ERROR'}]){
      const f=fixture();Object.assign(f.controller,changes);
      assert.equal(decide(f),'TITLE_INACTIVE');
    }
  });
  test('stale, missing, nonnumeric and future watcher times fail closed',()=>{
    for(const seen of [9999,12001,NaN,Infinity,undefined,null,'11000']){
      const f=fixture();f.controller.lastSeen=seen;
      assert.equal(decide(f),'TITLE_INACTIVE',String(seen));
    }
    for(const now of [undefined,null,NaN,Infinity,'12000']){
      assert.equal(decide({...fixture(),now}),'TITLE_INACTIVE',String(now));
    }
  });
}
