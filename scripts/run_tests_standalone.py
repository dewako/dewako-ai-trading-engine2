import unittest,sys,traceback,importlib,glob,os
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0]=[ROOT+'/backend',ROOT+'/backend/tests']
only=sys.argv[1:]
import inspect, itertools
class MonkeyPatch:
  def __init__(self): self._undo=[]
  def setattr(self,obj,name,value=None,raising=True):
    if isinstance(obj,str): mod,_,attr=obj.rpartition('.'); import importlib as il; o=il.import_module(mod); name,value=attr,name; obj=o
    self._undo.append(lambda o=obj,n=name,old=getattr(obj,name): setattr(o,n,old)); setattr(obj,name,value)
  def setenv(self,k,v):
    old=os.environ.get(k); self._undo.append(lambda: os.environ.pop(k,None) if old is None else os.environ.__setitem__(k,old)); os.environ[k]=str(v)
  def delenv(self,k,raising=True):
    old=os.environ.get(k); self._undo.append(lambda: None if old is None else os.environ.__setitem__(k,old)); os.environ.pop(k,None)
  def undo(self):
    while self._undo: self._undo.pop()()
def expand(fn):
  """Yield (label, callable) for each parameter combination; injects `monkeypatch` when requested."""
  groups=getattr(fn,'_params',[]) or [([],[()])]
  names_sets=[g[0] for g in groups]
  for combo in itertools.product(*[g[1] for g in groups]):
    kw={}
    for (names,_),vals in zip(groups,combo):
      vals=vals if isinstance(vals,(tuple,list)) and len(names)>1 else (vals,)
      kw.update(dict(zip(names,vals)))
    def call(fn=fn,kw=kw):
      mp=MonkeyPatch()
      args=dict(kw)
      if 'monkeypatch' in inspect.signature(fn).parameters: args['monkeypatch']=mp
      try: return fn(**args)
      finally: mp.undo()
    yield (repr(kw)[:60] if kw else ''), call
import signal
class TestTimeout(BaseException):          # BaseException: library code with `except Exception` must not swallow the timeout
  pass
def _alarm(*a): raise TestTimeout()
LIMIT=int(os.environ.get('DEWAKO_TEST_TIMEOUT','180'))
if hasattr(signal,'SIGALRM'): signal.signal(signal.SIGALRM,_alarm)
from dewako.persistence.db import Database
mods=sorted(os.path.basename(f)[:-3] for f in glob.glob(ROOT+'/backend/tests/test_*.py'))
tot_ok=tot_bad=tot_skip=0
for mod in mods:
  if only and not any(o in mod for o in only): continue
  m=importlib.import_module(mod);ok=bad=skipped=0
  for n in sorted(dir(m)):
    if n.startswith('test_') and callable(getattr(m,n)):
      for label,call in expand(getattr(m,n)):
        tn=n+('['+label+']' if label else '')
        if hasattr(signal,'SIGALRM'): signal.alarm(LIMIT)
        try: call();ok+=1;Database.close_all_idle()
        except TestTimeout: bad+=1;print('FAIL (TIMEOUT after %ss)'%LIMIT,mod,tn)
        except unittest.SkipTest: skipped+=1;print('SKIP',mod,tn)
        except Exception: bad+=1;print('FAIL',mod,tn);traceback.print_exc(limit=5)
        finally:
          if hasattr(signal,'SIGALRM'): signal.alarm(0)
  print(f'{mod}: {ok} passed, {bad} failed, {skipped} skipped');tot_ok+=ok;tot_bad+=bad;tot_skip+=skipped
print(f'TOTAL {tot_ok} passed, {tot_bad} failed, {tot_skip} skipped'); sys.exit(1 if tot_bad else 0)
