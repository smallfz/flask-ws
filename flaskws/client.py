#-*- coding: utf-8 -*-

import os
import time
import socket
import urllib, urlparse
import hashlib
import threading
import Queue
from cStringIO import StringIO
import base64
from defs import *
from protocol import parse_frame, make_frame
from utils import r_select


class _Client(object):

    def __init__(self, url, proxies=None):
        self.url = url
        self.uri = urlparse.urlparse(url)
        self.scheme = self.uri.scheme.lower()
        self.proxies = proxies
        self.is_ssl = self.scheme == 'wss'
        self.sock = None
        self.f = None
        self.evt_abort = threading.Event()
        self.q_in = Queue.Queue()
        self.key = ''
        self.status = None
        self.headers = Headers()

    def _parse_response(self, resp_header_raw):
        lines = resp_header_raw.split(crlf)
        h = lines[0].split(' ', 2)
        status = (int(h[1]), h[2])
        rows = [line.split(':')[:2] for line in lines if ':' in line]
        headers = Headers()
        for _k, _v in rows:
            k = _k.strip().lower()
            v = _v.strip()
            if k in headers:
                v0 = headers[k]
                if isinstance(v0, list):
                    v0.append(v)
                else:
                    headers[k] = [v0, v]
            else:
                headers[k] = v
        return status, headers

    def _check_handshake(self, resp_headers):
        connection = resp_headers.get('connection', None)
        if 'upgrade' not in resp_headers.get_lower_list('connection'):
            return False
        if 'websocket' not in resp_headers.get_lower_list('upgrade'):
            return False
        if 'sec-websocket-accept' not in resp_headers:
            return False
        key_hash = '%s%s' % (self.key, ws_uid)
        key_hash = base64.b64encode(hashlib.sha1(key_hash).digest())
        _accept = resp_headers['sec-websocket-accept']
        if key_hash != _accept:
            return False
        return True

    def handshake(self, timeout=20.0):
        t0 = time.time()
        uri = self.uri
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((uri.hostname, (uri.port or 80)))
        except socket.error:
            raise WsError(u'failed to connect to host.')
        self.sock = sock
        origin = '%s://%s' % (uri.scheme, uri.netloc)
        key = base64.b64encode(os.urandom(8))
        self.key = key
        headers = (('host', uri.netloc),
                   ('user-agent', 'hd-cluster/libs/ws.client'),
                   ('connection', 'upgrade'),
                   ('upgrade', 'websocket'),
                   ('origin', origin),
                   ('sec-websocket-version', 13),
                   ('sec-websocket-key', key),)
        sock.send('GET %s HTTP/1.1%s' % (uri.path, crlf))
        headers_str = crlf.join(['%s: %s' % (k,v) for k,v in headers])
        sock.send(headers_str)
        sock.send(crlf + crlf)
        buff = StringIO()
        while not self.evt_abort.is_set():
            r = r_select([sock], timeout=0.5)
            if not r:
                if t0 + timeout < time.time():
                    return False
                continue
            data = r[0].recv(1024)
            if not data:
                continue
            buff.write(data)
            if crlf + crlf not in buff.getvalue():
                continue
            resp_raw = buff.getvalue()
            resp_header_raw = resp_raw.split(crlf+crlf)[0]
            status, resp_headers = self._parse_response(resp_header_raw)
            self.status = status
            self.resp_headers = resp_headers
            if self.status[0] != 101:
                raise HTTPError(*self.status)
            handshake_ok = self._check_handshake(resp_headers)
            if not handshake_ok:
                return False
            data = resp_raw[len(resp_header_raw + crlf + crlf):]
            if data:
                try:
                    parse_frame(data)
                except EOF:
                    pass
            self.f = sock.makefile()
            return True

    def recv(self, timeout=5.0, allow_fragments=True):
        _op, _buff = None, None
        while not self.evt_abort.is_set():
            frame = self._recv_next(timeout=timeout)
            if frame:
                fin, op, payload = frame
                if not allow_fragments:
                    if fin and not _buff:
                        return frame
                    if not fin:
                        if not _buff:
                            _op = op
                            _buff = StringIO()
                    _buff.write(payload)
                    if fin:
                        return fin, _op, _buff.getvalue()
                    else:
                        continue
            return frame

    def _recv_next(self, timeout=5.0):
        _op, _buff = None, None
        t0 = time.time()
        while t0 + timeout >= time.time() and not self.evt_abort.is_set():
            if not self.f:
                time.sleep(0.1)
                continue
            r = r_select([self.f], timeout=0.1)
            if not r:
                continue
            f = r[0]
            try:
                frame = parse_frame(f)
                return frame
            except (IOError, AttributeError, socket.error, WsIOError):
                self.close()
                # raise WsCommunicationError()

    def __iter__(self):
        if not self.f:
            return
        while not self.evt_abort.is_set():
            item = self._recv_next()
            if item:
                yield item

    def send(self, data, fin=True, op=OP_TEXT, mask=True):
        if self.evt_abort.is_set() or not self.f:
            raise WsError('websocket was closed.')
        sub_f_size = MAX_FRAME_SIZE
        size = len(data)
        if fin and (size > sub_f_size):
            cur = 0
            while True:
                part = data[cur: cur + sub_f_size]
                if not part:
                    break
                _fin = 0
                if cur + len(part) >= size:
                    _fin = 1
                _op = op
                if cur > 0:
                    _op = 0
                frame = make_frame(_fin, _op, part, mask=mask)
                self.f.write(frame)
                cur += len(part)
            self.f.flush()
        else:
            frame = make_frame(fin, op, data, mask=mask)
            self.f.write(frame)
            self.f.flush()

    def close(self):
        if not self.evt_abort.is_set():
            self.evt_abort.set()
        if self.f:
            self.f.close()
            self.f = None
        if self.sock:
            self.sock.close()
            self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *args, **kargs):
        self.close()


def ws_connect(*args, **kargs):
    return _Client(*args, **kargs)


if __name__ == '__main__':
    with ws_connect('ws://50.gz2.yj.hp:8082/ws') as c:
        if c.handshake():
            print 'handshake: ok'
            c.send('{"op":"LIST_NOTES","principal":"anonymous","ticket":"anonymous"}')
            for msg in c:
                print msg
                break
    # ws_connect('ws://localhost:10080/hub/notebooks/notebook/2BA4MWGBT/_.ws')
