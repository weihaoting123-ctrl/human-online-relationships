'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = path.join(__dirname, '..', 'placement.cjs');
test('a real independent positioning module exists', () => assert.ok(fs.existsSync(source), 'positioning implementation is missing'));
const {placePanel, nearEdge, validFrame} = fs.existsSync(source) ? require(source) : {};
const target = {x:100,y:100,width:700,height:650};
const workArea = {x:0,y:0,width:1440,height:900};
test('docks right without covering target', {skip:!placePanel}, () => {
  assert.deepEqual(placePanel({target,workArea,size:{width:380,height:560}}), {x:808,y:100,width:380,height:560,side:'right',collapsed:false});
});
test('docks left if right side has insufficient space', {skip:!placePanel}, () => {
  const result=placePanel({target:{...target,x:550},workArea,size:{width:380,height:560}});
  assert.equal(result.x,162);assert.equal(result.side,'left');
});
test('both edges tight produces small capsule, not a full overlapping panel', {skip:!placePanel}, () => {
  const result=placePanel({target:{x:0,y:0,width:1440,height:900},workArea,size:{width:380,height:560}});
  assert.equal(result.collapsed,true);assert.ok(result.width<=160);assert.ok(result.height<=56);assert.ok(result.y<300);
});
test('user can explicitly expand even on narrow monitor', {skip:!placePanel}, () => {
  const result=placePanel({target:{x:0,y:0,width:640,height:600},workArea:{x:0,y:0,width:640,height:600},size:{width:380,height:560},allowOverlap:true});
  assert.equal(result.collapsed,false);assert.equal(result.width,380);assert.ok(result.x>=0);assert.ok(result.y+result.height<=600);
});
test('negative monitor coordinates are preserved', {skip:!placePanel}, () => {
  const result=placePanel({target:{x:-1500,y:80,width:700,height:650},workArea:{x:-1920,y:0,width:1920,height:1080},size:{width:380,height:560}});
  assert.equal(result.x,-792);assert.equal(result.y,80);
});
test('free floating coordinates are clamped to work area', {skip:!placePanel}, () => {
  const result=placePanel({target,workArea,size:{width:380,height:560},mode:'float',floating:{x:2000,y:1000}});
  assert.equal(result.x,1060);assert.equal(result.y,340);
});
test('invalid sizes are refused rather than moved off screen', {skip:!placePanel}, () => {
  assert.throws(()=>placePanel({target,workArea,size:{width:NaN,height:20}}));
});
test('near-edge detection uses vertical overlap and snap distance', {skip:!nearEdge}, () => {
  assert.equal(nearEdge({x:815,y:100,width:300,height:500},target),true);
  assert.equal(nearEdge({x:815,y:900,width:300,height:500},target),false);
  assert.equal(nearEdge({x:400,y:100,width:300,height:500},target),false);
});
test('only typed bounded protocol snapshots are accepted', {skip:!validFrame}, () => {
  const frame={version:1,state:'bound',target:'opaque',rect:target,visible:true,minimized:false,foreground:true,foreground_pid:12,candidate_count:1};
  assert.equal(validFrame(frame),true);
  for(const bad of [{...frame,visible:1},{...frame,rect:{...target,width:Infinity}},{...frame,target:'x'.repeat(200)},{...frame,version:2}])assert.equal(validFrame(bad),false);
});
