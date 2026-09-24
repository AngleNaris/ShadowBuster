import os,sys,json,shutil,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--no-sandbox --disable-gpu --autoplay-policy=no-user-gesture-required'
root=Path('experiments/results/player_v2_light_20260920').resolve();root.mkdir(parents=True,exist_ok=True)
os.environ['SB_PROCESSING_CACHE_DIR']=str(root/'cache')
import numpy as np,soundfile as sf
import main,prepared_audio
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
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
app=QApplication([]);main.StudioWindow._restore_or_default_geometry=lambda self:self.resize(1100,1050)
main.StudioWindow._persistent_storage_dir=lambda self:root/'web'
w=main.StudioWindow();w.show();page=w.view.page()
s=QWebEngineScript();s.setName('probe');s.setInjectionPoint(QWebEngineScript.DocumentCreation);s.setWorldId(QWebEngineScript.MainWorld)
s.setSourceCode("localStorage.setItem('sb_param_ui_mode','light');localStorage.setItem('sb_param_output',"+json.dumps(str(out))+ ");window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>probeErrors.push(String(e.reason)));")
page.scripts().insert(s);w.view.reload()
def setup():
 page.runJavaScript("document.getElementById('player-audio').muted=true;const oldLoad=DraftPlayer.prototype.load;DraftPlayer.prototype.load=async function(p){await oldLoad.call(this,p);this.node.disconnect();const mute=this.context.createGain();mute.gain.value=0;this.node.connect(mute);mute.connect(this.context.destination);};")
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
  phase=1;page.runJavaScript("document.getElementById('draft-play').click()")
 elif phase==1 and float(p.get('position',0))>1:
  phase=2;page.runJavaScript("document.getElementById('knob-guitar').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));document.getElementById('knob-guidance').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));document.querySelector('#player-sources button').click();")
 elif phase==2 and p.get('mode')=='原声' and float(p.get('position',0))>1.5:
  phase=3;page.runJavaScript("[...document.querySelectorAll('#player-sources button')].find(b=>b.textContent==='V1').click()")
 elif phase==3 and p.get('mode')=='V1' and float(p.get('position',0))>2:
  phase=4;page.runJavaScript("[...document.querySelectorAll('#player-sources button')].find(b=>b.textContent==='草稿').click()")
 elif phase==4 and p.get('mode')=='草稿' and float(p.get('position',0))>2.5:
  phase=5;page.runJavaScript("const a=document.getElementById('player-a'),b=document.getElementById('player-b');a.value='0:01';a.dispatchEvent(new Event('change'));b.value='0:02';b.dispatchEvent(new Event('change'));document.getElementById('draft-loop').click();")
 elif phase==5 and p.get('loop')=='true' and 1<=float(p.get('position',0))<2:
  phase=6;QTimer.singleShot(1600,lambda:finish(0,p))

def finish(code,p):
 timer.stop();result={'exit':code,'state':p,'calls':calls,'observed':observed}
 (root/'ui_stream.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 w.grab().save(str(root/'player.png'));page.runJavaScript("document.querySelector('#settings-modal [data-mode=light]').click()");QTimer.singleShot(250,lambda:(w.grab().save(str(root/'player_light.png')),app.exit(code)));print(json.dumps({'exit':code,'state':p,'calls':calls},ensure_ascii=False),flush=True)
timer=QTimer();timer.timeout.connect(lambda:page.runJavaScript("document.getElementById('draft-play') && JSON.stringify({ready:document.getElementById('player').dataset.cache==='ready',max:document.getElementById('draft-seek').max,position:document.getElementById('draft-seek').value,play:document.getElementById('draft-play').dataset.playing,time:document.getElementById('draft-time').textContent,status:document.getElementById('draft-status').textContent,cache:document.getElementById('player').dataset.cache,guitar:document.getElementById('knob-guitar').getAttribute('aria-valuenow'),guidance:document.getElementById('knob-guidance').getAttribute('aria-valuenow'),mode:document.getElementById('player-mode').textContent,loop:document.getElementById('draft-loop').getAttribute('aria-pressed'),previewTab:!!document.getElementById('tab-preview'),title:!!document.getElementById('draft-title'),errors:probeErrors})",check));timer.start(200)
sys.exit(app.exec())
