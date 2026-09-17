const {remote}=require('webdriverio'); const {spawn,execFileSync}=require('child_process'); const path=require('path');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{try{execFileSync('powershell.exe',['-NoProfile','-Command','Get-Process ledgerlens,ledgerlens-backend,tauri-driver,msedgedriver -ErrorAction SilentlyContinue|Stop-Process -Force']);}catch{}
const d=spawn(path.join(process.env.USERPROFILE,'.cargo','bin','tauri-driver.exe'),['--port','4444']); await sleep(1500);
const b=await remote({hostname:'127.0.0.1',port:4444,path:'/',capabilities:{'tauri:options':{application:path.join(process.env.LOCALAPPDATA,'LedgerLens','ledgerlens.exe')}}});
await sleep(25000); let c=await b.$('button*=Continue to App'); if(await c.isExisting()) await c.click(); let n=await b.$('[data-testid="nav-clients"]'); await n.click();
let cards=await b.$$('[data-testid^="client-card-"]'); if(cards.length){await cards[0].click();} let p=await b.$('[data-testid="pick-folder-button"]'); await p.waitForDisplayed({timeout:10000}); await p.click(); await sleep(500);
let ts=await b.$$('[data-sonner-toast]'); let tt=[]; for(const x of ts) tt.push(await x.getText()); console.log('TOASTS=',tt); console.log('PAGE_TEXT_IMMEDIATE=',await b.$('body').getText()); await sleep(2000);
try{execFileSync('python',[path.resolve(__dirname,'select_folder.py'),path.resolve(__dirname,'final_ui_synthetic')],{stdio:'inherit'});}catch(e){console.log('DIALOG_PROBE_FAILED');}
try{await b.deleteSession()}catch{}; try{d.kill()}catch{};})();