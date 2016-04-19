#-*- coding: utf-8 -*-

import time
import threading
from cStringIO import StringIO


crlf = '\r\n'
ws_uid = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


OP_CONTINUATION = 0
OP_TEXT = 1
OP_BINARY = 2
OP_CLOSE = 8
OP_PING = 9
OP_PONG = 0xA


class WsError(Exception):
    
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        if self.msg:
            if isinstance(self.msg, unicode):
                return self.msg.encode('utf-8')
        return self.msg or ''


class WsTimeout(WsError):
    
    def __init__(self, *args):
        self.msg = args[0] if args else None


class WsCommunicationError(Warning):

    def __init__(self, *args):
        self.msg = args[0] if args else None


class HTTPError(Exception):

    def __init__(self, code, msg):
        self.code = code
        if msg and isinstance(msg, unicode):
            msg = msg.encode('utf-8')
        self.msg = msg or ''

    def __str__(self):
        return '%d: %s' % (self.code, self.msg)


class EOF(Exception): pass


class Headers(dict):

    def __init__(self, *args, **kargs):
        super(Headers, self).__init__(*args, **kargs)

    def get_lower_list(self, key):
        if key in self:
            v = self[key]
            if isinstance(v, (list, tuple)):
                return [v1.lower() for v1 in v]
            else:
                return [v.lower()]
        return []

class StrReader(object):

    def __init__(self, d):
        self.d = d or ''
        self.cur = 0

    def read(self, size):
        cur = self.cur + size
        if cur > len(self.d):
            raise EOF()
        v = self.d[self.cur:self.cur + size]
        self.cur += size
        return v

    def __enter__(self):
        return self

    def __exit__(self, *args, **kargs):
        pass


class PromisedReader(object):

    def __init__(self, f):
        self.f = f
        self.buff = StringIO()
        self.evt_close = threading.Event()
        self.read_lock = threading.Lock()

    def read(self, size, timeout=60.0):
        if not size:
            return ''
        try:
            self.read_lock.acquire(True)
            self.buff.seek(0)
            self.buff.truncate()
            # print '---- read(%d) ----' % size
            while not self.evt_close.is_set():
                _size = size - self.buff.tell()
                # print 'raw_read(%d): ' % _size
                data = self.f.read(_size)
                # print '%d bytes.' % (len(data) if data else 0)
                if data:
                    self.buff.write(data)
                    # print 'buff.tell(): %d' % self.buff.tell()
                    if self.buff.tell() >= size:
                        return self.buff.getvalue()
        finally:
            self.read_lock.release()

    def close(self):
        self.f = None
        self.buff = None
        if self.evt_close:
            if not self.evt_close.is_set():
                self.evt_close.set()
                self.evt_close = None
            
    def __enter__(self):
        return self

    def __exit__(self, *args, **kargs):
        self.close()


class BlockedReader(object):
    '''convert a tornado stream to a blocked reader'''

    def __init__(self, f):
        self.f = f
        self.data = ''
        self.evt_ready = threading.Event()

    def _on_ready(self, data):
        if data:
            self.data += data
        self.evt_ready.set()

    def read(self, size, timeout=5.0):
        self.data = ''
        while True:
            self.evt_ready.clear()
            self.f.read_bytes(size, self._on_ready)
            self.evt_ready.wait()
            if len(self.data) >= size:
                break
        return self.data
