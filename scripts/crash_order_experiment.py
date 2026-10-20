"""Run the crash/recovery modules in a chosen order, N repetitions in ONE process; per-test durations; fail loudly on hang (watchdog already in tests)."""
import sys, os, time, random, importlib, unittest, traceback, faulthandler, threading
B=os.environ.get('DEWAKO_TEST_SHIM','')  # optional dir with a pytest shim when pytest is not installed
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0]=[ROOT+'/backend',ROOT+'/backend/tests']+([B] if B else [])
from dewako.persistence.db import Database
mode, reps, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
mods=[importlib.import_module(m) for m in ("test_crash_matrix","test_crash_and_recon")]
tests=[(m.__name__,n,getattr(m,n)) for m in mods for n in sorted(dir(m)) if n.startswith("test_") and callable(getattr(m,n))]
rnd=random.Random(seed)
faulthandler.dump_traceback_later(900, exit=True)       # absolute backstop for the experiment itself
ok=bad=skip=0; slow=[]
for r in range(reps):
    order=list(tests)
    if mode=="reverse": order.reverse()
    elif mode=="shuffle": rnd.shuffle(order)
    for mod,n,fn in order:
        t0=time.monotonic()
        try: fn(); ok+=1
        except unittest.SkipTest: skip+=1
        except BaseException as e:
            bad+=1; print("FAIL",r,mod,n,type(e).__name__,str(e)[:300])
        finally: Database.close_all_idle()
        d=time.monotonic()-t0; slow.append((d,n))
    print(f"rep {r} done ok={ok} bad={bad} skip={skip} threads={threading.active_count()}", flush=True)
slow.sort(reverse=True)
print(f"MODE={mode} reps={reps} seed={seed} passed={ok} failed={bad} skipped={skip} slowest={[(round(d,1),n) for d,n in slow[:3]]}")
