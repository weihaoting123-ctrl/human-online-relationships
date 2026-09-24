'use strict';
const {placePanel,nearEdge,validFrame} = require('./placement.cjs');
const SIZES = Object.freeze({expanded:{width:420,height:720},compact:{width:360,height:340},bubble:{width:156,height:48}});

class WindowController {
  constructor({view, ownPid, clock=Date.now, workArea, toDip, ready=()=>true, onState=()=>{}}) {
    this.view=view; this.ownPid=ownPid; this.clock=clock; this.workArea=workArea; this.toDip=toDip; this.onState=onState; this.ready=ready;
    this.frame=null; this.lastSeen=0; this.mode='dock'; this.size='compact'; this.effectiveSize='compact';
    this.paused=false; this.presentation=false; this.presentationArea=null;
    this.visible=false; this.lastBounds=null; this.floating=null; this.allowOverlap=false;
    this.error=null; this.lastNotice='';
  }
  snapshot() {
    return {state:this.error?'error':this.frame?.state||'waiting',target:this.frame?.state==='bound'?this.frame.target:null,
      mode:this.mode,size:this.size,effectiveSize:this.effectiveSize,paused:this.paused,presentation:this.presentation,error_code:this.error};
  }
  emit() { const notice=JSON.stringify(this.snapshot());if(notice!==this.lastNotice){this.lastNotice=notice;this.onState(this.snapshot())} }
  hide() { if(this.visible){this.view.hide();this.visible=false} }
  fail(code='WINDOW_UNAVAILABLE') {this.error=code;this.frame=null;this.hide();this.emit()}
  update(frame) {
    if(this.paused)return;
    if(!validFrame(frame)){this.fail('WINDOW_PROTOCOL_ERROR');return}
    this.lastSeen=this.clock();this.error=null;this.frame=frame;this.render();
  }
  tick() {if(this.frame && this.clock()-this.lastSeen>2000)this.fail('WINDOW_WATCH_TIMEOUT')}
  setMode(mode) {
    if(!['dock','float'].includes(mode))throw new TypeError('Invalid window mode');
    if(mode==='float'&&this.lastBounds)this.floating={x:this.lastBounds.x,y:this.lastBounds.y};
    this.mode=mode;this.render();
  }
  setSize(size) {
    if(!Object.hasOwn(SIZES,size))throw new TypeError('Invalid panel size');
    this.size=size;this.allowOverlap=size==='expanded';this.render();
  }
  pause(value) {
    if(typeof value!=='boolean')throw new TypeError('Invalid pause state');
    if(value!==this.paused){this.frame=null;this.lastSeen=0}
    if(!value){this.presentation=false;this.presentationArea=null}
    this.paused=value;this.render();
  }
  present(workArea) {
    this.paused=true;this.presentation=true;this.presentationArea={...workArea};
    this.frame=null;this.lastSeen=0;this.error=null;this.size='expanded';this.allowOverlap=true;
    this.render();
  }
  setFloating(bounds) {this.floating={x:bounds.x,y:bounds.y};this.lastBounds=null;this.mode='float';this.render()}
  onUserMove(bounds) {
    if(this.paused||this.error||this.frame?.state!=='bound'
      ||JSON.stringify(bounds)===JSON.stringify(this.lastBounds))return;
    try {
      const target=this.toDip(this.frame.rect);
      // The window has already moved. Cache its actual position so rendering
      // can restore a dock position even when that was the previous command.
      this.lastBounds={...bounds};
      this.mode=nearEdge(bounds,target)?'dock':'float';
      if(this.mode==='float')this.floating={x:bounds.x,y:bounds.y};
      this.render();
    } catch {this.fail('WINDOW_GEOMETRY_ERROR')}
  }
  render() {
    if(this.presentation&&this.ready()&&!this.error){
      try{
        const area=this.presentationArea;
        const placement=placePanel({target:area,workArea:area,size:SIZES[this.size],mode:'float',
          floating:{x:area.x+(area.width-SIZES[this.size].width)/2,y:area.y+(area.height-SIZES[this.size].height)/2}});
        this.effectiveSize=this.size;
        const bounds={x:placement.x,y:placement.y,width:placement.width,height:placement.height};
        if(JSON.stringify(bounds)!==JSON.stringify(this.lastBounds)){this.view.setBounds(bounds);this.lastBounds=bounds}
        if(!this.visible){this.view.showInactive();this.visible=true}
        this.emit();return;
      }catch{this.fail('WINDOW_GEOMETRY_ERROR');return}
    }
    const frame=this.frame;
    if(!this.ready()||this.paused||this.error||!frame||frame.state!=='bound'||!frame.visible||frame.minimized
      ||(!frame.foreground&&frame.foreground_pid!==this.ownPid)){this.hide();this.emit();return}
    try {
      const target=this.toDip(frame.rect);
      const area=this.workArea(this.mode==='float'&&this.floating?{...this.floating,...SIZES[this.size]}:target);
      const placement=placePanel({target,workArea:area,size:SIZES[this.size],mode:this.mode,floating:this.floating,allowOverlap:this.allowOverlap});
      this.effectiveSize=placement.collapsed?'bubble':this.size;
      const bounds={x:placement.x,y:placement.y,width:placement.width,height:placement.height};
      if(this.mode==='float')this.floating={x:bounds.x,y:bounds.y};
      if(JSON.stringify(bounds)!==JSON.stringify(this.lastBounds)){this.view.setBounds(bounds);this.lastBounds=bounds}
      if(!this.visible){this.view.showInactive();this.visible=true}
      this.emit();
    } catch {this.fail('WINDOW_GEOMETRY_ERROR')}
  }
}
module.exports={WindowController,SIZES};
