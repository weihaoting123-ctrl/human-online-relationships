'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');
const source=require('node:path').join(__dirname,'..','security.cjs');
test('desktop security policy exists',()=>assert.ok(fs.existsSync(source),'security policy missing'));
const {launchOptions,trustedSender,windowCommand}=fs.existsSync(source)?require(source):{};
test('launch destination is exact loopback path with bounded numeric port',{skip:!launchOptions},()=>{
 assert.equal(launchOptions([]).url,'http://127.0.0.1:8765/copilot/index.html');
 assert.equal(launchOptions(['--port=18765']).port,18765);
 for(const args of [['--port=80'],['--port=65536'],['--port=8765@evil'],['--url=https://example.com'],['--no-sandbox']])assert.throws(()=>launchOptions(args));
});
test('explicit show flag is bounded and does not change ordinary launches',()=>{
 assert.equal(launchOptions([]).show,false);
 assert.equal(launchOptions(['--show']).show,true);
 assert.deepEqual(launchOptions(['--port=18765','--show']),{port:18765,show:true,url:'http://127.0.0.1:18765/copilot/index.html'});
 assert.equal(launchOptions(['--show','--port=18765']).port,18765);
 for(const args of [['--show','--show'],['--port=8765','--port=8765'],['--port=8765','--port=18765'],['--show=true'],['--show','--url=https://example.com'],['--show','--no-sandbox']])assert.throws(()=>launchOptions(args));
});
test('second-instance presentation requires the same executable, app path and port',()=>{
 const {presentationRequest}=require(source);
 const expected={executable:'C:\\runtime\\electron.exe',appPath:'D:\\app\\desktop\\copilot',cwd:'D:\\app',port:18765};
 const command=['C:\\runtime\\electron.exe','D:\\app\\desktop\\copilot','--port=18765','--show'];
 assert.equal(presentationRequest(command,expected),true);
 assert.equal(presentationRequest([command[0],'.\\desktop\\copilot',...command.slice(2)],expected),true);
 assert.equal(presentationRequest(command.slice(0,-1),expected),false);
 assert.equal(presentationRequest([...command.slice(0,2),'--port=8765','--show'],expected),false);
 assert.equal(presentationRequest(['C:\\other\\electron.exe',...command.slice(1)],expected),false);
 assert.equal(presentationRequest([command[0],'D:\\other\\main.cjs',...command.slice(2)],expected),false);
 for(const suffix of [['--show'],['--unknown'],['--port=18765']])assert.equal(presentationRequest([...command,...suffix],expected),false);
 assert.equal(presentationRequest([],expected),false);
});
test('second-instance parser accepts only the observed Electron switch envelope',()=>{
 const {presentationRequest}=require(source);
 const expected={executable:'C:\\runtime\\electron.exe',appPath:'D:\\app\\desktop\\copilot',cwd:'D:\\app',port:18765};
 const command=[expected.executable,'--port=18765','--show','--allow-file-access-from-files','--disable-http-cache','--source-app-id',expected.appPath];
 assert.equal(presentationRequest(command,expected),true);
 assert.equal(presentationRequest([...command.slice(0,-1),'D:\\other\\app'],expected),false);
 for(const flag of ['--show','--no-sandbox','--allow-file-access-from-files','--disable-http-cache','--source-app-id']){
  assert.equal(presentationRequest([command[0],flag,...command.slice(1)],expected),false);
 }
});
test('only trusted top-level frame can use narrow native IPC',{skip:!trustedSender},()=>{
 const frame={url:'http://127.0.0.1:8765/copilot/index.html'};const contents={mainFrame:frame};
 assert.equal(trustedSender({sender:contents,senderFrame:frame},contents,frame.url),true);
 assert.equal(trustedSender({sender:contents,senderFrame:{url:frame.url}},contents,frame.url),false);
 assert.equal(trustedSender({sender:contents,senderFrame:frame},contents,'http://evil/'),false);
});
test('all commands have exact primitive shape',{skip:!windowCommand},()=>{
 assert.deepEqual(windowCommand('size','compact'),{command:'size',value:'compact'});
 assert.deepEqual(windowCommand('pause',true),{command:'pause',value:true});
 for(const [op,v] of [['url','https://evil'],['size','tiny'],['pause',1],['edit',{}],['close','extra']])assert.throws(()=>windowCommand(op,v));
});
test('network policy restricts top-level navigation and rejects foreign frames',()=>{
 const {allowedResource}=require(source);const url=launchOptions([]).url;
 assert.equal(allowedResource({url,resourceType:'mainFrame'},url),true);
 assert.equal(allowedResource({url:url.replace('index.html','other.html'),resourceType:'mainFrame'},url),false);
 assert.equal(allowedResource({url,resourceType:'subFrame'},url),false);
 assert.equal(allowedResource({url:'http://127.0.0.1:8765/api/copilot/status',resourceType:'xhr'},url),true);
 assert.equal(allowedResource({url:'https://example.com',resourceType:'xhr'},url),false);
});
test('native clipboard command is text-only and bounded',()=>{
 assert.deepEqual(windowCommand('copy','synthetic suggestion'),{command:'copy',value:'synthetic suggestion'});
 for(const value of ['', 'a'.repeat(801), {text:'x'}, '\u0000bad'])assert.throws(()=>windowCommand('copy',value));
});
test('copy limit counts Unicode code points like the backend schema',()=>{
 assert.equal(windowCommand('copy','文'.repeat(799)+'😀').command,'copy');
 assert.throws(()=>windowCommand('copy','文'.repeat(800)+'😀'));
});
test('recognition IPC cannot specify a window, path or arbitrary crop',()=>{
 assert.equal(windowCommand('recognition',true).command,'recognition');
 for(const command of ['conversationState','refreshConversation','calibrateTitle','autoCalibrateTitle']){
   assert.equal(windowCommand(command,undefined).command,command);
   assert.throws(()=>windowCommand(command,{window:123}));
 }
 assert.throws(()=>windowCommand('recognition','true'));
});
