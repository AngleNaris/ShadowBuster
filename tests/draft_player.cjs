const assert=require('node:assert/strict');
require('../ui/draft_audio.js');
const messages=[];let resumeCount=0,completeResume;
global.AudioContext=class {
 constructor(){this.state='suspended';this.currentTime=0;this.audioWorklet={addModule:async()=>{}};}
 createGain(){return {connect(){},gain:{setTargetAtTime(){}}};}
 resume(){resumeCount++;return new Promise(resolve=>{completeResume=()=>{this.state='running';resolve();};});}
};
global.AudioWorkletNode=class {
 constructor(){this.port={postMessage:p=>messages.push(p)};}
 connect(){}
};
(async()=>{
 const p=new global.DraftPlayer();await p.load({id:1,frames:441000,stream:true});
 const starting=p.play(true);assert.equal(p.playing,true);
 await p.play(false);assert.equal(p.playing,false);assert.equal(resumeCount,1);
 assert.equal(messages.at(-1).value,false,'pause is sent before resume resolves');
 completeResume();await starting;assert.equal(messages.at(-1).value,false,'late resume cannot restart playback');
 await p.play(true);const oldRevision=p.playRevision;
 await p.play(false);
 p.node.port.onmessage({data:{type:'position',session:1,generation:0,playRevision:oldRevision,frame:128,playing:true,waiting:false,audioTime:0}});
 assert.equal(p.playing,false,'old running report must not override pause');
 await p.play(true);await p.play(false);await p.play(true);
 assert.equal(p.playing,true);assert.equal(messages.at(-1).value,true);assert.equal(resumeCount,1);
 console.log('Player commands: immediate feedback, pause without resume, stale reports and rapid toggles passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
