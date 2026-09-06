const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/app.js', 'utf8');
const start = source.indexOf('  function onGpuStatus(raw)');
const end = source.indexOf('  gpuDlBtn.addEventListener', start);
function setup() {
  const context = { GpuState: { installed: null, dev: null, downloading: false, checking: true },
    gpuDlBtn: {}, gpuCancelBtn: {}, gpuRestartBtn: {}, gpuProg: {}, gpuStatusEl: {},
    fmtSize: () => '3 GB', gpuRow: () => {}, setGpuBar: () => {} };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  return context;
}
let c = setup();
c.onGpuStatus(JSON.stringify({type:'busy', op:'install'}));
assert.equal(c.gpuDlBtn.hidden, true);
assert.equal(c.GpuState.checking, true);
c.onGpuStatus(JSON.stringify({type:'state', manifest:{totalSize:3}}));
assert.equal(c.GpuState.checking, false);
assert.equal(c.gpuDlBtn.disabled, false);
assert.equal(c.gpuDlBtn.hidden, false);
c = setup(); c.GpuState.downloading = true; c.gpuCancelBtn.hidden = false;
c.onGpuStatus(JSON.stringify({type:'busy',op:'check'}));
assert.equal(c.GpuState.downloading,true);
assert.equal(c.gpuCancelBtn.hidden,false);
c = setup();c.onGpuStatus(JSON.stringify({type:'error',msg:'timeout'}));
assert.equal(c.GpuState.checking,false);
assert.equal(c.gpuDlBtn.disabled,false);
assert.equal(c.gpuDlBtn.hidden,false);
console.log('GPU UI busy, completion, download preservation and error recovery passed');
