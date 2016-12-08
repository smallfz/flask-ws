#-*- coding: utf-8 -*-

import json
import socket
import hashlib
import base64
import traceback

from threading import Thread, Event
from Queue import Queue, Empty
from defs import *
from protocol import parse_frame, make_frame
from utils import r_select


class _BaseWsSock(object):

    def _handshake(self, environ, start_response):
        connection = environ.get('HTTP_CONNECTION', '') or ''
        connection = connection.lower().split(',')
        connection = [c.strip() for c in connection if c.strip()]
        upgrade = environ.get('HTTP_UPGRADE', '')
        if 'upgrade' not in connection:
            return False
        elif upgrade.lower() != 'websocket':
            return False
        key = environ.get('HTTP_SEC_WEBSOCKET_KEY', '')
        if not key:
            return False
        protocol = environ.get('HTTP_SEC_WEBSOCKET_PROTOCOL', '')
        version = environ.get('HTTP_SEC_WEBSOCKET_VERSION', '')
        # ---
        key_hash = '%s%s' % (key, ws_uid)
        key_hash = base64.b64encode(hashlib.sha1(key_hash).digest())
        # ---
        headers = [('upgrade', 'websocket'),
                   ('connection', 'upgrade'),
                   ('sec-websocket-accept', key_hash),
                   ('x-handshake-by', '_BaseWsSock'),
                   #  ('sec-websocket-protocol', 'chat'),
                   ]
        start_response('101 Switching protocols', headers)
        return True

    def html(self, environ, start_response):
        start_response('400 this is a websocket server.', {})
        yield 'BAD REQUEST: this is a websocket server.'


class WsSocket(_BaseWsSock):

    def __init__(self, environ, handler, values):
        self.environ = environ
        self.handler = handler
        self.values = values
        # print '---------------------'
        # for k in self.environ:
        #     print k, type(self.environ[k])
        f = self.environ.get('wsgi.input', None)
        # print dir(f)
        # print type(f)
        # print f.readable.__doc__
        # print f.readable()
        self.f = f
        # self.evt_msg = Event()
        self.q_frame = Queue()
        self.q_recv = Queue()
        self.evt_open = Event()
        self.evt_close = Event()

    def handshake(self, environ, start_response):
        return super(WsSocket, self)._handshake(environ, start_response)

    def _frame(self, fin, op, payload, mask=False):
        return make_frame(fin, op, payload, mask=mask)

    def _nextframe(self, interval=0.50):
        while not self.evt_close.is_set():
            try:
                frame = self.q_frame.get(True, interval)
                if frame:
                    yield frame
            except Empty:
                yield None

    # def _sending_iter(self):
    #     for frame in self._nextframe():
    #         if frame:
    #             yield frame

    def _recv(self, timeout=5.0):
        if self.evt_close.is_set() or not self.f:
            raise WsError(u'websocket closed.')
        # print '----------- _recv ------------'
        # print self.f
        # print type(self.f)
        # print dir(self.f)
        t0, f = time.time(), None
        while not self.evt_close.is_set():
            if hasattr(self.f, 'readable'):
                # r = [self.f] if self.f.readable() else []
                # if not r:
                #     time.sleep(timeout)
                r = [self.f]
            else:
                r = r_select([self.f], timeout=timeout)
            if not r:
                time.sleep(0.02)
                if time.time() - timeout > t0:
                    raise WsTimeout()
            else:
                f = r[0]
                break
        try:
            fin, op, payload = parse_frame(f)
            if op == OP_CLOSE:
                self.close()
            elif op == OP_PING:
                pong = self._frame(True, OP_PONG, '')
                self.q_frame.put(pong)
            return fin, op, payload
        except (IOError, AttributeError, socket.error):
            raise
        except WsClosedByRemote:
            raise
        
    def _recv_to_q(self, timeout=0.02):
        try:
            fin, op, data = self._recv(timeout=timeout)
            if data:
                self.q_recv.put((fin, op, data))
        except WsTimeout:
            pass
        except (WsIOError, WsClosedByRemote):
            self.close()

    def recv(self, timeout=5.0, allow_fragments=True):
        '''public recv(timeout=5.0)'''
        if self.evt_close.is_set():
            raise WsError(u'websocket closed.')
        t0 = time.time()
        _op, _buff = None, None
        while t0 + timeout >= time.time():
            try:
                frame = self.q_recv.get(True, 0.05)
                if frame:                    
                    if allow_fragments:
                        return frame
                    else:
                        fin, op, msg = frame
                        if fin and not _buff:
                            return frame
                        elif not _buff:
                            _op = op
                            _buff = StringIO()
                            _buff.write(msg)
                        if fin:
                            _buff.write(msg)
                            return fin, _op, _buff.getvalue()
            except Empty:
                pass

    def send_json(self, v, fin=True, op=OP_TEXT, mask=False):
        if isinstance(v, unicode) or isinstance(v, str):
            return self.send(v)
        else:
            return self.send(json.dumps(v))

    def send(self, data, fin=True, op=OP_TEXT, mask=False):
        '''public send(data)'''
        if not self.evt_close.is_set():
            size = len(data)
            sub_f_size = MAX_FRAME_SIZE
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
                    frame = self._frame(_fin, _op, part, mask=mask)
                    self.q_frame.put(frame)
                    cur += len(part)
            else:
                frame = self._frame(1 if fin else 0, op, data, mask=mask)
                self.q_frame.put(frame)
        else:
            raise WsError(u'websocket closed.')

    def ping(self):
        if not self.evt_close.is_set():
            frame = self._frame(1, OP_PING, '')
            self.q_frame.put(frame)
    
    def close(self):
        '''public close()'''
        if not self.evt_close.is_set():
            frame = self._frame(1, OP_CLOSE, '')
            self.q_frame.put(frame)
            time.sleep(0.05)
            self.evt_close.set()

    def _loop(self, only_downstream=False):
        for frame in self._nextframe():
            if frame:
                yield frame
            elif not only_downstream:
                self._recv_to_q()

    def __call__(self):
        def invoke_handler(handler, sock):
            try:
                handler(sock, **sock.values)
            finally:
                sock.close()
        th = Thread(target=invoke_handler, args=(self.handler, self,))
        th.setDaemon(True)
        th.start()
        try:
            yield self._frame(True, OP_PING, '')
            for item in self._loop():
                yield item
            # for frame in self._nextframe():
            #     yield frame
            # print 'sending channel closed.'
        finally:
            self.close()
            th.join()
            # print 'session ended.'

    def server(self, server):
        if not server:
            raise ValueError('server instance required.')
        def recv(server, sock):
            while not sock.evt_open.is_set():
                time.sleep(0.05)
            if hasattr(server, 'on_open'):
                server.on_open(self)
            while not sock.evt_close.is_set():
                frame = sock.recv(timeout=1.0)
                if frame:
                    server.on_message(sock, frame)
        def recv_to_q(sock):
            while not sock.evt_open.is_set():
                time.sleep(0.05)
            while not sock.evt_close.is_set():
                sock._recv_to_q()
        th_list = []
        if hasattr(server, 'on_message'):
            th = Thread(target=recv, args=(server, self,))
            th.setDaemon(True)
            th.start()
            th_list.append(th)
            th = Thread(target=recv_to_q, args=(self,))
            th.setDaemon(True)
            th.start()
            th_list.append(th)
        yield self._frame(True, OP_PING, '')
        self.evt_open.set()
        try:
            for item in self._loop(only_downstream=True):
                yield item
        finally:
            self.close()
            if hasattr(server, 'on_close'):
                server.on_close(self)
            if th_list:
                for th in th_list:
                    th.join()
