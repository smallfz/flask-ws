#-*- coding: utf-8 -*-

import os
import struct
from cStringIO import StringIO
from defs import *

try:
    import tornado
    from tornado.iostream import IOStream
except ImportError:
    tornado = None


def _unpack(pt, data):
    size = struct.calcsize(pt)
    if (len(data) if data else 0) >= size:
        return struct.unpack(pt, data)
    raise WsIOError(u'more bytes required for unpacking.')

def _read_next_frame(f):
    h = f.read(2)
    b0, plen = _unpack('>BB', h)
    fin = (b0 >> 7) & 0b0001
    op = b0 & 0b1111
    mask = (plen >> 7) & 0b0001
    plen = plen & 0b01111111
    if plen <= 125:
        pass
    elif plen == 126:
        plen = _unpack('>H', f.read(2))[0]
    elif plen == 127:
        plen = _unpack('>Q', f.read(8))[0]
    mask_key = []
    if mask:
        mask_key = f.read(4)
        mask_key = _unpack('>BBBB', mask_key)
    block_size = 1024 * 8
    if plen <= block_size:
        payload = f.read(plen)
    else:
        _payload = StringIO()
        while True:
            read_size = plen - _payload.tell()
            read_size = block_size if read_size > block_size else read_size
            if not read_size:
                break
            block = f.read(read_size)
            if not block:
                raise WsError()
            _payload.write(block)
        payload = _payload.getvalue()
    if payload and mask and mask_key:
        _m = mask_key
        payload = [ord(c) ^ _m[i%len(_m)] for i, c in enumerate(payload)]
        payload = ''.join(map(chr, payload))
    if op == OP_CLOSE:
        raise WsClosedByRemote()
    elif op == OP_PONG:
        pass
    elif op == OP_PING:
        pass
    return (fin, op, payload)

def parse_frame(data, auto_fragment=True):
    u'''only read one frame, auto split if too large'''
    if isinstance(data, str):
        f = StrReader(data)
    elif tornado and isinstance(data, IOStream):
        f = BlockedReader(data)
    else:
        f = data # 
        # f = PromisedReader(data)
    try:
        return _read_next_frame(f)
    finally:
        # if hasattr(f, 'close'):
        #     f.close()
        pass

def make_frame(fin, op, payload, mask=False, mask_key=None):
    f = StringIO()
    b0 = (1<<7) if fin else 0
    b0 = b0 | (0b1111 & op)
    f.write(struct.pack('>B', b0))
    if isinstance(payload, unicode):
        payload = payload.encode('utf-8')
    plen = len(payload)
    mask_flag = (1<<7) if mask else 0
    if plen <= 125:
        b1 = plen | mask_flag
        f.write(struct.pack('>B', b1))
    elif plen < (1<<16):
        b1 = 126 | mask_flag
        f.write(struct.pack('>B', b1))
        f.write(struct.pack('>H', plen)) # H: unsinged short: 2 bytes
    else:
        b1 = 127 | mask_flag
        f.write(struct.pack('>B', b1))
        f.write(struct.pack('>I', plen)) # I: unsinged int: 4 bytes
    if mask:
        if not mask_key:
            mask_key = os.urandom(4)
        f.write(mask_key)
        _m = struct.unpack('>BBBB', mask_key)
        payload = [ord(c) ^ _m[i%len(_m)] for i, c in enumerate(payload)]
        payload = ''.join(map(chr, payload))
    f.write(payload)
    return f.getvalue()
