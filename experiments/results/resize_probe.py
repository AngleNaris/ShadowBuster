"""Native Qt resize and pointer regression probe (default GPU path)."""
import os, sys, json
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import main
from PySide6.QtCore import QTimer, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtQuickWidgets import QQuickWidget

root = Path('experiments/results/resize_' + os.environ.get('QSG_RHI_BACKEND', 'native')).resolve()
root.mkdir(exist_ok=True)
app = QApplication([])
main.StudioWindow._persistent_storage_dir = lambda self: root / 'web'
main.StudioWindow._restore_or_default_geometry = lambda self: self.resize(1100, 850)
main.StudioWindow.closeEvent = lambda self, event: event.accept()
w = main.StudioWindow()
w.show()
page = w.view.page()
results = []
sizes = [(1100,850),(1000,820),(960,700),(840,600),(980,820),(1440,1000),(1000,820),'maximize','restore']
query = """JSON.stringify((()=>{const b=document.getElementById('tab-report'),r=b.getBoundingClientRect();return {inner:[innerWidth,innerHeight],dpr:devicePixelRatio,scroll:[scrollX,scrollY],tab:{x:r.x,y:r.y,w:r.width,h:r.height},hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.id,report:!document.getElementById('pane-report').hidden,body:document.body.getBoundingClientRect().toJSON()};})())"""

def sample():
    page.runJavaScript(query, clicked)

def clicked(raw):
    data = json.loads(raw)
    data['qt'] = [w.view.width(),w.view.height()]
    data['renderers'] = [str(q.quickWindow().rendererInterface().graphicsApi()) for q in w.findChildren(QQuickWidget)]
    results.append(data)
    r=data['tab']
    QTest.mouseClick(w.view.focusProxy(), Qt.LeftButton, Qt.NoModifier, QPoint(round(r['x']+r['w']/2),round(r['y']+r['h']/2)))
    QTimer.singleShot(400, verify)

def verify():
    page.runJavaScript(query, checked)

def checked(raw):
    data=json.loads(raw)
    results[-1]['afterClick']=data
    w.view.grab().save(str(root/f'case{len(results)}.png'))
    page.runJavaScript("document.getElementById('tab-process').click()")
    QTimer.singleShot(300, advance)

def advance():
    if not sizes:
        (root/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        passed = all(r['afterClick']['report'] and r['qt']==r['inner'] and r['hit']=='tab-report' for r in results)
        print(json.dumps({'cases':len(results),'passed':passed,'renderers':results[0]['renderers']}),flush=True)
        w.close(); app.exit(0 if passed else 1); return
    size=sizes.pop(0)
    if isinstance(size,str):
        w.showMaximized() if size=='maximize' else w.showNormal()
        QTimer.singleShot(500,sample)
        return
    width,height=size
    # Drive successive frames through the breakpoint, not only final geometry.
    start=w.size()
    frames=[(round(start.width()+(width-start.width())*i/15),round(start.height()+(height-start.height())*i/15)) for i in range(1,16)]
    def frame():
        if frames:
            w.resize(*frames.pop(0)); QTimer.singleShot(16,frame)
        else: QTimer.singleShot(400,sample)
    frame()

page.loadFinished.connect(lambda ok: QTimer.singleShot(1500,advance))
QTimer.singleShot(30000,app.quit)
sys.exit(app.exec())
