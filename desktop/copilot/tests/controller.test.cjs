'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source=path.join(__dirname,'..','window-controller.cjs');
test('a tested lifecycle controller exists',()=>assert.ok(fs.existsSync(source),'controller implementation is missing'));
const {WindowController}=fs.existsSync(source)?require(source):{};
function setup(){
 let now=0;const calls=[];
 const view={showInactive:()=>calls.push('showInactive'),hide:()=>calls.push('hide'),setBounds:r=>calls.push(['bounds',r])};
 const controller=new WindowController({view,ownPid:99,clock:()=>now,workArea:()=>({x:0,y:0,width:1600,height:900}),toDip:r=>r,onState:s=>calls.push(['state',s])});
 const frame={version:1,state:'bound',target:'abc',rect:{x:100,y:100,width:700,height:600},visible:true,minimized:false,foreground:true,foreground_pid:11,candidate_count:1};
 return {controller,calls,frame,setTime:n=>{now=n}};
}
test('passive follow shows inactive and does not contain focus action', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);assert.ok(calls.includes('showInactive'));assert.equal(controller.snapshot().state,'bound');
});
test('identical heartbeats do not move or show repeatedly', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);calls.length=0;controller.update(frame);assert.equal(calls.filter(c=>c==='showInactive'||Array.isArray(c)&&c[0]==='bounds').length,0);
});
test('minimize hides and restore does not activate', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);controller.update({...frame,minimized:true});assert.equal(calls.filter(c=>typeof c==='string').at(-1),'hide');controller.update(frame);assert.equal(calls.filter(c=>typeof c==='string').at(-1),'showInactive');
});
test('foreground of another application hides but own window stays', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);controller.update({...frame,foreground:false,foreground_pid:99});assert.notEqual(calls.filter(c=>typeof c==='string').at(-1),'hide');controller.update({...frame,foreground:false,foreground_pid:20});assert.equal(calls.filter(c=>typeof c==='string').at(-1),'hide');
});
test('helper timeout fails closed', {skip:!WindowController},()=>{
 const {controller,calls,frame,setTime}=setup();controller.update(frame);setTime(2101);controller.tick();assert.equal(calls.filter(c=>typeof c==='string').at(-1),'hide');assert.equal(controller.snapshot().state,'error');
});
test('invalid and ambiguous snapshots clear target identity', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);controller.update({bad:true});assert.equal(controller.snapshot().target,null);assert.equal(controller.snapshot().state,'error');assert.ok(calls.includes('hide'));
});
test('paused state hides and does not silently resume', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);controller.pause(true);controller.update(frame);assert.equal(calls.filter(c=>typeof c==='string').at(-1),'hide');assert.equal(controller.snapshot().paused,true);
});
test('near-edge user movement snaps actual bounds back even when the previous dock bounds are cached', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);
 const docked={...controller.lastBounds};calls.length=0;
 assert.equal(typeof controller.onUserMove,'function');
 controller.onUserMove({...docked,x:docked.x+12,y:docked.y+6});
 assert.equal(controller.mode,'dock');
 assert.deepEqual(calls.filter(c=>Array.isArray(c)&&c[0]==='bounds'),[['bounds',docked]]);
 calls.length=0;controller.onUserMove({...docked});controller.update(frame);
 assert.equal(calls.filter(c=>Array.isArray(c)&&c[0]==='bounds').length,0);
});
test('a user move away from either target edge stays floating on later target movement', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);
 const moved={...controller.lastBounds,x:1100,y:350};
 assert.equal(typeof controller.onUserMove,'function');
 controller.onUserMove(moved);calls.length=0;
 controller.update({...frame,rect:{...frame.rect,x:200,y:150}});
 assert.equal(controller.mode,'float');assert.deepEqual(controller.lastBounds,moved);
 assert.equal(calls.filter(c=>Array.isArray(c)&&c[0]==='bounds').length,0);
});
test('resuming waits for a fresh frame and discards frames arriving while paused', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);controller.pause(true);
 controller.update({...frame,target:'late-paused-target'});calls.length=0;
 controller.pause(false);
 assert.equal(controller.visible,false);assert.equal(controller.snapshot().target,null);
 assert.equal(calls.includes('showInactive'),false);
 controller.update({...frame,target:'fresh-target'});
 assert.equal(controller.visible,true);assert.equal(controller.snapshot().target,'fresh-target');
 assert.equal(calls.filter(c=>c==='showInactive').length,1);
});
test('selecting float captures the current position instead of continuing to follow', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.update(frame);
 const initial={...controller.lastBounds};controller.setMode('float');calls.length=0;
 controller.update({...frame,rect:{...frame.rect,x:200,y:150}});
 assert.deepEqual(controller.lastBounds,initial);
 assert.equal(calls.filter(c=>Array.isArray(c)&&c[0]==='bounds').length,0);
});
test('float selected before binding freezes the first valid placement', {skip:!WindowController},()=>{
 const {controller,calls,frame}=setup();controller.setMode('float');controller.update(frame);
 const initial={...controller.lastBounds};calls.length=0;
 controller.update({...frame,rect:{...frame.rect,x:200,y:150}});
 assert.deepEqual(controller.lastBounds,initial);
 assert.equal(calls.filter(c=>Array.isArray(c)&&c[0]==='bounds').length,0);
});
test('a page that is not ready cannot mark itself shown', {skip:!WindowController},()=>{
 let ready=false;let shows=0;
 const controller=new WindowController({view:{showInactive:()=>shows++,hide:()=>{},setBounds:()=>{}},ownPid:99,
  ready:()=>ready,workArea:()=>({x:0,y:0,width:1600,height:900}),toDip:r=>r});
 const {frame}=setup();controller.update(frame);assert.equal(shows,0);assert.equal(controller.visible,false);
 ready=true;controller.render();assert.equal(shows,1);
});
test('floating panel stays on its own monitor when target changes monitors',()=>{
 const {controller,frame}=setup();
 controller.workArea=rect=>({x:rect.x>=1600?1600:0,y:0,width:1600,height:900});
 controller.update(frame);controller.setFloating({x:1000,y:200});
 controller.update({...frame,rect:{...frame.rect,x:1800}});
 assert.equal(controller.lastBounds.x,1000);
});
test('explicit presentation clears an old target and error and shows expanded without a foreground target',()=>{
 const {controller,calls,frame}=setup();controller.update(frame);controller.fail('WINDOW_WATCH_STOPPED');calls.length=0;
 controller.present({x:1600,y:0,width:1280,height:800});
 assert.equal(controller.snapshot().presentation,true);assert.equal(controller.snapshot().paused,true);
 assert.equal(controller.snapshot().target,null);assert.equal(controller.snapshot().error_code,null);
 assert.equal(controller.snapshot().size,'expanded');assert.equal(controller.snapshot().effectiveSize,'expanded');
 assert.equal(controller.frame,null);assert.equal(controller.visible,true);assert.equal(calls.includes('showInactive'),true);
 assert.deepEqual(controller.lastBounds,{x:2030,y:40,width:420,height:720});
 controller.update({...frame,target:'ignored-while-presented'});
 assert.equal(controller.snapshot().target,null);assert.equal(controller.visible,true);
});
test('presentation clamps expanded bounds to a small display and waits for page readiness',()=>{
 const {controller,calls}=setup();let ready=false;controller.ready=()=>ready;
 controller.present({x:-320,y:20,width:320,height:600});
 assert.equal(controller.visible,false);assert.equal(calls.includes('showInactive'),false);
 ready=true;controller.render();
 assert.deepEqual(controller.lastBounds,{x:-320,y:20,width:320,height:600});assert.equal(controller.visible,true);
});
test('resume exits presentation and stays hidden until a fresh eligible target arrives',()=>{
 const {controller,calls,frame}=setup();controller.present({x:0,y:0,width:1600,height:900});
 controller.pause(false);calls.length=0;
 assert.equal(controller.snapshot().presentation,false);assert.equal(controller.snapshot().paused,false);
 assert.equal(controller.snapshot().target,null);assert.equal(controller.visible,false);
 controller.update({...frame,foreground:false,foreground_pid:20});assert.equal(controller.visible,false);
 controller.update(frame);assert.equal(controller.visible,true);assert.equal(calls.includes('showInactive'),true);
});
