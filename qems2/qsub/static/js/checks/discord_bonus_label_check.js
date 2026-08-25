const fs=require('fs'); const src=fs.readFileSync(process.argv[2],'utf8');
global.window={}; global.document={addEventListener(){}}; global.Node={TEXT_NODE:3,ELEMENT_NODE:1};
const chain=new Proxy(function(){},{get:(t,k)=>(k==='length'?0:k==='then'?undefined:chain),apply:()=>chain});
global.$=function(a){if(typeof a==='function'){a();return;}return chain;}; global.$.fn=chain;
eval(src);
const D=global.window.QemsDiscord;
const parts=[{text:'P1',answer:'_A_',diff:'m'},{text:'P2',answer:'_B_',diff:'e'},{text:'P3',answer:'_C_',diff:'h'}];
const sp=D.bonus('Lead.',parts,'Vikram Narasimhan','History - World',2047);
const pl=D.bonusPlain('Lead.',parts,'Vikram Narasimhan','History - World',2047);
console.log(JSON.stringify(sp));
const ok = !/\[10[emh]\]/.test(sp) && (sp.match(/\[10\]/g)||[]).length===3 && sp.includes('||m/e/h||')
        && pl.includes('[10m]') && pl.includes('[10e]') && pl.includes('[10h]');
console.log(ok?'PASS':'FAIL'); process.exit(ok?0:1);
