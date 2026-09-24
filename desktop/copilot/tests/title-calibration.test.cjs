'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const filename=path.join(__dirname,'../title-calibration.cjs');
test('header calibration exists',()=>assert.ok(fs.existsSync(filename)));
if(fs.existsSync(filename)){
 const {TitleCalibration}=require(filename);
 function fixture(){let callback,point={x:400,y:225},target={id:'A',x:100,y:200,width:1000,height:800},saved;
 const codes=[];let unregistered=0;
 const calibration=new TitleCalibration({shortcuts:{register:(_key,fn)=>{callback=fn;return true},unregister:()=>unregistered++},target:()=>target,cursor:()=>point,notify:code=>codes.push(code),save:roi=>saved=roi});
 return {calibration,codes,press:()=>callback(),point:p=>point=p,target:t=>target=t,saved:()=>saved,unregistered:()=>unregistered};}
 test('two hotkey marks save only relative numeric header bounds',()=>{const f=fixture();f.calibration.toggle();f.press();f.point({x:650,y:265});f.press();assert.deepEqual(f.saved(),{x:300,y:25,width:250,height:40});assert.equal(f.calibration.active,false);assert.ok(f.unregistered()>0);});
 test('target changes or out-of-header marks never save',()=>{const f=fixture();f.calibration.toggle();f.press();f.target({id:'B',x:100,y:200,width:1000,height:800});f.press();assert.equal(f.saved(),undefined);assert.equal(f.calibration.active,false);
 const g=fixture();g.calibration.toggle();g.point({x:400,y:600});g.press();assert.equal(g.saved(),undefined);g.calibration.cancel();});
 test('second click cancels temporary global shortcut',()=>{const f=fixture();f.calibration.toggle();f.calibration.toggle();assert.equal(f.calibration.active,false);assert.equal(f.saved(),undefined);assert.ok(f.unregistered()>0);});
}
