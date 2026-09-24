import os,sys,json,shutil,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--no-sandbox --disable-gpu --autoplay-policy=no-user-gesture-required'
root=Path('experiments/results/feedback_transport_'+os.environ.get('SB_PROBE_THEME','dark')+'_'+os.environ.get('SB_PROBE_WIDTH','1100')+'_20260920').resolve();root.mkdir(parents=True,exist_ok=True)
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
 page.runJavaScript("document.getElementById('player-audio').muted=true;const oldLoad=DraftPlayer.prototype.load;DraftPlayer.prototype.load=async function(p){await oldLoad.call(this,p);this.node.disconnect();const mute=this.context.createGain();mute.gain.value=0;this.node.connect(mute);mute.connect(this.context.destination);};")
 w.bridge.filesDropped.emit([source.as_posix()])
QTimer.singleShot(1500,setup)
QTimer.singleShot(3000,lambda:page.runJavaScript("document.querySelector('[data-source=draft]').click()"))
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
  phase=3;page.runJavaScript("const select=document.querySelector('#player-sources select');select.selectedIndex=1;select.dispatchEvent(new Event('change'))")
 elif phase==3 and p.get('mode')=='V1' and float(p.get('position',0))>2:
  phase=4;page.runJavaScript("[...document.querySelectorAll('#player-sources button')].find(b=>b.textContent==='草稿').click()")
 elif phase==4 and p.get('mode')=='草稿' and float(p.get('position',0))>2.5:
  phase=5;page.runJavaScript("document.getElementById('player-delta').click();");page.runJavaScript("document.getElementById('player-loop-lane').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));document.getElementById('player-a').value='0:01';document.getElementById('player-b').value='0:02';document.getElementById('player-loop-form').requestSubmit();")
 elif phase==5 and p.get('loop')=='true' and 1<=float(p.get('position',0))<2:
  phase=6;QTimer.singleShot(1600,interactions)

def interactions():
 page.runJavaScript(r"""(()=>{
 if(document.getElementById('draft-play').dataset.playing==='true')document.getElementById('draft-play').click();const results=[];const check=(name,ok)=>{results.push({name,ok});if(!ok)throw Error(name);};
 const deltaButton=document.getElementById('player-delta');check('delta active during playback',deltaButton.getAttribute('aria-pressed')==='true'&&!deltaButton.disabled);
 check('buster ready',!document.getElementById('btn-process').disabled);check('quality locked',document.getElementById('knob-quality').getAttribute('aria-disabled')==='true');const oldPosition=+document.getElementById('draft-seek').value;deltaButton.click();check('delta toggle preserves position',Math.abs(+document.getElementById('draft-seek').value-oldPosition)<.1);deltaButton.click();
 const knob=document.getElementById('player-volume'),captureKnob=knob.setPointerCapture;knob.setPointerCapture=()=>{};
 knob.dispatchEvent(new PointerEvent('pointerdown',{pointerId:2,clientY:100,bubbles:true}));knob.dispatchEvent(new PointerEvent('pointermove',{pointerId:2,clientY:150,bubbles:true}));knob.dispatchEvent(new PointerEvent('pointerup',{pointerId:2,bubbles:true}));check('volume drag',+knob.getAttribute('aria-valuenow')<100);knob.setPointerCapture=captureKnob;knob.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));check('volume reset',knob.getAttribute('aria-valuenow')==='100');
 const lane=document.getElementById('player-loop-lane'),rect=lane.getBoundingClientRect();
 const duration=+document.getElementById('draft-seek').max,band=document.getElementById('player-loop-region');const range=()=>[parseFloat(band.style.left)/100*duration,(parseFloat(band.style.left)+parseFloat(band.style.width))/100*duration];const looping=()=>document.getElementById('draft-loop').getAttribute('aria-pressed')==='true';const capture=lane.setPointerCapture;lane.setPointerCapture=()=>{};
 function drag(target,from,to,cancel=false){
  const event=(type,x)=>new PointerEvent(type,{bubbles:true,button:0,clientX:rect.left+x/duration*rect.width,pointerId:1});
  target.dispatchEvent(event('pointerdown',from));lane.dispatchEvent(event('pointermove',to));lane.dispatchEvent(event(cancel?'pointercancel':'pointerup',to));
 }
 drag(lane,5,15);check('create',Math.abs(range()[0]-5)<.01&&Math.abs(range()[1]-15)<.01&&looping());
 drag(lane.querySelector('[data-edge=a]'),5,7);check('resize start',Math.abs(range()[0]-7)<.01);
 drag(lane.querySelector('[data-edge=b]'),15,18);check('resize end',Math.abs(range()[1]-18)<.01);
 drag(document.getElementById('player-loop-label'),12,15);check('move',Math.abs(range()[0]-10)<.01&&Math.abs(range()[1]-21)<.01);
 drag(lane,25,30,true);check('cancel restores',Math.abs(range()[0]-10)<.01);
 lane.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));check('keyboard move',Math.abs(range()[0]-11)<.01);
 const seek=document.getElementById('draft-seek');seek.value=32;seek.dispatchEvent(new Event('input'));seek.dispatchEvent(new Event('change'));check('seek outside disables loop',!looping());
 document.getElementById('player-loop-lane').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));document.getElementById('player-a').value='0:30';document.getElementById('player-b').value='0:02';document.getElementById('player-loop-form').requestSubmit();check('invalid interval rejected',document.getElementById('player-loop-editor').open&&!looping());
 document.getElementById('player-loop-cancel').click();
 lane.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}));check('delete',range()[1]===0&&!looping());
 drag(lane,20,10);check('reverse selection',Math.abs(range()[0]-10)<.01&&Math.abs(range()[1]-20)<.01);
 lane.setPointerCapture=capture;check('cache retained',document.getElementById('player').dataset.cache==='ready');
 seek.value=12;seek.dispatchEvent(new Event('input'));seek.dispatchEvent(new Event('change'));document.querySelector('#player-sources button').click();check('original clears delta',deltaButton.disabled&&deltaButton.getAttribute('aria-pressed')==='false');return JSON.stringify(results);
 })()""",interaction_result)

def interaction_result(raw):
 result=json.loads(raw) if raw else []
 (root/'interactions.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
 if not result or not all(r['ok'] for r in result):finish(3,{'interactions':result});return
 page.runJavaScript("JSON.stringify((()=>{const r=document.getElementById('player-loop-lane').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})())",native_drag)

def native_drag(raw):
 r=json.loads(raw);target=w.view.focusProxy()
 a=QPoint(round(r['x']+r['w']*.7),round(r['y']+r['h']/2));b=QPoint(round(r['x']+r['w']*.9),a.y())
 QTest.mouseMove(target,a);QTest.mousePress(target,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,a)
 QTest.mouseMove(target,b,100);QTest.mouseRelease(target,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,b)
 QTimer.singleShot(400,lambda:page.runJavaScript("JSON.stringify({left:parseFloat(document.getElementById('player-loop-region').style.left),width:parseFloat(document.getElementById('player-loop-region').style.width),active:document.getElementById('draft-loop').getAttribute('aria-pressed'),errors:probeErrors})",native_result))

def native_result(raw):
 r=json.loads(raw);(root/'native_pointer.json').write_text(json.dumps(r,indent=2),encoding='utf-8')
 ok=abs(r['left']-70)<1 and abs(r['width']-20)<1 and r['active']=='true' and not r['errors']
 w.resize(int(os.environ.get('SB_PROBE_WIDTH','1100'))+1,1050);w.view.update()
 QTimer.singleShot(500,lambda:finish(0 if ok else 4,r))

def finish(code,p):
 timer.stop();result={'exit':code,'state':p,'calls':calls,'observed':observed}
 (root/'ui_stream.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 w.grab().save(str(root/'player.png'));w.setMinimumWidth(600);w.resize(700,1050);QTimer.singleShot(1500,lambda:(w.grab().save(str(root/'player_narrow.png')),app.exit(code)));print(json.dumps({'exit':code,'state':p,'calls':calls},ensure_ascii=False),flush=True)
timer=QTimer();timer.timeout.connect(lambda:page.runJavaScript("document.getElementById('draft-play') && JSON.stringify({ready:document.getElementById('player').dataset.cache==='ready',max:document.getElementById('draft-seek').max,position:document.getElementById('draft-seek').value,play:document.getElementById('draft-play').dataset.playing,time:document.getElementById('draft-time').textContent,status:document.getElementById('draft-status').textContent,cache:document.getElementById('player').dataset.cache,guitar:document.getElementById('knob-guitar').getAttribute('aria-valuenow'),guidance:document.getElementById('knob-guidance').getAttribute('aria-valuenow'),mode:document.getElementById('player-mode').textContent,loop:document.getElementById('draft-loop').getAttribute('aria-pressed'),previewTab:!!document.getElementById('tab-preview'),title:!!document.getElementById('draft-title'),errors:probeErrors})",check));timer.start(200)
sys.exit(app.exec())
