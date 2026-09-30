'use strict';
const {validFrame}=require('./placement.cjs');

// A temporary focus reason is not identity proof or permission to capture.
// Native watcher maps a non-active input desktop to hidden / foreground PID 0.
function inactiveTitleReason({controller,pageReady,recognitionEnabled,expectedTarget,ownPid,now}){
  if(controller?.paused===true)return 'TITLE_PAUSED';
  const frame=controller?.frame;
  const seen=controller?.lastSeen;
  if(pageReady!==true||recognitionEnabled!==true||controller?.paused!==false||controller.error
    ||!validFrame(frame)||frame.state!=='bound'||frame.visible!==true||frame.minimized!==false
    ||typeof expectedTarget!=='string'||!expectedTarget||frame.target!==expectedTarget
    ||frame.foreground!==false||!Number.isSafeInteger(ownPid)||ownPid<=0
    ||frame.foreground_pid<=0||frame.foreground_pid===ownPid
    ||!Number.isFinite(now)||!Number.isFinite(seen)||seen<0||now<seen||now-seen>2000){
    return 'TITLE_INACTIVE';
  }
  return 'TITLE_FOCUS_LOST';
}

module.exports={inactiveTitleReason};
