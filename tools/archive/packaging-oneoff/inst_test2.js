const http = require("http");
function getTargets() {
  return new Promise((resolve, reject) => {
    http.get("http://localhost:9223/json", (res) => {
      let data = ""; res.on("data", c => data += c);
      res.on("end", () => resolve(JSON.parse(data)));
    }).on("error", reject);
  });
}
async function main() {
  const targets = await getTargets();
  const page = targets.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = {};
  const send = (method, params) => new Promise(res => {
    const m = ++id; pending[m] = res;
    ws.send(JSON.stringify({ id: m, method, params: params || {} }));
  });
  await new Promise(r => ws.addEventListener("open", r));
  ws.addEventListener("message", e => {
    const m = JSON.parse(e.data);
    if (m.id && pending[m.id]) pending[m.id](m.result);
  });
  const evalJs = async js => (await send("Runtime.evaluate", { expression: js, returnByValue: true, awaitPromise: true })).result.value;
  await send("Runtime.enable");
  await new Promise(r => setTimeout(r, 500));
  const inWav = "C:/Users/Administrator/AppData/Local/Temp/sb_e2e/in.wav";
  const outDir = "C:/Users/Administrator/AppData/Local/Temp/sb_e2e/out_inst";
  console.log("process:", await evalJs(`window.pyApi.process({files:["${inWav}"], output:"${outDir}", genre:"Pop", loudness:"normal", eq:"Neutral", quality:1, guidance:1.5, sub:4, sat:0.35})`));
  await new Promise(r => setTimeout(r, 20000));
  ws.close(); process.exit(0);
}
main().catch(e => { console.error(e); process.exit(1); });
