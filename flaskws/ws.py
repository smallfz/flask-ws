#-*- coding: utf-8 -*-

import re
import hashlib
import base64
import functools
import struct
import select
import time
import json
import socket
from cStringIO import StringIO
from werkzeug.routing import Map, Rule, NotFound
from werkzeug.wrappers import Response as BaseResponse
from werkzeug.datastructures import Headers
from flask import request
from threading import Thread, Event
from Queue import Queue, Empty
from defs import *
from protocol import parse_frame, make_frame


class WsSocket(object):

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

    def html(self, environ, start_response):
        start_response('400 this is a websocket server.', {})
        yield 'BAD REQUEST: this is a websocket server.'

    def handshake(self, environ, start_response):
        connection = environ.get('HTTP_CONNECTION', '')
        upgrade = environ.get('HTTP_UPGRADE', '')
        if connection.lower() != 'upgrade':
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
                   #  ('sec-websocket-protocol', 'chat'),
                   ]
        start_response('101 Switching protocols', headers)
        return True

    def _frame(self, fin, op, payload, mask=False):
        return make_frame(fin, op, payload, mask=mask)

    def _nextframe(self, interval=0.50):
        while not self.evt_close.is_set():
            try:
                frame = self.q_frame.get(False, interval)
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
        t0, f = time.time(), None
        while not self.evt_close.is_set():
            if hasattr(self.f, 'readable'):
                # r = [self.f] if self.f.readable() else []
                # if not r:
                #     time.sleep(timeout)
                r = [self.f]
            else:
                r, _, _ = select.select([self.f], [], [], timeout)
            if not r:
                time.sleep(0.05)
                if time.time() - timeout > t0:
                    raise WsTimeout()
            else:
                f = r[0]
                break
        try:
            fin, op, payload = parse_frame(f)
        except (IOError, AttributeError, socket.error):
            raise WsCommunicationError()
        if op == OP_CLOSE:
            self.close()
        elif op == OP_PING:
            pong = self._frame(True, OP_PONG, '')
            self.q_frame.put(pong)
        return fin, op, payload

    def _recv_to_q(self, timeout=0.5):
        try:
            fin, op, data = self._recv(timeout=timeout)
            if data:
                self.q_recv.put((fin, op, data))
        except WsTimeout:
            pass
        except WsCommunicationError:
            self.close()

    def recv(self, timeout=5.0, allow_fragments=True):
        '''public recv(timeout=5.0)'''
        if self.evt_close.is_set():
            raise WsError(u'websocket closed.')
        t0 = time.time()
        _op, _buff = None, None
        while t0 + timeout >= time.time():
            try:
                frame = self.q_recv.get(False, 0.05)
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

    def _loop(self):
        for frame in self._nextframe():
            if frame:
                yield frame
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
        th = None
        if hasattr(server, 'on_message'):
            th = Thread(target=recv, args=(server, self,))
            th.setDaemon(True)
            th.start()
        yield self._frame(True, OP_PING, '')
        self.evt_open.set()
        try:
            for item in self._loop():
                yield item
        finally:
            self.close()
            if hasattr(server, 'on_close'):
                server.on_close(self)
            if th:
                th.join()


class WsDeliver(Exception):

    def __init__(self, response):
        self.response = response


class WsMiddleware(object):

    def __init__(self, wsgi_app, *args):
        self.wsgi_app = wsgi_app
        self.app = args[0] if args else None
        self.wss = args[1] if len(args) > 1 else None

    def __call__(self, environ, start_response):
        if self.wss and self.app:
            adapter = self.wss.map.bind_to_environ(environ)
            try:
                handler, values = adapter.match()
                sock = WsSocket(environ, handler, values)
                if sock.handshake(environ, start_response):
                    return sock()
                else:
                    return sock.html(environ, start_response)
            except NotFound:
                pass
        try:
            return self.wsgi_app(environ, start_response)
        except WsDeliver, e:
            resp = e.response
            return resp.run(environ, start_response)


class WsServer(object):

    def __init__(self, app):
        self.map = Map()
        self.app = app
        app.wsgi_app = WsMiddleware(app.wsgi_app, app, self)

    def add_rule(self, rule):
        self.map.add(rule)

    def route(self, pattern):
        def wrapper_maker(view):
            self.add_rule(Rule(pattern, endpoint=view))
            @functools.wraps(view)
            def wrapper(*args, **kargs):
                return view(*args, **kargs)
            return wrapper
        return wrapper_maker


class WsResponse(object):

    def __init__(self, handler, **kargs):
        self.handler = handler
        self.values = kargs

    def run(self, environ, start_response):
        sock = WsSocket(environ, self.handler, self.values)
        if sock.handshake(environ, start_response):
            return sock()
        else:
            return sock.html(environ, start_response)

    def __call__(self, environ, start_response):
        raise WsDeliver(self)


class WsResponseForServer(object):

    def __init__(self, server_cls, **kargs):
        self.server_cls = server_cls
        self.values = kargs

    def run(self, environ, start_response):
        sock = WsSocket(environ, None, None)
        if sock.handshake(environ, start_response):
            return sock.server(self.server_cls(sock, **self.values))
        else:
            return sock.html(environ, start_response)

    def __call__(self, environ, start_response):
        raise WsDeliver(self)


def ws_server_view(handler):
    @functools.wraps(handler)
    def wrapper(**kargs):
        return WsResponse(handler, **kargs)
    return wrapper


def ws_server(server_cls):
    @functools.wraps(server_cls)
    def wrapper(**kargs):
        return WsResponseForServer(server_cls, **kargs)
    return wrapper
