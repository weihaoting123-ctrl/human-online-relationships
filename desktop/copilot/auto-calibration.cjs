'use strict';
const {validCalibration}=require('./title-observer.cjs');
// A user-started, single local operation. Never retries or changes a module.
class AutoCalibration {
  constructor({target,read,stop,save}){Object.assign(this,{target,read,stop,save});this.active=false;this.generation=0;this.before=null}
  cancel(reason='TITLE_CALIBRATION_CANCELLED'){
    this.generation++;this.active=false;this.before=null;this.stop(reason);
  }
  check(){
    if(this.active&&JSON.stringify(this.target())!==this.before)this.cancel('TITLE_TARGET_CHANGED');
  }
  async run(){
    if(this.active){this.cancel();return}
    const target=this.target();if(!target){this.stop('TITLE_WINDOW_UNAVAILABLE');return}
    this.before=JSON.stringify(target);this.active=true;const generation=++this.generation;
    let result;
    try{result=await this.read()}catch{result={state:'unavailable',reason:'TITLE_AUTO_FAILED'}}
    this.check();
    if(!this.active||generation!==this.generation)return;
    this.active=false;this.before=null;
    if(result?.state!=='calibrated'||!validCalibration(result.region)
      ||result.region.x+result.region.width>target.width-8||result.region.y+result.region.height>target.height){
      this.stop(result?.reason||'TITLE_AUTO_FAILED');return;
    }
    try{this.save(result.region);this.stop('TITLE_CALIBRATION_SAVED')}catch{this.stop('TITLE_SAVE_FAILED')}
  }
}
module.exports={AutoCalibration};
