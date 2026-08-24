const http = require("http");
http.get("http://localhost:9223/json", (res) => {
  let data = ""; res.on("data", c => data += c);
  res.on("end", async () => {
    const page = JSON.parse(data).find(t => t.type === "page");
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0; const pending = {};
    ws.addEventListener("message", e => {
      const m = JSON.parse(e.data);
      if (m.id && pending[m.id]) pending[m.id](m.result);
    });
    await new Promise(r => ws.addEventListener("open", r));
    const send = (method, params) => new Promise(res => {
      const m = ++id; pending[m] = res;
      ws.send(JSON.stringify({ id: m, method, params: params || {} }));
    });
    await send("Runtime.enable");
    await send("Runtime.evaluate", { expression: "window.pyApi.cancel()" });
    console.log("cancel sent");
    ws.close(); process.exit(0);
  });
});
