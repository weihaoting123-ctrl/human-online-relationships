'use strict';
// Run with Node on Windows, after Python Playwright and Electron are installed.
// The real main/preload run from an isolated synthetic root with NO watcher,
// archive, model key or WeChat access. Clipboard writes are intercepted in main.
const fs=require('node:fs');const path=require('node:path');const http=require('node:http');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../..');
const {_electron}=require(path.join(root,'.venv','Lib','site-packages','playwright','driver','package'));
const runtime=path.join(root,'desktop','copilot','node_modules','electron','dist','electron.exe');
const fixture=fs.mkdtempSync(path.join(root,'scripts','tmp','copilot-app-'));
const fixtureApp=path.join(fixture,'desktop','copilot');fs.mkdirSync(fixtureApp,{recursive:true});
for(const name of ['package.json','main.cjs','preload.cjs','security.cjs','icon.cjs','placement.cjs','window-controller.cjs'])fs.copyFileSync(path.join(root,'desktop','copilot',name),path.join(fixtureApp,name));
fs.copyFileSync(path.join(__dirname,'fixture-bootstrap.cjs'),path.join(fixtureApp,'fixture-bootstrap.cjs'));
let calls=0,enabled=false,preview;
const server=http.createServer(async(req,res)=>{
 const pathname=new URL(req.url,'http://localhost').pathname;
 const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value))};
 let body='';for await(const chunk of req)body+=chunk;
 const request=body?JSON.parse(body):{};
 if(pathname==='/api/modules'){
  if(req.method==='POST')enabled=request.enabled===true;
  return json({status:'ok',modules:[{id:'analysis',enabled:true,version:0},{id:'copilot',enabled,version:enabled?1:0}]});
 }
 if(pathname==='/api/copilot/status')return json({status:'ok',enabled,configured:true,provider:'synthetic',model:'synthetic',capabilities:{live_capture:false,auto_send:false},conversations:[{bundle_id:'synthetic-person',contact_display:'合成青禾',date_from:'2026-09-01',date_to:'2026-09-24',message_count:20}]});
 if(pathname==='/api/copilot/preview'){
  preview={status:'ok',preview_id:'a'.repeat(48),binding_revision:request.binding_revision,scope:request,max_calls:1,
   recipient:{configured:true,provider:'synthetic',model:'synthetic',endpoint:'https://example.invalid'},
   counts:{sample_messages:20,sample_chars:200,draft_chars:0,sample_date_from:'2026-09-01',sample_date_to:'2026-09-24',omitted_messages:0,truncated_messages:0},privacy_notices:[]};
  return json(preview);
 }
 if(pathname==='/api/copilot/run'){
  calls++;return json({status:'ok',binding_revision:request.binding_revision,replies:['natural','warm','invite'].map(style=>({style,text:'合成建议，仅用于测试。',reason:'合成理由'})),topics:[],caveats:[]});
 }
 const allowed={'/copilot/index.html':'text/html','/copilot/copilot.js':'text/javascript','/copilot/copilot.css':'text/css'};
 if(!allowed[pathname]){res.writeHead(404);return res.end()}
 res.setHeader('Content-Type',allowed[pathname]);
 res.setHeader('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");
 res.end(fs.readFileSync(path.join(root,'dashboard','static',pathname)));
});
(async()=>{
 let app;const checked=[];
 try{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
  app=await _electron.launch({executablePath:runtime,args:[path.join(fixtureApp,'fixture-bootstrap.cjs'),`--port=${server.address().port}`],env,timeout:20000});
  const page=await app.firstWindow();page.setDefaultTimeout(6000);
  await page.waitForFunction(()=>document.querySelector('#enable-copilot')&&!document.querySelector('#enable-copilot').disabled);
  assert.equal(await page.evaluate(()=>typeof require),'undefined');
  assert.equal(await page.evaluate(()=>typeof process),'undefined');checked.push('sandboxed-real-preload');
  await app.evaluate(({BrowserWindow,clipboard})=>{
   // Focus only this fixture, to exercise deliberate input; never touch WeChat.
   const win=BrowserWindow.getAllWindows()[0];win.setFocusable(true);win.show();
   clipboard.writeText=text=>{globalThis.__syntheticCopy=text};
  });
  await page.locator('#expand-window').click();
  await page.locator('#enable-copilot').click();
  await page.locator('#context-settings').evaluate(node=>{node.open=true});
  await page.locator('#contact-select').selectOption('synthetic-person');
  await page.locator('#prepare-preview').click();
  await page.locator('#confirm-run').waitFor({state:'visible'});assert.equal(calls,0);checked.push('no-call-before-confirmation');
  await page.locator('#confirm-run').click();
  await page.locator('.reply-card').first().waitFor();assert.equal(calls,1);checked.push('one-explicit-synthetic-call');
  await page.locator('.reply-card .copy-button').first().click();
  assert.equal(await app.evaluate(()=>globalThis.__syntheticCopy),'合成建议，仅用于测试。');checked.push('gesture-only-copy-bridge');
  await page.screenshot({path:path.join(root,'scripts','tmp','copilot-desktop-synthetic.png')});
  assert.equal(await page.evaluate(async()=>{try{await fetch('https://example.invalid');return false}catch{return true}}),true);checked.push('external-network-blocked');
  console.log(JSON.stringify({status:'passed',count:checked.length,checks:checked,real_wechat_accessed:false,real_clipboard_modified:false,real_cloud_calls:0}));
 }catch(error){console.error(JSON.stringify({status:'failed',completed:checked,message:String(error.message).slice(0,1200)}));process.exitCode=1}
 finally{if(app)await app.close();await new Promise(resolve=>server.close(resolve));}
})();
