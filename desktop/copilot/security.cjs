'use strict';
function launchOptions(args) {
  let port=8765;
  if(args.length>1)throw new TypeError('Unsupported desktop option');
  if(args.length){const match=/^--port=(\d{4,5})$/.exec(args[0]);if(!match)throw new TypeError('Unsupported desktop option');port=Number(match[1])}
  if(!Number.isInteger(port)||port<1024||port>65535)throw new TypeError('Invalid local port');
  return {port,url:`http://127.0.0.1:${port}/copilot/index.html`};
}
function trustedSender(event,contents,url) {
  return event.sender===contents&&event.senderFrame===contents.mainFrame&&event.senderFrame?.url===url;
}
function windowCommand(command,value) {
  const valid=(command==='mode'&&['dock','float'].includes(value))
    ||(command==='size'&&['expanded','compact','bubble'].includes(value))
    ||(['pause','edit'].includes(command)&&typeof value==='boolean')
    ||(command==='copy'&&typeof value==='string'&&value.length>0&&Array.from(value).length<=800&&!/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value))
    ||(['close','state'].includes(command)&&value===undefined);
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
module.exports={launchOptions,trustedSender,windowCommand,allowedResource};
