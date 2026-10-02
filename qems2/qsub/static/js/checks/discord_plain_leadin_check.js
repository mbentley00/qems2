const fs=require('fs'); const src=fs.readFileSync(process.argv[2],'utf8');
global.window={}; global.document={addEventListener(){}}; global.Node={TEXT_NODE:3,ELEMENT_NODE:1};
const chain=new Proxy(function(){},{get:(t,k)=>(k==='length'?0:k==='then'?undefined:chain),apply:()=>chain});
global.$=function(a){if(typeof a==='function'){a();return;}return chain;}; global.$.fn=chain;
eval(src);
const D=global.window.QemsDiscord;
const parts=[{text:'P1',answer:'_A_',diff:'m'},{text:'P2',answer:'_B_',diff:'e'},{text:'P3',answer:'_C_',diff:'h'}];
const count=function(leadin){
    const first=D.bonusPlain(leadin,parts,'Vikram Narasimhan','History - World',2047).split('\n')[0];
    console.log(JSON.stringify(first));
    return (first.match(/points,? each/gi)||[]).length;
};
// A leadin that already carries the phrase must not get a second one; one
// without it still gets it supplied.
const ok = count('Lead. For 10 points each:')===1
        && count('For 10 points each, name these kings.')===1
        && count('Lead. For ten points each:')===1
        && count('Lead. for 15 points each -')===1
        && count('Lead.')===1
        && count('')===1
        && D.bonusPlain('',parts,'','',0).indexOf('For 10 points each:\n')===0;
console.log(ok?'PASS':'FAIL'); process.exit(ok?0:1);
