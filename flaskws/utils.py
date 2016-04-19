# -*- coding: utf-8 -*- 

import os, sys
import time
import select
import threading


def r_select(rlist, timeout=5.0):
    if not rlist:
        return []
    t0 = time.time()
    has_fileno = True
    for f in rlist:
        if not hasattr(f, 'fileno'):
            has_fileno = False
    if not has_fileno:
        raise Exception(u'input file needs fileno.')
    r, _, _ = select.select(rlist, [], [], timeout)
    return r
