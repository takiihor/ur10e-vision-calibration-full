"""Preserve the SDK caller policy without changing its background threads."""
from contextlib import contextmanager
import os


@contextmanager
def preserve_caller_scheduler():
    if not all(hasattr(os,name) for name in ('sched_getscheduler','sched_getparam','sched_setscheduler')):
        yield
        return
    policy,param = os.sched_getscheduler(0),os.sched_getparam(0)
    try:
        yield
    finally:
        if (os.sched_getscheduler(0),os.sched_getparam(0).sched_priority) != (policy,param.sched_priority):
            os.sched_setscheduler(0,policy,param)
