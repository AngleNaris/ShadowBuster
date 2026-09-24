"""Read-only capture of the developer WebEngine page and native frame geometry."""
import ctypes, json, urllib.request, websocket, base64
from ctypes import wintypes
from pathlib import Path

root=Path('experiments/results/resize_live')
root.mkdir(exist_ok=True)
pages=json.load(urllib.request.urlopen('http://127.0.0.1:9226/json/list'))
ws=websocket.create_connection(pages[0]['webSocketDebuggerUrl'],suppress_origin=True,timeout=10)
def call(number,method,params):
    ws.send(json.dumps({'id':number,'method':method,'params':params}))
    while True:
        reply=json.loads(ws.recv())
        if reply.get('id')==number:return reply
expression="""JSON.stringify({size:[innerWidth,innerHeight],dpr:devicePixelRatio,scroll:[scrollX,scrollY],report:!document.getElementById('pane-report').hidden,active:document.activeElement.id,rects:Object.fromEntries(['tab-report','player','content'].map(id=>{const e=document.getElementById(id),r=e.getBoundingClientRect();return [id,{...r.toJSON(),hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.id}]}))})"""
data=call(1,'Runtime.evaluate',{'expression':expression,'returnByValue':True})
page=json.loads(data['result']['result']['value'])
shot=call(2,'Page.captureScreenshot',{'format':'png'})
(root/'page.png').write_bytes(base64.b64decode(shot['result']['data']))
user=ctypes.windll.user32
user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
user.GetClientRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
user.ClientToScreen.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
windows=[]
@ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
def visit(hwnd,_):
    title=ctypes.create_unicode_buffer(512);user.GetWindowTextW(hwnd,title,512)
    if title.value.startswith('ShadowBuster —'):
        rect=wintypes.RECT();client=wintypes.RECT();origin=wintypes.POINT()
        user.GetWindowRect(hwnd,ctypes.byref(rect));user.GetClientRect(hwnd,ctypes.byref(client));user.ClientToScreen(hwnd,ctypes.byref(origin))
        windows.append({'handle':hwnd,'frame':[rect.left,rect.top,rect.right,rect.bottom],'client':[client.right,client.bottom],'origin':[origin.x,origin.y]})
    return True
user.EnumWindows(visit,0)
result={'page':page,'windows':windows}
(root/'state.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result));ws.close()
