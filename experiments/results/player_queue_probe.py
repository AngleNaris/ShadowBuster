"""Focused Qt render/geometry checks for the KFL player revision."""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--no-sandbox --disable-gpu'
import main
from PySide6.QtCore import QTimer, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

root = Path('experiments/results/player_queue_interactions_20260920').resolve()
root.mkdir(exist_ok=True)
app = QApplication([])
main.StudioWindow._persistent_storage_dir = lambda self: root / 'web'
main.StudioWindow._restore_or_default_geometry = lambda self: self.resize(1100, 1000)
w = main.StudioWindow()
w.show()
page = w.view.page()

import shutil
results=[]
source=Path('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav').resolve()
paths=[]
for name in ('第一首.wav','第二首很长的文件名称用来检查省略显示.wav','第三首.wav'):
 path=root/name;shutil.copyfile(source,path);paths.append(path.as_posix())
steps=[
 ("JSON.stringify((()=>{let checks=[];for(const el of document.querySelectorAll('.fader,[id=width-meter]')){const before=+el.getAttribute('aria-valuenow'),step=+el.dataset.step,up=before<+el.dataset.max;el.dispatchEvent(new WheelEvent('wheel',{deltaY:up?-120:120,bubbles:true,cancelable:true}));checks.push(+el.getAttribute('aria-valuenow')!==before);el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));checks.push(+el.getAttribute('aria-valuenow')===+el.dataset.default);el.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));checks.push(+el.getAttribute('aria-valuenow')===Math.min(+el.dataset.max,+el.dataset.default+step));}return {name:'faders wheel reset keyboard',ok:checks.length>=9&&checks.every(Boolean),checks};})())",False),
 ("document.getElementById('player-file-picker').dispatchEvent(new WheelEvent('wheel',{deltaY:-120,bubbles:true,cancelable:true}));JSON.stringify({name:'wheel previous',ok:document.getElementById('player-file-name').textContent.startsWith('第二首')&&document.getElementById('draft-play').dataset.playing==='false'})",False),
 ("document.getElementById('player-file-trigger').click();JSON.stringify({name:'popover opens',ok:document.getElementById('player-file-menu').matches(':popover-open')})",True),
 ("JSON.stringify((()=>{const r=document.getElementById('player-file-menu').getBoundingClientRect();return {name:'popover anchored inside viewport',ok:r.x>=0&&r.right<=innerWidth&&r.y>=0&&r.bottom<=innerHeight&&document.getElementById('player-file-trigger').getAttribute('aria-expanded')==='true'};})())",False),
 ("document.querySelector('#file-list .fi-name').click();JSON.stringify({name:'select closes menu',ok:document.getElementById('player-file-name').textContent==='第一首.wav'&&!document.getElementById('player-file-menu').matches(':popover-open')})",False),
 ("document.getElementById('player-file-trigger').click();document.querySelector('#file-list .fi-x').click();JSON.stringify({name:'remove starts',ok:true})",False),
 ("JSON.stringify({name:'remove selected file',ok:document.querySelectorAll('#file-list .file-item').length===2&&document.getElementById('player-file-name').textContent.startsWith('第二首')})",False),
 ("document.getElementById('btn-clear').click();JSON.stringify({name:'clear queue',ok:document.querySelectorAll('#file-list .file-item').length===0&&document.getElementById('player-file-name').textContent==='添加歌曲…'})",False),
]
def run():
 if not steps:
  w.bridge.filesDropped.emit(paths[:1]);QTimer.singleShot(300,finish);return
 script,screenshot=steps.pop(0)
 def checked(raw):
  results.append(json.loads(raw))
  if screenshot:QTimer.singleShot(400,lambda:w.grab().save(str(root/'menu.png')))
  QTimer.singleShot(700,run)
 page.runJavaScript(script,checked)
def finish():
 page.runJavaScript("JSON.stringify({name:'add after clear',ok:document.querySelectorAll('#file-list .file-item').length===1,errors:window.probeErrors})",save)
def save(raw):
 results.append(json.loads(raw));(root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(results,ensure_ascii=False),flush=True);app.exit(int(any(not r['ok'] or r.get('errors') for r in results)))
def setup():
 page.runJavaScript("window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message))")
 w.bridge.filesDropped.emit(paths);QTimer.singleShot(700,run)
QTimer.singleShot(1500,setup);QTimer.singleShot(20000,lambda:app.exit(2));sys.exit(app.exec())
