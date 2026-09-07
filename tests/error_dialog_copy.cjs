const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/app.js', 'utf8');
const start = source.indexOf('  let errorCopyText =');
const end = source.indexOf('  function finishProcessing()', start);
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: '', disabled: false, hidden: true,
    classList: { add() {}, remove() {} },
    addEventListener(event, handler) { this[event] = handler; },
  });
  return elements.get(id);
}
let copied;
const context = vm.createContext({ $, api: { copyText(text, done) { copied = text; done(true); } },
  navigator: {}, requestAnimationFrame(fn) { fn(); }, setTimeout });
vm.runInContext(source.slice(start, end), context);
(async () => {
  const detail = 'Traceback\n中文 <error> & file.wav\n' + 'long error\n'.repeat(1000);
  context.detail = detail;
  vm.runInContext('showErrorModal("处理失败", detail, "D:/输出")', context);
  await $('err-copy').click();
  assert.equal(copied, '处理失败\n\n产物目录：D:/输出\n\n' + detail);
  assert.equal($('err-copy').textContent, '已复制');
  assert.equal($('err-modal').hidden, false);
  vm.runInContext('showErrorModal("另一个错误", "详情", "")', context);
  assert.equal($('err-copy').textContent, '复制错误信息');
  context.api.copyText = (_, done) => done(false);
  await $('err-copy').click();
  assert.equal($('err-copy').textContent, '复制失败，重试');
  assert.equal($('err-copy').disabled, false);
  context.api = null;
  context.navigator.clipboard = { async writeText(text) { copied = text; } };
  await $('err-copy').click();
  assert.equal(copied, '另一个错误\n\n详情');
  console.log('Error dialog copy: full text, feedback, retry, browser fallback passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
