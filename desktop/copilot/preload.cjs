'use strict';
const {contextBridge,ipcRenderer}=require('electron');
const invoke=(command,value)=>ipcRenderer.invoke('copilot:command',command,value);
contextBridge.exposeInMainWorld('copilotDesktop',Object.freeze({
  setMode:mode=>invoke('mode',mode),
  setSize:size=>invoke('size',size),
  pause:value=>invoke('pause',value),
  edit:value=>invoke('edit',value),
  copy:text=>{
    if(!navigator.userActivation.isActive)return Promise.reject(new Error('COPILOT_USER_GESTURE_REQUIRED'));
    return invoke('copy',text);
  },
  close:()=>invoke('close'),
  state:()=>invoke('state'),
  conversationState:()=>invoke('conversationState'),
  refreshConversation:()=>invoke('refreshConversation'),
  setRecognition:value=>invoke('recognition',value),
  calibrateTitle:()=>{
    if(!navigator.userActivation.isActive)return Promise.reject(new Error('COPILOT_USER_GESTURE_REQUIRED'));
    return invoke('calibrateTitle');
  },
  onConversation:callback=>{
    if(typeof callback!=='function')throw new TypeError('A callback is required');
    const listener=(_event,state)=>callback(state);
    ipcRenderer.on('copilot:conversation',listener);
    return ()=>ipcRenderer.removeListener('copilot:conversation',listener);
  },
  onState:callback=>{
    if(typeof callback!=='function')throw new TypeError('A callback is required');
    const listener=(_event,state)=>callback(state);
    ipcRenderer.on('copilot:state',listener);
    return ()=>ipcRenderer.removeListener('copilot:state',listener);
  }
}));
