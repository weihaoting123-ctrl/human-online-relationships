'use strict';
// Test-only entrypoint copied into a synthetic root; never used by the launcher.
// Playwright's debugger switches are not accepted by the production CLI.
process.argv=[process.argv[0],__filename,...process.argv.filter(value=>/^--port=\d+$/.test(value))];
const fs=require('node:fs');const {PassThrough}=require('node:stream');const {EventEmitter}=require('node:events');
const childProcess=require('node:child_process');const exists=fs.existsSync;
fs.existsSync=file=>String(file).endsWith('Scripts\\python.exe')||String(file).endsWith('copilot_window_watch.py')||exists(file);
childProcess.spawn=()=>{
 const child=new EventEmitter();child.stdin=new PassThrough();child.stdout=new PassThrough();child.exitCode=null;
 const frame={version:1,state:'bound',target:'synthetic-window',rect:{x:80,y:80,width:500,height:500},visible:true,minimized:false,foreground:true,foreground_pid:process.pid,candidate_count:1};
 const timer=setInterval(()=>child.stdout.write(JSON.stringify(frame)+'\n'),100);
 child.kill=()=>{clearInterval(timer);child.exitCode=0;child.emit('exit',0)};
 child.stdin.on('finish',()=>child.kill());return child;
};
require('./main.cjs');
