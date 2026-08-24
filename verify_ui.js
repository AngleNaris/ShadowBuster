const http = require("http");
const fs = require("fs");

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
  const evalJs = async (js) => {
    const r = await send("Runtime.evaluate", { expression: js, returnByValue: true });
    return r.result.value;
  };
  await send("Runtime.enable");
  await send("Page.enable");
  await new Promise((r) => setTimeout(r, 1000));

  // 1) 四边边距
  console.log("margins:", await evalJs(`
  (function(){
    const R = (s) => { const r = document.querySelector(s).getBoundingClientRect(); return { l: +r.left.toFixed(1), t: +r.top.toFixed(1), r: +r.right.toFixed(1), b: +r.bottom.toFixed(1) }; };
    return JSON.stringify({ inner: { w: innerWidth, h: innerHeight }, queue: R(".queue"), dock: R(".dock") });
  })();
  `));

  // 2) 下拉遮挡检测
  await evalJs(`document.getElementById("dd-genre-trigger").click();`);
  await new Promise((r) => setTimeout(r, 300));
  console.log("dropdown:", await evalJs(`
  (function(){
    const panel = document.getElementById("dd-genre-panel");
    panel.scrollTop = panel.scrollHeight;
    const p = panel.getBoundingClientRect();
    const opts = panel.querySelectorAll(".dd-opt");
    const last = opts[opts.length - 1].getBoundingClientRect();
    const cx = last.left + last.width / 2, cy = last.top + last.height / 2;
    const el = document.elementFromPoint(cx, cy);
    return JSON.stringify({ panelB: p.bottom, lastOptVisible: cy <= innerHeight, hit: el ? el.className : "null" });
  })();
  `));
  const shot1 = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync("ui_check3_dd.png", Buffer.from(shot1.data, "base64"));
  await evalJs(`document.getElementById("dd-genre-trigger").click();`); // 关闭

  // 3) 帮助弹窗：打开
  await evalJs(`document.querySelector('.rack-help[data-help="lew"]').click();`);
  await new Promise((r) => setTimeout(r, 300));
  const posBefore = await evalJs(`
  (function(){ const r = document.getElementById("help-dialog").getBoundingClientRect();
    return JSON.stringify({ l: r.left, t: r.top, open: document.getElementById("help-dialog").classList.contains("open") }); })();
  `);
  console.log("help before drag:", posBefore);

  // 4) 模拟拖动标题栏
  const hb = JSON.parse(await evalJs(`
  (function(){ const r = document.getElementById("help-drag").getBoundingClientRect();
    return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })();
  `));
  const toX = Math.max(20, hb.x - 150), toY = Math.max(20, hb.y - 80);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: hb.x, y: hb.y, button: "left", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: toX, y: toY, button: "left" });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: toX, y: toY, button: "left" });
  await new Promise((r) => setTimeout(r, 200));
  const posAfter = await evalJs(`
  (function(){ const r = document.getElementById("help-dialog").getBoundingClientRect();
    return JSON.stringify({ l: r.left, t: r.top }); })();
  `);
  console.log("help after drag:", posAfter);
  const shot2 = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync("ui_check3_help.png", Buffer.from(shot2.data, "base64"));

  // 5) 关闭
  await evalJs(`document.getElementById("help-close").click();`);
  await new Promise((r) => setTimeout(r, 300));
  console.log("help closed:", await evalJs(`
  (function(){ const d = document.getElementById("help-dialog");
    return JSON.stringify({ open: d.classList.contains("open"), vis: getComputedStyle(d).visibility }); })();
  `));
  const shot3 = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync("ui_check3_final.png", Buffer.from(shot3.data, "base64"));
  console.log("screenshots saved");
  ws.close();
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
