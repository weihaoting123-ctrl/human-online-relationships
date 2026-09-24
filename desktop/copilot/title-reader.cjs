'use strict';
const path=require('node:path');
const {spawn:systemSpawn}=require('node:child_process');
const {validCalibration}=require('./title-observer.cjs');
class TitleReader {
  constructor({root,parentPid,observer,spawn=systemSpawn,timeout=5000}){
    Object.assign(this,{root,parentPid,observer,spawn,timeout});this.region=null;this.child=null;this.pending=null;this.retryAfter=0;
  }
  setRegion(region){if(!validCalibration(region))throw new TypeError('TITLE_ROI_INVALID');this.stop();this.region={...region}}
  finish(frame){const pending=this.pending;this.pending=null;if(pending){clearTimeout(pending.timer);pending.resolve(frame)}}
  stop(reason='TITLE_UNAVAILABLE'){
    const child=this.child;this.child=null;
    if(child){child.stdin.end();if(child.exitCode===null)child.kill()}
    this.finish(this.observer.clear(reason));
  }
  start(){
    if(this.child)return true;
    if(Date.now()<this.retryAfter)return false;
    const exe=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
    try{
      const child=this.spawn(exe,['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',path.join(this.root,'scripts','copilot_title_read.ps1'),'-ParentPid',String(this.parentPid)],{cwd:this.root,windowsHide:true,stdio:['pipe','pipe','ignore'],shell:false});
      this.child=child;let buffer='';child.stdout.setEncoding('utf8');
      child.stdout.on('data',chunk=>{
        if(this.child!==child)return;
        buffer+=chunk;if(buffer.length>4096){this.stop('TITLE_PROTOCOL_ERROR');return}
        let newline;
        while((newline=buffer.indexOf('\n'))!==-1){
          const line=buffer.slice(0,newline).trim();buffer=buffer.slice(newline+1);
          if(!line)continue;
          if(!this.pending){this.stop('TITLE_PROTOCOL_ERROR');return}
          try{this.finish(this.observer.accept(JSON.parse(line)))}catch{this.stop('TITLE_PROTOCOL_ERROR');return}
        }
      });
      const failed=()=>{if(this.child===child){this.retryAfter=Date.now()+5000;this.stop('TITLE_READER_UNAVAILABLE')}};
      child.on('error',failed);child.on('exit',failed);child.stdin.on('error',failed);
      return true;
    }catch{this.retryAfter=Date.now()+5000;this.stop('TITLE_READER_UNAVAILABLE');return false}
  }
  sample(){
    if(!this.region)return Promise.resolve(this.observer.clear('TITLE_CALIBRATION_REQUIRED'));
    if(this.pending)return this.pending.promise;
    if(!this.start())return Promise.resolve(this.observer.clear('TITLE_READER_UNAVAILABLE'));
    let resolve;const promise=new Promise(done=>{resolve=done});
    const timer=setTimeout(()=>{this.retryAfter=Date.now()+5000;this.stop('TITLE_READER_TIMEOUT')},this.timeout);
    this.pending={promise,resolve,timer};
    try{this.child.stdin.write(JSON.stringify(this.region)+'\n')}catch{this.stop('TITLE_READER_UNAVAILABLE')}
    return promise;
  }
  async refresh(){if(this.pending)await this.pending.promise;return this.sample()}
}
module.exports={TitleReader};
