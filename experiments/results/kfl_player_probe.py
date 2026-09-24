"""Focused Qt render/geometry checks for the KFL player revision."""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
# Native Windows rendering matches the development app.

import main
from PySide6.QtCore import QTimer, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

root = Path('experiments/results/player_compact_layout').resolve()
root.mkdir(exist_ok=True)
app = QApplication([])
main.StudioWindow._persistent_storage_dir = lambda self: root / 'web'
main.StudioWindow._restore_or_default_geometry = lambda self: self.resize(1100, 1000)
w = main.StudioWindow()
w.show()
page = w.view.page()
results = []
cases = [(theme, width, zoom) for theme in ('dark', 'light')
         for width, zoom in [(840, 1), (900, 1), (967, 1), (969, 1), (980, 1), (1024, 1),
                             (1100, 1), (1440, 1), (1920, 1), (840, 2)]]
geometry = r"""JSON.stringify((()=>{
 const player=document.getElementById('player'), rect=player.getBoundingClientRect();
 const nodes=[...player.querySelectorAll('button,input,select,.player-readout,[role=slider]')].filter(e=>e.getClientRects().length&&!e.closest('dialog'));
 const controls=nodes.map(e=>{const r=e.getBoundingClientRect();return {id:e.id||e.textContent,w:r.width,h:r.height,x:r.x,y:r.y,bottom:r.bottom,right:r.right,font:parseFloat(getComputedStyle(e).fontSize)};});
 const groups=[...player.querySelectorAll('.player-main-controls,.player-source-controls,.player-monitor-controls')];
 const groupAlignment=groups.every(g=>{const rs=[...g.children].map(e=>e.getBoundingClientRect());return rs.every(r=>rs.filter(other=>Math.abs(other.top-r.top)<1).every(other=>Math.abs(other.bottom-r.bottom)<1));});
 const lane=document.getElementById('player-loop-lane').getBoundingClientRect(),ruler=document.getElementById('player-ruler').getBoundingClientRect();
 return {rect:{x:rect.x,y:rect.y,w:rect.width,h:rect.height},controls,
 overflow:controls.filter(e=>e.x<rect.x-1||e.right>rect.right+1),
 undersized:controls.filter(e=>e.h<43.9),
 alignment:Math.abs(lane.x-ruler.x)<1&&Math.abs(lane.right-ruler.right)<1,groupAlignment,
 status:document.getElementById('player-cache-description').textContent,
 theme:document.documentElement.dataset.mode,background:getComputedStyle(player).backgroundImage,
 errors:window.probeErrors||[]};})())"""

def next_case():
    if not cases:
        interaction()
        return
    theme, width, zoom = cases.pop(0)
    w.resize(width, 1000)
    w.view.setZoomFactor(zoom)
    page.runJavaScript(f"document.documentElement.dataset.mode='{theme}'")
    w.view.update()
    QTimer.singleShot(700, lambda: page.runJavaScript(geometry, lambda raw: record(raw, theme, width, zoom)))

def record(raw, theme, width, zoom):
    result = json.loads(raw)
    result.update(theme=theme, width=width, zoom=zoom)
    results.append(result)
    def capture():
        if width in (840, 1100) and zoom == 1:
            r = result['rect']
            w.view.grab(QRect(round(r['x']), round(r['y']), round(r['w']), round(r['h']))).save(str(root/f'{theme}_{width}.png'))
        next_case()
    QTimer.singleShot(200, capture)

def interaction():
    w.view.setZoomFactor(1)
    w.resize(1100, 1000)
    page.runJavaScript("JSON.stringify((()=>{const k=document.getElementById('player-volume');k.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));const key=k.getAttribute('aria-valuenow')==='95'&&Math.abs(document.getElementById('player-audio').volume-.95)<.001;k.dispatchEvent(new WheelEvent('wheel',{deltaY:1,bubbles:true,cancelable:true}));const wheel=k.getAttribute('aria-valuenow')==='90';k.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));return {volumeKey:key,volumeWheel:wheel,volumeReset:k.getAttribute('aria-valuenow')==='100',deltaDisabled:document.getElementById('player-delta').disabled};})())",lambda raw:results.append(json.loads(raw)))
    page.runJavaScript("document.querySelector('#player-sources button').focus();document.querySelector('#player-sources button').click();")
    QTimer.singleShot(350, open_editor)

def open_editor():
    page.runJavaScript("JSON.stringify({sourceFocus:document.activeElement.dataset.source==='original'})", lambda raw: results.append(json.loads(raw)))
    page.runJavaScript("document.getElementById('player-loop-lane').focus();document.getElementById('player-loop-lane').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));document.getElementById('player-a').value='0:30';document.getElementById('player-b').value='0:02';document.getElementById('player-loop-form').requestSubmit();")
    QTimer.singleShot(150, check_invalid)

def check_invalid():
    def checked(raw):
        results.append(json.loads(raw))
        QTest.keyClick(w.view.focusProxy(), Qt.Key.Key_Escape)
        QTimer.singleShot(150, check_focus)
    page.runJavaScript("JSON.stringify({invalid:document.getElementById('player-loop-editor').open&&!!document.getElementById('player-loop-error').textContent,preserved:document.getElementById('player-a').value==='0:30'})", checked)

def check_focus():
    page.runJavaScript("JSON.stringify({closed:!document.getElementById('player-loop-editor').open,focus:document.activeElement.id==='player-loop-lane'})", lambda raw: results.append(json.loads(raw)))
    # Real bridge failure event, with the current initial request id, verifies rendering.
    page.runJavaScript('window.probeDraftId',lambda request_id:w.bridge.draftFailed.emit(json.dumps({'id':request_id,'error':'测试：无法读取缓存，请重新准备'})))
    QTimer.singleShot(150, finish)

def finish():
    page.runJavaScript("JSON.stringify({errorVisible:!document.getElementById('draft-status').hidden,errorStatus:document.getElementById('player-cache-description').textContent==='缓存准备失败'})", save)

def save(raw):
    results.append(json.loads(raw))
    (root/'audit.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    failed=[r for r in results if r.get('overflow') or r.get('undersized') or r.get('alignment') is False or r.get('errors') or any(v is False for v in r.values())]
    print(json.dumps({'cases':len(results),'failed':failed}, ensure_ascii=False), flush=True)
    app.exit(bool(failed))

def setup():
    page.runJavaScript("const lookup=window.pyApi.draftLookup;window.pyApi.draftLookup=(path,params,id)=>{window.probeDraftId=id;return lookup(path,params,id);};window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));")
    source=Path('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav').resolve()
    w.bridge.filesDropped.emit([source.as_posix()])
    QTimer.singleShot(700, next_case)

QTimer.singleShot(1500, setup)
QTimer.singleShot(45000, lambda: app.exit(2))
sys.exit(app.exec())
