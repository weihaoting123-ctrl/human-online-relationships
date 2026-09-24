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
 for(const command of ['conversationState','refreshConversation','calibrateTitle']){
   assert.equal(windowCommand(command,undefined).command,command);
   assert.throws(()=>windowCommand(command,{window:123}));
 }
 assert.throws(()=>windowCommand('recognition','true'));
});
