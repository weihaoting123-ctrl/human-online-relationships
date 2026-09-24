'use strict';
const path=require('node:path');
function launchOptions(args) {
  let port=8765,show=false,hasPort=false;
  for(const arg of args){
    if(arg==='--show'&&!show){show=true;continue}
    const match=/^--port=(\d{4,5})$/.exec(arg);
    if(!match||hasPort)throw new TypeError('Unsupported desktop option');
    port=Number(match[1]);hasPort=true;
  }
  if(!Number.isInteger(port)||port<1024||port>65535)throw new TypeError('Invalid local port');
  return {port,show,url:`http://127.0.0.1:${port}/copilot/index.html`};
}
function presentationRequest(args,{executable,appPath,cwd,port}){
  try{
    if(!Array.isArray(args)||args.length<3||args.some(arg=>typeof arg!=='string'))return false;
    const resolved=value=>path.win32.resolve(cwd,value).toLowerCase();
    if(resolved(args[0])!==resolved(executable))return false;
    let launchArgs;
    if(resolved(args[1])===resolved(appPath))launchArgs=args.slice(2);
    else{
      // Electron on Windows moves the app path behind its fixed source-app-id
      // marker and inserts these runtime switches before delivering this event.
      if(args.at(-2)!=='--source-app-id'||resolved(args.at(-1))!==resolved(appPath))return false;
      const switches=new Set(['--allow-file-access-from-files','--disable-http-cache']);
      launchArgs=args.slice(1,-2).filter(arg=>!switches.delete(arg));
      if(switches.size)return false;
    }
    const requested=launchOptions(launchArgs);
    return requested.show&&requested.port===port;
  }catch{return false}
}
function trustedSender(event,contents,url) {
  return event.sender===contents&&event.senderFrame===contents.mainFrame&&event.senderFrame?.url===url;
}
function windowCommand(command,value) {
  const valid=(command==='mode'&&['dock','float'].includes(value))
    ||(command==='size'&&['expanded','compact','bubble'].includes(value))
    ||(['pause','edit','recognition'].includes(command)&&typeof value==='boolean')
    ||(command==='copy'&&typeof value==='string'&&value.length>0&&Array.from(value).length<=800&&!/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value))
    ||(['close','state','conversationState','refreshConversation','calibrateTitle','autoCalibrateTitle'].includes(command)&&value===undefined);
  if(!valid)throw new TypeError('Invalid native command');
  return {command,value};
}
function allowedResource(details,url){
  try{
    if(details.resourceType==='subFrame')return false;
    if(details.resourceType==='mainFrame')return details.url===url;
    return new URL(details.url).origin===new URL(url).origin;
  }catch{return false}
}
module.exports={launchOptions,presentationRequest,trustedSender,windowCommand,allowedResource};
