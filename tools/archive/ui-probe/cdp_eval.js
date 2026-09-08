/* CDP 通用评测：node cdp_eval.js "js表达式" [out.json]
   连接 localhost:9223，执行 JS（returnByValue），输出 JSON 到 stdout（或文件）。 */
const http = require("http");
const fs = require("fs");

const js = process.argv[2];
const outFile = process.argv[3];
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
  const evalJs = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result.value;
  };
  await send("Runtime.enable");
  await new Promise((r) => setTimeout(r, 300));
  const val = await evalJs(js);
  if (outFile) fs.writeFileSync(outFile, JSON.stringify(val));
  else console.log(JSON.stringify(val));
  ws.close();
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
