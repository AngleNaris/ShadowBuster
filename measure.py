import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtCore import QUrl, QTimer, QSize
import json

HTML = r"D:\_3.AI\audio_upscale\SorenStudio\ui\index.html"

app = QApplication(sys.argv)
view = QWebEngineView()
view.setFixedSize(760, 1000)
settings = view.settings()
settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)

def run_measure():
    js = r"""
    (function(){
      const out = {};
      const stages = document.querySelector('.stages');
      const pipeline = document.querySelector('.pipeline');
      const studio = document.querySelector('.studio');
      if (stages) {
        const r = stages.getBoundingClientRect();
        out.stages = {x:r.x,y:r.y,w:r.width,h:r.height};
      }
      if (pipeline) {
        const r = pipeline.getBoundingClientRect();
        out.pipeline = {x:r.x,y:r.y,w:r.width,h:r.height};
      }
      if (studio) {
        const r = studio.getBoundingClientRect();
        out.studio = {x:r.x,y:r.y,w:r.width,h:r.height};
      }
      out.items = [];
      document.querySelectorAll('.stage').forEach(s => {
        const r = s.getBoundingClientRect();
        const dot = s.querySelector('.stage-dot');
        const dr = dot ? dot.getBoundingClientRect() : null;
        out.items.push({
          text: s.innerText.trim(),
          x:r.x, y:r.y, w:r.width, h:r.height,
          dotX: dr ? dr.x : null, dotY: dr ? dr.y : null, dotW: dr ? dr.width : null,
          cx: r.x + r.width/2
        });
      });
      return JSON.stringify(out);
    })();
    """
    view.page().runJavaScript(js, lambda res: on_done(res))

def on_done(res):
    data = json.loads(res)
    print("=== LAYOUT MEASUREMENT ===")
    for k in ("studio","pipeline","stages"):
        if k in data:
            v = data[k]
            print(f"{k:9s}: x={v['x']:.1f} y={v['y']:.1f} w={v['w']:.1f} h={v['h']:.1f}")
    print("--- stage items ---")
    for i, it in enumerate(data["items"]):
        print(f"  [{i}] '{it['text']}' item x={it['x']:.1f} w={it['w']:.1f} cx={it['cx']:.1f} | dotX={it['dotX']:.1f} dotY={it['dotY']:.1f} dotW={it['dotW']:.1f}")
    # compute gaps
    items = data["items"]
    if len(items) >= 2:
        gaps = []
        for i in range(1, len(items)):
            g = items[i]["x"] - (items[i-1]["x"] + items[i-1]["w"])
            gaps.append(g)
        print("--- inter-item gaps (item right -> next item left) ---")
        print("  gaps:", [f"{g:.1f}" for g in gaps])
        # edge gaps
        if "stages" in data:
            st = data["stages"]
            left_edge = items[0]["x"] - st["x"]
            right_edge = (st["x"] + st["w"]) - (items[-1]["x"] + items[-1]["w"])
            print(f"  left edge gap: {left_edge:.1f}  right edge gap: {right_edge:.1f}")
    app.quit()

view.loadFinished.connect(lambda ok: QTimer.singleShot(300, run_measure))
view.load(QUrl.fromLocalFile(HTML))
app.exec()
