import os,sys,json,shutil,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--no-sandbox --disable-gpu --autoplay-policy=no-user-gesture-required'
root=Path('experiments/results/full_draft_20260920').resolve();root.mkdir(parents=True,exist_ok=True)
os.environ['SB_PROCESSING_CACHE_DIR']=str(root/'cache')
import numpy as np,soundfile as sf
import main,prepared_audio
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtWebEngineCore import QWebEngineScript
source=root/'whole_song.wav'
x,sr=sf.read('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav');sf.write(source,np.tile(x,(3,1)),sr,subtype='FLOAT')
calls={'lew':0,'demucs':0}
def lew(b,inp,out,**kw):
 calls['lew']+=1;shutil.copyfile(inp,out)
def demucs(b,inp,out,model='htdemucs',**kw):
 calls['demucs']+=1;dst=Path(out)/model/Path(inp).stem;dst.mkdir(parents=True,exist_ok=True)
 a,r=sf.read(inp)
 for name in ('bass','drums','vocals','other'):sf.write(dst/(name+'.wav'),a/4,r,subtype='FLOAT')
 return dst
prepared_audio.lew=lew;prepared_audio.demucs=demucs
class ProbePage(main.QWebEnginePage):
 def javaScriptConsoleMessage(self, level, message, line, source):
  print('JS',message,line,source,flush=True)
main.QWebEnginePage=ProbePage
app=QApplication([]);main.StudioWindow._restore_or_default_geometry=lambda self:self.resize(1100,1050)
main.StudioWindow._persistent_storage_dir=lambda self:root/'web'
w=main.StudioWindow();w.show();page=w.view.page()
s=QWebEngineScript();s.setName('probe');s.setInjectionPoint(QWebEngineScript.DocumentCreation);s.setWorldId(QWebEngineScript.MainWorld)
s.setSourceCode("window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>probeErrors.push(String(e.reason)));")
page.scripts().insert(s);w.view.reload()
def setup():
 page.runJavaScript("const oldLoad=DraftPlayer.prototype.load;DraftPlayer.prototype.load=async function(p){await oldLoad.call(this,p);this.node.disconnect();const mute=this.context.createGain();mute.gain.value=0;this.node.connect(mute);mute.connect(this.context.destination);};")
 w.bridge.filesDropped.emit([source.as_posix()])
QTimer.singleShot(1500,setup)
QTimer.singleShot(3000,lambda:page.runJavaScript("document.getElementById('draft-prepare').click()"))
phase=0;start=time.monotonic();observed=[]
def check(raw):
 global phase
 p=json.loads(raw) if raw else {};observed.append(p)
 if p.get('errors'):finish(1,p);return
 if time.monotonic()-start>35:finish(2,p);return
 if phase==0 and p.get('ready'):
  assert float(p['max'])>30
  phase=1;page.runJavaScript("document.getElementById('draft-play').click()")
 elif phase==1 and float(p.get('position',0))>5.5:
  phase=2;page.runJavaScript("const seek=document.getElementById('draft-seek');seek.value=32;seek.dispatchEvent(new Event('input'));seek.dispatchEvent(new Event('change'));")
 elif phase==2 and float(p.get('position',0))>33:
  phase=3
 elif phase==3 and p.get('play')=='播放草稿':
  finish(0,p)
def finish(code,p):
 timer.stop();result={'exit':code,'state':p,'calls':calls,'observed':observed}
 (root/'ui_stream.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 w.grab().save(str(root/'player.png'));print(json.dumps({'exit':code,'state':p,'calls':calls},ensure_ascii=False),flush=True);app.exit(code)
timer=QTimer();timer.timeout.connect(lambda:page.runJavaScript("document.getElementById('draft-play') && JSON.stringify({ready:!document.getElementById('draft-play').disabled,max:document.getElementById('draft-seek').max,position:document.getElementById('draft-seek').value,play:document.getElementById('draft-play').textContent,time:document.getElementById('draft-time').textContent,status:document.getElementById('draft-status').textContent,errors:probeErrors})",check));timer.start(200)
sys.exit(app.exec())
