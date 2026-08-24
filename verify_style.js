const http = require("http");

function getTargets() {
  return new Promise((resolve, reject) => {
    http.get("http://localhost:9223/json", (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve(JSON.parse(data)));
    }).on("error", reject);
  });
}

async function main() {
  const targets = await getTargets();
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = {};
  function send(method, params) {
    return new Promise((resolve) => {
      const msgId = ++id;
      pending[msgId] = resolve;
      ws.send(JSON.stringify({ id: msgId, method, params: params || {} }));
    });
  }
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending[msg.id]) pending[msg.id](msg.result);
  });
  await send("Runtime.enable");
  await new Promise((r) => setTimeout(r, 600));
  const js = `
  (function(){
    const cs = (sel, prop) => { const el = document.querySelector(sel); return el ? getComputedStyle(el)[prop] : 'MISSING'; };
    const out = {};
    out.pageUrl = location.href;
    out.rackBodyPadding = cs('.rack-body', 'padding');
    out.topbarBorderBottom = cs('.topbar', 'borderBottomWidth');
    out.bodyFont = cs('body', 'fontFamily');
    out.brandFont = cs('.brand-name', 'fontFamily');
    out.knobValueFont = cs('.knob-value', 'fontFamily');
    out.rackIndexFont = cs('.rack-index', 'fontFamily');
    out.queueCountFont = cs('.queue-count', 'fontFamily');
    out.statusTimeFont = cs('.status-time', 'fontFamily');
    out.rackBodyAlign = cs('.rack-body', 'alignItems');
    out.rackFieldsAlign = cs('.rack-fields', 'alignItems');
    // knob vertical centering: compare knob center vs rack-body content center
    const rb = document.querySelector('.rack[data-rack="lew"] .rack-body').getBoundingClientRect();
    const kw = document.querySelector('.rack[data-rack="lew"] .knob-wrap').getBoundingClientRect();
    out.rackBody = {top:rb.top, bottom:rb.bottom, h:rb.height};
    out.knobWrap = {top:kw.top, bottom:kw.bottom, h:kw.height};
    out.knobCenterOffset = (kw.top + kw.height/2) - (rb.top + rb.height/2);
    return JSON.stringify(out);
  })();
  `;
  const res = await send("Runtime.evaluate", { expression: js, returnByValue: true });
  console.log(JSON.stringify(JSON.parse(res.result.value), null, 2));
  ws.close();
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
