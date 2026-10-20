import unittest,sys,traceback,importlib,glob,os
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0]=[ROOT+'/backend',ROOT+'/backend/tests']
only=sys.argv[1:]
from dewako.persistence.db import Database
mods=sorted(os.path.basename(f)[:-3] for f in glob.glob(ROOT+'/backend/tests/test_*.py'))
tot_ok=tot_bad=0
for mod in mods:
  if only and not any(o in mod for o in only): continue
  m=importlib.import_module(mod);ok=bad=skipped=0
  for n in sorted(dir(m)):
    if n.startswith('test_') and callable(getattr(m,n)):
      try: getattr(m,n)();ok+=1;Database.close_all_idle()
      except unittest.SkipTest: skipped+=1;print('SKIP',mod,n)
      except Exception: bad+=1;print('FAIL',mod,n);traceback.print_exc(limit=5)
  print(f'{mod}: {ok} passed, {bad} failed, {skipped} skipped');tot_ok+=ok;tot_bad+=bad
print(f'TOTAL {tot_ok} passed, {tot_bad} failed'); sys.exit(1 if tot_bad else 0)
