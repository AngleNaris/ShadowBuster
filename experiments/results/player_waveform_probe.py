"""Native Qt playback controls, waveform and app version menu checks."""
import sys,json,shutil
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import main
from PySide6.QtCore import QTimer, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
root=Path('experiments/results/player_waveform').resolve();root.mkdir(exist_ok=True)
app=QApplication([])
main.StudioWindow._persistent_storage_dir=lambda self:root/'web'
main.StudioWindow._restore_or_default_geometry=lambda self:self.resize(1000,850)
main.StudioWindow.closeEvent=lambda self,event:event.accept()
w=main.StudioWindow();w.show();page=w.view.page();results=[]
source=Path('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav').resolve()
paths=[]
for name in ['原曲.wav','另一首.wav','V1.wav','V2.wav']:
    target=root/name;shutil.copyfile(source,target);paths.append(target.as_posix())
    Path(str(target)+'.quality.json').write_text(json.dumps({'output':{'metrics':{}},'input':{'metrics':{}},'meta':{'generated_at':'2026-09-20T16:00:00'}}),encoding='utf-8')
checks=[
"JSON.stringify((()=>{const bs=[...document.querySelectorAll('#player-sources .player-source:not(.player-version-arrow)')],widths=bs.map(b=>b.offsetWidth),c=document.getElementById('player-waveform'),a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;return {name:'equal widths and real waveform',ok:widths.length===3&&new Set(widths).size===1&&bs[1].textContent==='创建缓存'&&a.some((v,i)=>i%4===3&&v>0),widths,wavePixels:a.filter((v,i)=>i%4===3&&v>0).length};})())",
"document.querySelector('[data-source=versions]').click();JSON.stringify({name:'main selects latest',ok:document.getElementById('player-audio').src.endsWith('V2.wav')&&!document.getElementById('player-version-menu').matches(':popover-open')})",
"document.getElementById('player-version-arrow').click();JSON.stringify({name:'app menu opens',ok:document.getElementById('player-version-menu').matches(':popover-open')&&!document.querySelector('#player-sources select')})",
"JSON.stringify({name:'menu selected focus',ok:document.activeElement.textContent==='成品 V2'})",
"document.querySelector('#player-version-menu button').click();JSON.stringify({name:'choose V1',ok:document.getElementById('player-audio').src.endsWith('V1.wav')&&!document.getElementById('player-version-menu').matches(':popover-open')&&document.activeElement.id==='player-version-arrow'})",
"document.querySelector('[data-source=original]').click();JSON.stringify({name:'original restores',ok:document.getElementById('player-audio').src.includes(encodeURIComponent('另一首.wav'))})",
"document.querySelector('[data-source=versions]').click();JSON.stringify({name:'main remembers V1',ok:document.getElementById('player-audio').src.endsWith('V1.wav')})",
"document.getElementById('player-file-trigger').click();JSON.stringify({name:'file menu opens',ok:document.getElementById('player-file-menu').matches(':popover-open')})",
"JSON.stringify((()=>{const r=document.getElementById('player-file-menu').getBoundingClientRect(),row=document.querySelector('#file-list .file-item'),btn=document.getElementById('btn-add');return {name:'compact file menu',ok:row.offsetHeight===32&&btn.offsetHeight===32&&r.height<200&&r.right<=innerWidth&&r.bottom<=innerHeight,row:row.offsetHeight,button:btn.offsetHeight,height:r.height,transition:getComputedStyle(document.getElementById('player-file-menu')).transitionDuration};})())",
"document.getElementById('player-file-menu').hidePopover();JSON.stringify({name:'fade out retained',ok:getComputedStyle(document.getElementById('player-file-menu')).display!=='none'})",
"JSON.stringify({name:'fade out completed',ok:getComputedStyle(document.getElementById('player-file-menu')).display==='none'})",
"document.documentElement.dataset.mode='light';document.dispatchEvent(new Event('sb-theme'));JSON.stringify({name:'shared well surface',ok:getComputedStyle(document.querySelector('.player-readout')).backgroundColor===getComputedStyle(document.getElementById('player-file-picker')).backgroundColor&&!document.querySelector('[data-help=player]')})",
]
checks += [
"document.getElementById('tab-report').click();window.sameButton=document.querySelector('[data-source=versions]');JSON.stringify({name:'report selector removed',ok:!document.getElementById('rp-chips')&&document.getElementById('rp-legend').parentElement.className==='rp-head-side'})",
"JSON.stringify({name:'report follows V1',ok:document.getElementById('rp-name').textContent==='V1'})",
"document.getElementById('player-version-arrow').click();document.querySelectorAll('#player-version-menu button')[1].click();JSON.stringify({name:'persistent button nodes',ok:window.sameButton===document.querySelector('[data-source=versions]')&&getComputedStyle(window.sameButton).transitionDuration.includes('0.16s')})",
"JSON.stringify({name:'report follows V2',ok:document.getElementById('rp-name').textContent==='V2'})",
"document.querySelector('[data-source=original]').click();JSON.stringify({name:'original preserves report version',ok:document.getElementById('rp-name').textContent==='V2'})",
"JSON.stringify((()=>{const c=document.getElementById('player-waveform').getBoundingClientRect(),l=document.getElementById('player-loop-lane').getBoundingClientRect(),t=document.getElementById('player-track').getBoundingClientRect();return {name:'waveform spans both lanes',ok:c.top<=l.top&&c.bottom>=t.bottom&&Math.abs(c.width-t.width)<1};})())",
]

checks += [
"JSON.stringify((()=>{const lane=getComputedStyle(document.getElementById('player-loop-lane'));return {name:'range mask and divider',ok:lane.backgroundColor!=='rgba(0, 0, 0, 0)'&&lane.boxShadow!=='none',background:lane.backgroundColor,divider:lane.boxShadow};})())",
"document.querySelector('[data-source=versions]').click();JSON.stringify({name:'switch starts fade out',ok:document.getElementById('player-waveform').getAnimations().length===1})",
"JSON.stringify({name:'sequential fade completes',ok:document.getElementById('player-waveform').getAnimations().length===0&&getComputedStyle(document.getElementById('player-waveform')).opacity==='1'})",
"document.querySelector('[data-source=original]').click();document.querySelector('[data-source=versions]').click();document.querySelector('[data-source=original]').click();JSON.stringify({name:'rapid switching starts',ok:document.getElementById('player-waveform').getAnimations().length<=1})",
"JSON.stringify({name:'rapid switch settles on original',ok:document.querySelector('[data-source=original]').getAttribute('aria-pressed')==='true'&&document.getElementById('player-waveform').getAnimations().length===0&&getComputedStyle(document.getElementById('player-waveform')).opacity==='1'})",
"JSON.stringify((()=>{const original=window.matchMedia;window.matchMedia=()=>({matches:true});document.querySelector('[data-source=versions]').click();window.matchMedia=original;return {name:'reduced motion immediate',ok:document.getElementById('player-waveform').getAnimations().length===0&&getComputedStyle(document.getElementById('player-waveform')).opacity==='1'};})())",
"document.documentElement.dataset.mode='dark';document.dispatchEvent(new Event('sb-theme'));document.querySelector('[data-source=original]').click();const region=document.getElementById('player-loop-region');region.hidden=false;region.style.left='30%';region.style.width='30%';JSON.stringify({name:'restore real dark waveform',ok:getComputedStyle(document.getElementById('player-loop-lane'),'::after').pointerEvents==='none'})",

]

def run():
    if not checks:
        drag();return
    script=checks.pop(0)
    def record(raw):
        results.append(json.loads(raw) if raw else {'name':script,'ok':False,'error':'JS expression failed'})
        w.view.grab().save(str(root/f'case{len(results)}.png'))
        QTimer.singleShot(350,run)
    page.runJavaScript(script,record)
def drag():
    page.runJavaScript("JSON.stringify(document.getElementById('draft-seek').getBoundingClientRect().toJSON())",start_drag)
def start_drag(raw):
    r=json.loads(raw);proxy=w.view.focusProxy();y=round(r['y']+r['height']/2)
    first=QPoint(round(r['x']+r['width']*.2),y);last=QPoint(round(r['x']+r['width']*.72),y)
    QTest.mousePress(proxy,Qt.LeftButton,Qt.NoModifier,first)
    QTest.mouseMove(proxy,last,50)
    def held(raw):
        results.append(json.loads(raw));QTest.mouseRelease(proxy,Qt.LeftButton,Qt.NoModifier,last)
        QTimer.singleShot(400,drag_done)
    QTimer.singleShot(150,lambda:page.runJavaScript("JSON.stringify({name:'real drag follows pointer',ok:Math.abs(+document.getElementById('draft-seek').value-8.64)<.1})",held))
def drag_done():
    def done(raw):
        results.append(json.loads(raw));page.runJavaScript('JSON.stringify(window.probeErrors)',finish)
    page.runJavaScript("JSON.stringify({name:'real release commits',ok:Math.abs(document.getElementById('player-audio').currentTime-8.64)<.1,time:document.getElementById('player-audio').currentTime})",done)
def finish(raw):
    w.view.grab().save(str(root/"dark_final.png"))
    errors=json.loads(raw);passed=all(r['ok'] for r in results) and not errors
    (root/'results.json').write_text(json.dumps({'results':results,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':passed,'results':results,'errors':errors},ensure_ascii=False),flush=True)
    w.close();app.exit(0 if passed else 1)
def setup():
    outs=[{'path':paths[2],'version':1},{'path':paths[3],'version':2}]
    page.runJavaScript("window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));window.pyApi.listOutputs=async()=>"+json.dumps(json.dumps({'outputs':outs}))+";window.pyApi.selectOutput=async()=>"+json.dumps(root.as_posix())+";document.getElementById('output-card').click();",lambda _:load())
def load():
    w.bridge.filesDropped.emit(paths[:2]);QTimer.singleShot(1500,run)
page.loadFinished.connect(lambda _:QTimer.singleShot(1200,setup))
QTimer.singleShot(20000,lambda:app.exit(2));sys.exit(app.exec())
