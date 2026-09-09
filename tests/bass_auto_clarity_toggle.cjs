// 低频自动清晰 opt-in 开关：行为静态测试（vm 执行绑定段，不启动界面）
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/app.js', 'utf8');
const start = source.indexOf('  /* ─── 低频自动清晰 opt-in 开关');
const end = source.indexOf('  /* ─── 自定义下拉组件', start);
assert.ok(start > 0 && end > start, 'binding section markers found');

const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: '',
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(event, handler) { this[event] = handler; },
  });
  return elements.get(id);
}
function makeContext(initial) {
  const saved = [];
  const ctx = vm.createContext({
    $,
    loadValue: () => initial,
    saveValue: (k, v) => saved.push([k, v]),
  });
  ctx.__saved = saved;
  return ctx;
}
function checked() { return $('opt-bass-clarity').attrs['aria-checked']; }

// 默认关闭 → 点击开启（持久化 "1"）→ 再点关闭（持久化 "0"）
let ctx = makeContext('0');
vm.runInContext(source.slice(start, end), ctx);
assert.equal(checked(), 'false');
assert.equal($('val-bass-clarity').textContent, '关');
const getter = vm.runInContext('getBassClarity', ctx);
assert.equal(getter(), false);
$('opt-bass-clarity').click();
assert.equal(checked(), 'true');
assert.equal($('val-bass-clarity').textContent, '开');
assert.equal(getter(), true);
assert.deepEqual(ctx.__saved, [['bass_auto_clarity', '1']]);
$('opt-bass-clarity').click();
assert.equal(checked(), 'false');
assert.equal(getter(), false);
assert.deepEqual(ctx.__saved, [['bass_auto_clarity', '1'], ['bass_auto_clarity', '0']]);

// 已持久化的开启状态：启动即 aria-checked=true，getter 返回 true
ctx = makeContext('1');
vm.runInContext(source.slice(start, end), ctx);
assert.equal(checked(), 'true');
assert.equal(vm.runInContext('getBassClarity()', ctx), true);

// 存储脏值（非 "1"）：安全回落到默认关闭
ctx = makeContext('garbage');
vm.runInContext(source.slice(start, end), ctx);
assert.equal(checked(), 'false');
assert.equal(vm.runInContext('getBassClarity()', ctx), false);

console.log('Bass auto clarity toggle: default off, click/persist, restore-on, dirty-storage fallback passed');
