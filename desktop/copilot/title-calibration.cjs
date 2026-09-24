'use strict';
const {validCalibration}=require('./title-observer.cjs');
const SHORTCUT='CommandOrControl+Alt+F8';
class TitleCalibration {
  constructor({shortcuts,target,cursor,notify,save}){Object.assign(this,{shortcuts,target,cursor,notify,save});this.active=false;this.first=null;this.timer=null}
  cancel(reason='TITLE_CALIBRATION_CANCELLED'){
    clearTimeout(this.timer);this.timer=null;if(this.active)this.shortcuts.unregister(SHORTCUT);
    this.active=false;this.first=null;this.notify(reason);
  }
  toggle(){
    if(this.active){this.cancel();return}
    if(!this.target()){this.notify('TITLE_WINDOW_UNAVAILABLE');return}
    if(!this.shortcuts.register(SHORTCUT,()=>this.mark())){this.notify('TITLE_SHORTCUT_UNAVAILABLE');return}
    this.active=true;this.first=null;this.notify('TITLE_MARK_TOP_LEFT');
    this.timer=setTimeout(()=>this.cancel('TITLE_CALIBRATION_TIMEOUT'),60000);this.timer.unref?.();
  }
  mark(){
    if(!this.active)return;
    const target=this.target();if(!target){this.cancel('TITLE_WINDOW_UNAVAILABLE');return}
    const point=this.cursor(),relative={x:point.x-target.x,y:point.y-target.y};
    if(!this.first){
      if(relative.x<80||relative.x>target.width-40||relative.y<4||relative.y>120){this.cancel('TITLE_ROI_INVALID');return}
      this.first={...relative,id:target.id,width:target.width,height:target.height};this.notify('TITLE_MARK_BOTTOM_RIGHT');return;
    }
    const first=this.first;
    if(target.id!==first.id||target.width!==first.width||target.height!==first.height){this.cancel('TITLE_WINDOW_CHANGED');return}
    const region={x:first.x,y:first.y,width:relative.x-first.x,height:relative.y-first.y};
    if(!validCalibration(region)||relative.x>target.width-8){this.cancel('TITLE_ROI_INVALID');return}
    try{this.save(region);this.cancel('TITLE_CALIBRATION_SAVED')}catch{this.cancel('TITLE_SAVE_FAILED')}
  }
}
module.exports={TitleCalibration};
