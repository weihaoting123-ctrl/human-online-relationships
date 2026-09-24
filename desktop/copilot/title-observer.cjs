'use strict';
const REGION_PROFILE='wechat-4.1.13-visual-v2';
class TitleObserver {
  constructor({clock=Date.now,onState=()=>{}}={}){
    this.clock=clock;this.onState=onState;this.pending='';this.lastSeen=0;
    this.value={state:'unavailable',title:'',source:'local_ocr',target:null,seq:1,reason:'TITLE_WAITING'};
  }
  publish(value){this.value={...value,source:'local_ocr',seq:this.value.seq+1};this.onState(this.snapshot());return this.snapshot()}
  accept(frame){
    this.lastSeen=this.clock();
    const title=frame?.title;
    if(frame?.state!=='observed'||frame.source!=='local_ocr'||typeof title!=='string'
      ||title.length<1||Array.from(title).length>200||title!==title.trim()
      ||/[\p{C}\u2028\u2029\u2026\u22ef]|\.{3}/u.test(title)
      ||typeof frame.target!=='string'||!/^[-a-zA-Z0-9:_]{1,128}$/.test(frame.target)){
      return this.clear(frame?.reason==='TITLE_CALIBRATION_REQUIRED'?'TITLE_CALIBRATION_REQUIRED':'TITLE_UNAVAILABLE');
    }
    const key=JSON.stringify([frame.target,title]);
    if(this.pending!==key){this.pending=key;return this.publish({state:'unavailable',title:'',target:frame.target,reason:'TITLE_STABILIZING'})}
    return this.publish({state:'observed',title,target:frame.target,reason:'TITLE_UNVERIFIED'});
  }
  snapshot(){return {...this.value}}
  clear(reason='TITLE_UNAVAILABLE'){this.pending='';return this.publish({state:'unavailable',title:'',target:null,reason})}
  tick(){if(this.value.state==='observed'&&this.clock()-this.lastSeen>4000)this.clear('TITLE_STALE')}
}
function validCalibration(roi){
  return !!roi&&Object.keys(roi).sort().join(',')==='height,width,x,y'
    &&Object.values(roi).every(Number.isFinite)&&roi.x>=80&&roi.x<=1800&&roi.y>=4
    &&roi.width>=40&&roi.width<=480&&roi.height>=10&&roi.height<=48&&roi.y+roi.height<=130;
}
function storedCalibration(saved){
  if(!saved||Object.keys(saved).sort().join(',')!=='profile,region'
    ||saved.profile!==REGION_PROFILE||!validCalibration(saved.region))return null;
  return {...saved.region};
}
module.exports={TitleObserver,validCalibration,storedCalibration,REGION_PROFILE};
