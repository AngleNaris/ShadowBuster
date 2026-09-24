import os,sys,json,shutil,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--no-sandbox --disable-gpu --autoplay-policy=no-user-gesture-required'
root=Path('experiments/results/feedback_keyboard_'+os.environ.get('SB_PROBE_THEME','dark')+'_'+os.environ.get('SB_PROBE_WIDTH','1100')+'_20260920').resolve();root.mkdir(parents=True,exist_ok=True)
os.environ['SB_PROCESSING_CACHE_DIR']=str(root/'cache')
import numpy as np,soundfile as sf
import main,prepared_audio
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEngineScript
source=root/'whole_song.wav'
x,sr=sf.read('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav');sf.write(source,np.tile(x,(3,1)),sr,subtype='FLOAT')
out=root/'out';out.mkdir(exist_ok=True);shutil.copyfile(source,out/'whole_song_shadowbuster.wav')
calls={'lew':0,'demucs':0}
def lew(b,inp,out,**kw):
 calls['lew']+=1;shutil.copyfile(inp,out)
def demucs(b,inp,out,model='htdemucs',**kw):
 calls['demucs']+=1;dst=Path(out)/model/Path(inp).stem;dst.mkdir(parents=True,exist_ok=True)
 a,r=sf.read(inp)
 for name in ('bass','drums','vocals','other','guitar','piano'):sf.write(dst/(name+'.wav'),a/6,r,subtype='FLOAT')
 return dst
prepared_audio.lew=lew;prepared_audio.demucs=demucs
class ProbePage(main.QWebEnginePage):
 def javaScriptConsoleMessage(self, level, message, line, source):
  print('JS',message,line,source,flush=True)
main.QWebEnginePage=ProbePage
app=QApplication([]);main.StudioWindow._restore_or_default_geometry=lambda self:self.resize(int(os.environ.get('SB_PROBE_WIDTH','1100')),1050)
main.StudioWindow._persistent_storage_dir=lambda self:root/'web'
w=main.StudioWindow();w.show();page=w.view.page()
s=QWebEngineScript();s.setName('probe');s.setInjectionPoint(QWebEngineScript.DocumentCreation);s.setWorldId(QWebEngineScript.MainWorld)
s.setSourceCode("localStorage.setItem('sb_param_ui_mode',"+json.dumps(os.environ.get('SB_PROBE_THEME','dark'))+");localStorage.setItem('sb_param_output',"+json.dumps(str(out))+ ");window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>probeErrors.push(String(e.reason)));")
page.scripts().insert(s);w.view.reload()
def setup():
 page.runJavaScript("document.getElementById('player-audio').muted=true;window.playTimings=[];const realPlay=DraftPlayer.prototype.play;DraftPlayer.prototype.play=function(value){const at=performance.now();const result=realPlay.call(this,value),revision=this.playRevision;const listener=({data:p})=>{if(p.type==='position'&&p.playRevision===revision){window.playTimings.push({value,ms:performance.now()-at});this.node.port.removeEventListener('message',listener);}};this.node.port.addEventListener('message',listener);return result;};const oldLoad=DraftPlayer.prototype.load;DraftPlayer.prototype.load=async function(p){await oldLoad.call(this,p);this.node.disconnect();const mute=this.context.createGain();mute.gain.value=0;this.node.connect(mute);mute.connect(this.context.destination);};")
 w.bridge.filesDropped.emit([source.as_posix()])
QTimer.singleShot(1500,setup)
QTimer.singleShot(3000,lambda:page.runJavaScript("document.querySelector('[data-source=draft]').click()"))

results=[];phase=0;started=time.monotonic();last=0
steps=[
 ("document.getElementById('player-volume').focus();document.getElementById('player-volume').dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true,cancelable:true}));", "document.getElementById('draft-play').dataset.playing==='true'", 'space on knob starts'),
 ("document.querySelector('[data-source=original]').click();", "document.getElementById('draft-play').dataset.playing==='true'", 'original keeps playing'),
 ("document.querySelector('[data-source=draft]').click();", "document.getElementById('draft-play').dataset.playing==='true'&&!document.getElementById('player-track').classList.contains('buffering')", 'draft switch no buffering'),
 ("document.getElementById('draft-loop').focus();document.getElementById('draft-loop').dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true,cancelable:true}));", "document.getElementById('draft-play').dataset.playing==='false'&&document.getElementById('draft-loop').getAttribute('aria-pressed')==='false'", 'space on button pauses only'),
 ("document.getElementById('player-loop-lane').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));document.getElementById('player-a').dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true,cancelable:true}));", "document.getElementById('player-loop-editor').open&&document.getElementById('draft-play').dataset.playing==='true'", 'space in modal starts'),
 ("document.getElementById('player-a').dispatchEvent(new KeyboardEvent('keydown',{code:'Space',repeat:true,bubbles:true,cancelable:true}));", "document.getElementById('draft-play').dataset.playing==='true'", 'held space no repeat'),
 ("document.getElementById('player-loop-editor').close();document.querySelector('[data-source=original]').click();", "document.getElementById('knob-quality').getAttribute('aria-disabled')==='false'", 'original unlocks quality'),
 ("document.querySelector('[data-source=draft]').click();window.oldQuality=document.getElementById('knob-quality').getAttribute('aria-valuenow');document.getElementById('knob-quality').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));", "document.getElementById('knob-quality').getAttribute('aria-valuenow')===window.oldQuality", 'draft locks quality'),
]
def poll(raw):
 global phase
 p=json.loads(raw)
 if p['busy'] and not any(r['name']=='buster locked' for r in results):
  results.append({'name':'buster locked','ok':p['buster']})
 if p['ready'] and phase==0:
  phase=1;timer.stop();run_step()
 elif time.monotonic()-started>35:finish(2)
def run_step():
 if not steps:finish(0);return
 action,condition,name=steps.pop(0)
 page.runJavaScript(action)
 QTimer.singleShot(350,lambda:page.runJavaScript('JSON.stringify({ok:!!('+condition+'),position:+document.getElementById("draft-seek").value})',lambda raw:checked(raw,name)))
def checked(raw,name):
 r=json.loads(raw);r['name']=name;results.append(r);run_step()
def finish(code):
 timer.stop();page.runJavaScript("if(document.getElementById('draft-play').dataset.playing==='true')document.getElementById('draft-play').click()")
 code=code or int(not all(r['ok'] for r in results))
 page.runJavaScript("JSON.stringify(window.playTimings)",lambda raw:(root/'latency.json').write_text(raw,encoding='utf-8'))
 (root/'keyboard.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps({'exit':code,'results':results}),flush=True)
 QTimer.singleShot(1500,lambda:app.exit(code))
timer=QTimer();timer.timeout.connect(lambda:page.runJavaScript("JSON.stringify({busy:document.getElementById('player').dataset.cache==='preparing',ready:document.getElementById('player').dataset.cache==='ready',buster:document.getElementById('btn-process').disabled})",poll));timer.start(100)
sys.exit(app.exec())
