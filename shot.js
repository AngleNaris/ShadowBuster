/* CDP 截图工具：node shot.js [out.png] [--js=...]
   连接 localhost:9223，可选先执行 JS，再整窗截图。 */
const http = require("http");
const fs = require("fs");

const outFile = process.argv[2] || "ui_shot.png";
const jsFlag = process.argv.find((a) => a.startsWith("--js="));
const js = jsFlag ? jsFlag.slice(5) : null;
const PORT = process.env.CDP_PORT || 9223;

function getTargets() {
  return new Promise((resolve, reject) => {
    http.get(`http://localhost:${PORT}/json`, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve(JSON.parse(data)));
    }).on("error", reject);
  });
}

async function main() {
  const targets = await getTargets();
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("no page target");
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
  if (js) {
    await send("Runtime.evaluate", { expression: js, returnByValue: true });
    await new Promise((r) => setTimeout(r, 300));
  }
  await new Promise((r) => setTimeout(r, 400));
  const shot = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(outFile, Buffer.from(shot.data, "base64"));
  console.log("saved", outFile);
  ws.close();
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
