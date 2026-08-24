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
  await new Promise((r) => setTimeout(r, 800));

  const js = `
  (function(){
    const out = {};
    out.inner = { w: innerWidth, h: innerHeight };
    const R = (el) => { const r = el.getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom }; };
    out.studio = R(document.querySelector(".studio"));
    out.queue = R(document.querySelector(".queue"));
    out.racks = R(document.querySelector(".racks"));
    out.dock = R(document.querySelector(".dock"));
    out.btn = R(document.getElementById("btn-process"));
    // 打开流派下拉，测遮挡
    document.getElementById("dd-genre-trigger").click();
    const panel = document.getElementById("dd-genre-panel");
    const p = panel.getBoundingClientRect();
    out.panel = { t: p.top, b: p.bottom };
    // 探测面板底部与按钮交叠处
    const cy = Math.min(p.bottom - 4, innerHeight - 2);
    const cx = p.left + p.width / 2;
    const el1 = document.elementFromPoint(cx, cy);
    out.probePanelBottom = { x: cx, y: cy, hit: el1 ? (el1.className || el1.tagName) : "null" };
    // 探测按钮顶部中线
    const bb = document.getElementById("btn-process").getBoundingClientRect();
    const el2 = document.elementFromPoint(bb.left + bb.width / 2, bb.top + 4);
    out.probeBtnTop = { y: bb.top + 4, hit: el2 ? (el2.className || el2.tagName) : "null" };
    return JSON.stringify(out);
  })();
  `;
  const res = await send("Runtime.evaluate", { expression: js, returnByValue: true });
  console.log(JSON.stringify(JSON.parse(res.result.value), null, 2));
  ws.close();
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
