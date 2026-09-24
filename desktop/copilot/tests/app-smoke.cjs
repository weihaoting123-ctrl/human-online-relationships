'use strict';
// Run with Node on Windows, after Python Playwright and Electron are installed.
// The real main/preload run from an isolated synthetic root with NO watcher,
// archive, model key or WeChat access. Clipboard writes are intercepted in main.
const fs=require('node:fs');const path=require('node:path');const http=require('node:http');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'../../..');
const {_electron}=require(path.join(root,'.venv','Lib','site-packages','playwright','driver','package'));
const runtime=path.join(root,'desktop','copilot','node_modules','electron','dist','electron.exe');
const fixture=fs.mkdtempSync(path.join(root,'scripts','tmp','copilot-app-'));
const fixtureApp=path.join(fixture,'desktop','copilot');fs.mkdirSync(fixtureApp,{recursive:true});
for(const name of ['package.json','main.cjs','preload.cjs','security.cjs','icon.cjs','placement.cjs','window-controller.cjs','title-observer.cjs','title-reader.cjs','title-calibration.cjs','auto-calibration.cjs'])fs.copyFileSync(path.join(root,'desktop','copilot',name),path.join(fixtureApp,name));
fs.copyFileSync(path.join(__dirname,'fixture-bootstrap.cjs'),path.join(fixtureApp,'fixture-bootstrap.cjs'));
let calls=0,enabled=false,preview,bindingTitle='',bindingGeneration=0,bindingToken='',bindingSeq=0,pageRequests=0;
const people=[{bundle_id:'synthetic-person',contact_display:'合成青禾',date_from:'2026-09-01',date_to:'2026-09-24',message_count:20},
 {bundle_id:'synthetic-other',contact_display:'合成白鹭',date_from:'2026-09-01',date_to:'2026-09-24',message_count:20}];
const server=http.createServer(async(req,res)=>{
 const pathname=new URL(req.url,'http://localhost').pathname;
 const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value))};
 let body='';for await(const chunk of req)body+=chunk;
 const request=body?JSON.parse(body):{};
 if(pathname==='/api/modules'){
  if(req.method==='POST')enabled=request.enabled===true;
  return json({status:'ok',modules:[{id:'analysis',enabled:true,version:0},{id:'copilot',enabled,version:enabled?1:0}]});
 }
 if(pathname==='/api/copilot/status')return json({status:'ok',enabled,configured:true,provider:'synthetic',model:'synthetic',capabilities:{live_capture:false,auto_send:false},conversations:people});
 if(pathname==='/api/copilot/binding/start'){bindingTitle='';return json({status:'ok',session_id:'b'.repeat(48)})}
 if(pathname==='/api/copilot/binding'){
  bindingSeq=request.seq;
  const title=request.state==='observed'?request.title:'';
  if(title!==bindingTitle){bindingTitle=title;bindingToken=(++bindingGeneration).toString(16).padStart(48,'0')}
  const person=people.find(item=>item.contact_display===title);
  return json({state:person?'suggested':'unavailable',account_verified:false,observation_seq:bindingSeq,
   ...(person?{bundle_id:person.bundle_id,binding_token:bindingToken}:{})});
 }
 if(pathname==='/api/copilot/preview'){
  preview={status:'ok',preview_id:'a'.repeat(48),binding_revision:request.binding_revision,scope:request,max_calls:1,
   recipient:{configured:true,provider:'synthetic',model:'synthetic',endpoint:'https://example.invalid'},
   counts:{sample_messages:20,sample_chars:200,draft_chars:0,sample_date_from:'2026-09-01',sample_date_to:'2026-09-24',omitted_messages:0,truncated_messages:0},privacy_notices:[],
   ...(request.binding_token?{binding_required:true,account_verified:false,observation_seq:bindingSeq}:{})};
  return json(preview);
 }
 if(pathname==='/api/copilot/run'){
  if(preview.binding_required)assert.equal(request.binding_confirmed,true);
  calls++;return json({status:'ok',binding_revision:request.binding_revision,replies:['natural','warm','invite'].map(style=>({style,text:'合成建议，仅用于测试。',reason:'合成理由'})),topics:[],caveats:[]});
 }
 const allowed={'/copilot/index.html':'text/html','/copilot/copilot.js':'text/javascript','/copilot/live.js':'text/javascript','/copilot/copilot.css':'text/css'};
 if(!allowed[pathname]){res.writeHead(404);return res.end()}
 if(pathname==='/copilot/index.html')pageRequests++;
 res.setHeader('Content-Type',allowed[pathname]);
 res.setHeader('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");
 res.end(fs.readFileSync(path.join(root,'dashboard','static',pathname)));
});
(async()=>{
 let app;const checked=[];
 try{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
  const relaunch=args=>new Promise((resolve,reject)=>{
   const child=spawn(runtime,[path.join(fixtureApp,'fixture-bootstrap.cjs'),...args],{env,windowsHide:true,stdio:'ignore'});
   const timer=setTimeout(()=>{child.kill();reject(new Error('Synthetic second instance did not exit'))},10000);
   child.once('error',error=>{clearTimeout(timer);reject(error)});
   child.once('exit',code=>{clearTimeout(timer);code===0?resolve():reject(new Error('Synthetic second instance failed'))});
  });
  app=await _electron.launch({executablePath:runtime,args:[path.join(fixtureApp,'fixture-bootstrap.cjs'),`--port=${server.address().port}`,'--show'],env,timeout:20000});
  const page=await app.firstWindow();page.setDefaultTimeout(6000);
  await page.waitForFunction(()=>document.querySelector('#enable-copilot')&&!document.querySelector('#enable-copilot').disabled);
  await page.evaluate(async()=>{window.__fixtureNativeState=await window.copilotDesktop.state();window.copilotDesktop.onState(value=>{window.__fixtureNativeState=value})});
  assert.equal(await page.evaluate(()=>typeof require),'undefined');
  assert.equal(await page.evaluate(()=>typeof process),'undefined');checked.push('sandboxed-real-preload');
  let native=await page.evaluate(()=>window.copilotDesktop.state());
  assert.equal(native.presentation,true);assert.equal(native.paused,true);assert.equal(native.target,null);
  assert.equal(native.effectiveSize,'expanded');
  assert.deepEqual(await app.evaluate(({BrowserWindow})=>({visible:BrowserWindow.getAllWindows()[0].isVisible(),windows:BrowserWindow.getAllWindows().length,focus:globalThis.__fixtureFocusCalls,watchers:globalThis.__fixtureWatchStarts,captures:globalThis.__fixtureCaptures})),{visible:true,windows:1,focus:0,watchers:0,captures:0});
  await page.evaluate(()=>window.copilotDesktop.refreshConversation());
  assert.equal(await app.evaluate(()=>globalThis.__fixtureCaptures),0);assert.equal(calls,0);
  checked.push('cold-show-is-paused-expanded-without-capture-or-focus');
  await page.evaluate(()=>window.copilotDesktop.pause(false));
  await page.waitForFunction(()=>!window.__fixtureNativeState.paused&&window.__fixtureNativeState.target==='synthetic-window');
  native=await page.evaluate(()=>window.copilotDesktop.state());assert.equal(native.presentation,false);
  assert.equal(await app.evaluate(()=>globalThis.__fixtureWatchStarts),1);assert.equal(calls,0);checked.push('explicit-resume-starts-guarded-follow-only');
  await relaunch([`--port=${server.address().port}`]);
  assert.equal((await page.evaluate(()=>window.copilotDesktop.state())).paused,false);checked.push('unflagged-second-instance-does-not-present');
  await relaunch([`--port=${server.address().port===65535?65534:server.address().port+1}`,'--show']);
  assert.equal((await page.evaluate(()=>window.copilotDesktop.state())).paused,false);checked.push('different-port-second-instance-is-ignored');
  await relaunch([`--port=${server.address().port}`,'--show']);
  await page.waitForFunction(()=>window.__fixtureNativeState.presentation===true);
  // Graceful helper shutdown has a bounded one-second kill fallback.
  await app.evaluate(async()=>{const deadline=Date.now()+1500;while(globalThis.__fixtureWatchActive&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,25))});
  assert.deepEqual(await app.evaluate(({BrowserWindow})=>({visible:BrowserWindow.getAllWindows()[0].isVisible(),windows:BrowserWindow.getAllWindows().length,focus:globalThis.__fixtureFocusCalls,watchers:globalThis.__fixtureWatchActive,captures:globalThis.__fixtureCaptures})),{visible:true,windows:1,focus:0,watchers:0,captures:0});
  assert.equal(calls,0);checked.push('same-port-second-instance-reuses-paused-window');
  await page.evaluate(()=>window.copilotDesktop.pause(false));
  await page.waitForFunction(()=>window.__fixtureNativeState.target==='synthetic-window');
  await app.evaluate(({BrowserWindow,clipboard})=>{
   // Focus only this fixture, to exercise deliberate input; never touch WeChat.
   const win=BrowserWindow.getAllWindows()[0];win.setFocusable(true);win.show();
   clipboard.writeText=text=>{globalThis.__syntheticCopy=text};
  });
  await page.evaluate(()=>window.copilotDesktop.setSize('expanded'));
  await page.locator('#enable-copilot').click();
  assert.equal(await page.locator('#context-mode').inputValue(),'auto');
  assert.equal(await app.evaluate(()=>globalThis.__fixtureCaptures),0);checked.push('no-title-capture-before-calibration');
  await page.locator('#context-mode').selectOption('manual');
  await page.locator('#context-settings').evaluate(node=>{node.open=true});
  await page.locator('#contact-select').selectOption('synthetic-person');
  await page.locator('#prepare-preview').click();
  await page.locator('#confirm-run').waitFor({state:'visible'});assert.equal(calls,0);checked.push('no-call-before-confirmation');
  await page.locator('#confirm-run').click();
  await page.locator('.reply-card').first().waitFor();assert.equal(calls,1);checked.push('one-explicit-synthetic-call');
  await page.locator('.reply-card .copy-button').first().click();
  assert.equal(await app.evaluate(()=>globalThis.__syntheticCopy),'合成建议，仅用于测试。');checked.push('gesture-only-copy-bridge');
  await page.locator('#context-mode').selectOption('auto');
  await page.locator('#auto-calibrate-title').click();
  await page.waitForFunction(()=>document.querySelector('#auto-calibration-result').textContent.includes('已保存'));
  const autoNumeric=JSON.parse(fs.readFileSync(path.join(fixture,'data','private','copilot','header-region.json'),'utf8'));
  assert.deepEqual(autoNumeric.region,{x:340,y:16,width:100,height:48});assert.equal(calls,1);checked.push('one-click-numeric-calibration-no-cloud');
  await page.locator('#calibrate-title').click();
  await app.evaluate(()=>globalThis.__fixtureMark(100,40));
  await app.evaluate(()=>globalThis.__fixtureMark(340,72));
  await page.waitForFunction(()=>document.querySelector('#contact-select').value==='synthetic-person');
  assert.equal(calls,1);checked.push('calibrated-local-auto-candidate-no-cloud');
  const numeric=JSON.parse(fs.readFileSync(path.join(fixture,'data','private','copilot','header-region.json'),'utf8'));
  assert.deepEqual(numeric,{profile:'wechat-4.1.13',region:{x:100,y:40,width:240,height:32}});checked.push('numeric-only-calibration-preferences');
  await page.locator('#prepare-preview').click();
  await page.locator('#binding-confirmation').waitFor({state:'visible'});
  assert.equal(await page.locator('#binding-confirmed').isChecked(),false);
  assert.equal(await page.locator('#confirm-run').isDisabled(),true);
  await page.locator('#binding-confirmed').check();
  await page.locator('#confirm-run').click();
  await page.locator('.reply-card').first().waitFor();assert.equal(calls,2);checked.push('fresh-title-and-explicit-identity-confirmation');
  await page.locator('#latest-draft').fill('合成补充');
  await app.evaluate(()=>{globalThis.__fixtureTitle='合成白鹭'});
  await page.waitForFunction(()=>document.querySelector('#contact-select').value==='synthetic-other');
  assert.equal(await page.locator('#latest-draft').inputValue(),'');
  assert.equal(await page.locator('.reply-card').count(),0);assert.equal(calls,2);checked.push('same-window-contact-change-clears-context');
  await page.screenshot({path:path.join(root,'scripts','tmp','copilot-desktop-synthetic.png')});
  assert.equal(await page.evaluate(async()=>{try{await fetch('https://example.invalid');return false}catch{return true}}),true);checked.push('external-network-blocked');
  await page.locator('#prepare-preview').click();await page.locator('#binding-confirmation').waitFor({state:'visible'});
  await relaunch([`--port=${server.address().port}`,'--show']);
  await page.waitForFunction(()=>window.__fixtureNativeState.presentation===true);
  assert.equal((await page.evaluate(()=>window.copilotDesktop.conversationState())).title,'');
  await page.waitForFunction(()=>document.querySelector('#contact-select').value==='');
  assert.equal(await page.locator('#consent-panel').isVisible(),false);
  assert.equal(await page.locator('#binding-confirmed').isChecked(),false);
  assert.equal(await page.locator('#confirm-run').isDisabled(),true);
  const captures=await app.evaluate(()=>globalThis.__fixtureCaptures);
  await page.evaluate(()=>window.copilotDesktop.refreshConversation());
  assert.equal(await app.evaluate(()=>globalThis.__fixtureCaptures),captures);assert.equal(calls,2);
  checked.push('presentation-clears-calibrated-candidate-and-confirmation');
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.emit('render-process-gone',{},{}));
  const previousPageRequests=pageRequests;
  await relaunch([`--port=${server.address().port}`,'--show']);
  assert.ok(pageRequests>previousPageRequests,'An explicit show after renderer failure must reload its fixed page');
  await page.waitForFunction(()=>document.querySelector('#native-status').textContent.includes('设置窗口'));
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].isVisible()),true);
  assert.equal(await app.evaluate(()=>globalThis.__fixtureCaptures),captures);checked.push('show-recovers-failed-page-without-resuming-capture');
  await app.close();app=null;
  app=await _electron.launch({executablePath:runtime,args:[path.join(fixtureApp,'fixture-bootstrap.cjs'),`--port=${server.address().port}`],env,timeout:20000});
  let nextPage=await app.firstWindow();nextPage.setDefaultTimeout(6000);
  await nextPage.waitForFunction(()=>document.querySelector('#native-status')?.textContent.includes('已绑定窗口'));
  native=await nextPage.evaluate(()=>window.copilotDesktop.state());
  assert.equal(native.presentation,false);assert.equal(native.paused,false);assert.equal(await app.evaluate(()=>globalThis.__fixtureWatchStarts),1);
  checked.push('ordinary-cold-launch-retains-follow-behavior');
  await app.close();app=null;
  app=await _electron.launch({executablePath:runtime,args:[path.join(fixtureApp,'fixture-bootstrap.cjs'),`--port=${server.address().port}`],env:{...env,COPILOT_FIXTURE_QUEUED_SHOW:'1'},timeout:20000});
  nextPage=await app.firstWindow();nextPage.setDefaultTimeout(6000);
  await nextPage.waitForFunction(()=>document.querySelector('#native-status')?.textContent.includes('设置窗口'));
  native=await nextPage.evaluate(()=>window.copilotDesktop.state());
  assert.equal(native.presentation,true);assert.equal(native.paused,true);assert.equal(native.target,null);
  assert.deepEqual(await app.evaluate(({BrowserWindow})=>({visible:BrowserWindow.getAllWindows()[0].isVisible(),windows:BrowserWindow.getAllWindows().length,watchers:globalThis.__fixtureWatchStarts,captures:globalThis.__fixtureCaptures,focus:globalThis.__fixtureFocusCalls})),{visible:true,windows:1,watchers:0,captures:0,focus:0});
  assert.equal(calls,2);checked.push('queued-show-before-ready-suppresses-calibrated-capture');
  console.log(JSON.stringify({status:'passed',count:checked.length,checks:checked,real_wechat_accessed:false,real_clipboard_modified:false,real_cloud_calls:0}));
 }catch(error){console.error(JSON.stringify({status:'failed',completed:checked,message:String(error.message).slice(0,1200),second:app?await app.evaluate(()=>globalThis.__fixtureSecondArgs).catch(()=>[]):[]}));process.exitCode=1}
 finally{if(app)await app.close();await new Promise(resolve=>server.close(resolve));}
})();
