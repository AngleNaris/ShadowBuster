// 处理缓存设置行：行为测试（vm 执行缓存模块段，桥接用 mock，不启动界面）
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/app.js', 'utf8');
const start = source.indexOf('  /* ─── 处理缓存：容量选择');
const end = source.indexOf('  // 主题色 swatches', start);
assert.ok(start > 0 && end > start, 'cache module markers found');
const section = source.slice(start, end);

const elements = new Map();
function makeEl(id) {
  const el = {
    id, textContent: '', disabled: false, classes: new Set(), handlers: {},
    addEventListener(ev, fn) { el.handlers[ev] = fn; },
    click(...args) { if (el.handlers.click) el.handlers.click(...args); },
  };
  el.classList = {
    add: (c) => el.classes.add(c),
    remove: (c) => el.classes.delete(c),
    contains: (c) => el.classes.has(c),
  };
  return el;
}
function $(id) { if (!elements.has(id)) elements.set(id, makeEl(id)); return elements.get(id); }

function makeApi(overrides = {}) {
  return { refreshCacheInfo() {}, setCacheCapacity(gb, cb) { cb(true); },
    clearCache(cb) { cb(true); }, ...overrides };
}
function makeContext({ api = makeApi(), processing = false } = {}) {
  const timers = [];
  const ctx = vm.createContext({
    $, state: { processing }, api,
    buildDropdown: (id, options, initial) => {
      let value = initial;
      const get = () => value;
      get.set = (v) => { if (options.some((o) => o.v === String(v))) value = String(v); };
      get._options = options;
      return get;
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
  });
  ctx.__timers = timers;
  return ctx;
}
function boot(opts) {
  elements.clear();                 // 每次启动重置 DOM mock，避免跨用例串状态
  const ctx = makeContext(opts);
  vm.runInContext(section, ctx);
  return ctx;
}
function internals(ctx) {
  return vm.runInContext('({ cacheCap, onCacheStatus, renderCache, CACHE_CAPS, cacheClearBtn, cacheTrigger })', ctx);
}
function cacheStatus(ctx) { return vm.runInContext('cacheStatusEl.textContent', ctx); }

// 1) info 回传：已用显示 + 容量同步 + 清空按钮可用
let ctx = boot();
let it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 5, used_bytes: 1073741824, entries: 3 }));
assert.equal(cacheStatus(ctx), '已用 1.00 GiB · 3 条');
assert.equal(it.cacheCap(), '5');
assert.equal(it.cacheClearBtn.disabled, false);

// 2) 容量 0（关闭）：状态提示，清空禁用
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 0, used_bytes: 0, entries: 0 }));
assert.equal(cacheStatus(ctx), '已关闭，处理不写入缓存');
assert.equal(it.cacheCap(), '0');
assert.equal(it.cacheClearBtn.disabled, true);

// 3) 清空成功：cleared → 已清空 + 已用归零（按钮回到禁用）
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 5, used_bytes: 2048, entries: 1 }));
assert.equal(it.cacheClearBtn.disabled, false);
it.onCacheStatus(JSON.stringify({ type: 'cleared', capacity_gb: 5, used_bytes: 0, entries: 0 }));
assert.equal(cacheStatus(ctx), '已清空');
assert.equal(it.cacheClearBtn.disabled, true);

// 4) busy / error 文案
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'busy', op: 'clear' }));
assert.equal(cacheStatus(ctx), '处理进行中，请稍后再清空');
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'busy', op: 'capacity' }));
assert.equal(cacheStatus(ctx), '处理进行中，请稍后再改容量');
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'error', op: 'clear', msg: 'disk io' }));
assert.equal(cacheStatus(ctx), '失败：disk io');

// 5) 两步确认：先待确认（红字），再点才执行清空
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 5, used_bytes: 4096, entries: 1 }));
let calls = [];
ctx.api.clearCache = (cb) => { calls.push('clear'); cb(true); };
it.cacheClearBtn.click();
assert.equal(it.cacheClearBtn.textContent, '确认清空？');
assert.ok(it.cacheClearBtn.classes.has('set-btn--danger'));
assert.deepEqual(calls, []);          // 第一次点击不执行
it.cacheClearBtn.click();
assert.deepEqual(calls, ['clear']);   // 第二次执行
assert.equal(it.cacheClearBtn.textContent, '清空缓存');   // 复位

// 6) 待确认 4 秒后自动复位（超时回调）
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 5, used_bytes: 4096, entries: 1 }));
it.cacheClearBtn.click();
const timer = ctx.__timers[ctx.__timers.length - 1];
assert.equal(timer.ms, 4000);
timer.fn();
assert.equal(it.cacheClearBtn.textContent, '清空缓存');

// 7) 后端拒绝（返回 false）：busy 解除，等待 busy 信号给出文案
ctx = boot({ api: makeApi({ clearCache: (cb) => { cb(false); } }) }); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 5, used_bytes: 4096, entries: 1 }));
it.cacheClearBtn.click(); it.cacheClearBtn.click();
assert.equal(it.cacheClearBtn.disabled, false);

// 8) 处理进行中：容量下拉与清空同时禁用，点击不进入待确认
ctx = boot({ processing: true }); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 5, used_bytes: 4096, entries: 1 }));
assert.equal(it.cacheTrigger.disabled, true);
assert.equal(it.cacheClearBtn.disabled, true);
it.cacheClearBtn.click();
it.cacheClearBtn.click();   // disabled 时点击无效（handler 首行守卫）
assert.equal(vm.runInContext('cacheClearArmed', ctx), false);
assert.ok(!it.cacheClearBtn.classes.has('set-btn--danger'));

// 9) 静态预览（无后端）：打开设置（renderCache）后状态占位、清空禁用，不抛错
ctx = boot({ api: null }); it = internals(ctx);
vm.runInContext('renderCache()', ctx);   // openSettings 打开时的刷新调用
assert.equal(cacheStatus(ctx), '—');
assert.equal(it.cacheClearBtn.disabled, true);

// 10) 非档位容量（如 7 GiB）：补一个选项展示，不静默改值
ctx = boot(); it = internals(ctx);
it.onCacheStatus(JSON.stringify({ type: 'info', capacity_gb: 7, used_bytes: 0, entries: 0 }));
assert.equal(it.cacheCap(), '7');
assert.ok(it.CACHE_CAPS.some((o) => o.v === '7' && o.label === '7 GiB'));

// 11) 容量持久化不在前端：模块段不得调用 localStorage API / saveValue
assert.ok(!/localStorage\.(getItem|setItem|removeItem)/.test(section),
  'cache module must not use localStorage API');
assert.ok(!section.includes('saveValue('), 'cache module must not persist via saveValue');

console.log('cache settings row behavior (info/used/clear/busy/error/confirm/processing/static preview) passed');
