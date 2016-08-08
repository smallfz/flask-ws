#-*- coding: utf-8 -*-

import re
import hashlib
import base64
import functools
import struct
import time
import json
import socket
import logging
import traceback
from cStringIO import StringIO
from werkzeug.routing import Map, Rule, NotFound
from werkzeug.wrappers import Response as BaseResponse
from werkzeug.datastructures import Headers
from flask import request
from threading import Thread, Event
from Queue import Queue, Empty
from defs import *
from protocol import parse_frame, make_frame
from utils import r_select

try:
    import uwsgi
except ImportError:
    uwsgi = None

try:
    import tornado
    from tornado.iostream import StreamClosedError
except ImportError:
    tornado = None
    StreamClosedError = None

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
                time.sleep(0.05)
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
        
    def _recv_to_q(self, timeout=0.5):
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


class UwsgiWsSock(_BaseWsSock):

    def __init__(self, environ, *args):
        self.environ = environ
        self.evt_open = Event()
        self.evt_close = Event()
        self.q = Queue()
        self.q_recv = Queue()

    def handshake(self, environ, start_response):
        # key = environ.get('HTTP_SEC_WEBSOCKET_KEY', '')
        # origin = environ.get('HTTP_ORIGIN', '')
        # uwsgi.websocket_handshake(key, origin)
        # return True
        return super(UwsgiWsSock, self)._handshake(environ, start_response)

    def close(self):
        if not self.evt_close.is_set():
            self.evt_close.set()

    def send_json(self, v, fin=True, op=OP_TEXT, mask=False):
        if isinstance(v, unicode) or isinstance(v, str):
            return self.send(v)
        else:
            return self.send(json.dumps(v))

    def send(self, data, fin=True, op=OP_TEXT, mask=False):
        '''public send(data)'''
        if not self.evt_close.is_set():
            self.q.put(data)
        else:
            raise WsError(u'websocket closed.')

    def recv(self, timeout=5.0, **kargs):
        t0 = time.time()
        while not self.evt_close.is_set():
            try:
                msg = self.q_recv.get(True, 0.1)
                if msg:
                    return (1, OP_TEXT, msg)
                if to + timeout > time.time():
                    return
            except Empty:
                pass

    def serve_handler(self, handler, values):
        def invoke_handler(handler, sock):
            try:
                handler(sock, **values)
            finally:
                sock.close()
        th = Thread(target=invoke_handler, args=(handler, self,))
        th.setDaemon(True)
        th.start()
        try:
            fd = uwsgi.connection_fd()
            while not self.evt_close.is_set():
                uwsgi.wait_fd_read(fd, 0.3)
                uwsgi.suspend()
                _fd = uwsgi.ready_fd()
                msg = uwsgi.websocket.recv_nb()
                if msg:
                    self.q_recv.put(msg)
                try:
                    msg = self.q.get(True, 0.1)
                    if msg:
                        uwsgi.websocket_send(msg)
                except Empty:
                    pass
        finally:
            self.close()
            th.join()
        return []

    def server(self, server):
        if not server:
            raise ValueError('server instance required.')
        def recv(server, sock):
            while not sock.evt_open.is_set():
                time.sleep(0.05)
            if hasattr(server, 'on_open'):
                server.on_open(self)
            try:
                fd = uwsgi.connection_fd()
                while not sock.evt_close.is_set():
                    uwsgi.wait_fd_read(fd, 1.0)
                    uwsgi.suspend()
                    _fd = uwsgi.ready_fd()
                    msg = uwsgi.websocket_recv_nb()
                    if msg:
                        frame = (1, OP_TEXT, msg)
                        server.on_message(sock, frame)
            finally:
                sock.evt_close.set()
        th = None
        if hasattr(server, 'on_message'):
            th = Thread(target=recv, args=(server, self,))
            th.setDaemon(True)
            th.start()
        #yield self._frame(True, OP_PING, '')
        # uwsgi.websocket_send('')
        self.evt_open.set()
        try:
            while not self.evt_close.is_set():
                try:
                    msg = self.q.get(True, 0.1)
                    if msg:
                        uwsgi.websocket_send(msg)
                except Empty:
                    pass
        finally:
            sock.evt_close.set()
            if hasattr(server, 'on_close'):
                server.on_close(self)
            if th:
                th.join()
        return []

    
class WsDeliver(Exception):

    def __init__(self, response):
        self.response = response


class TornadoWebSocketAdapter(object):

    def __init__(self, environ, app, request):
        self.environ = environ
        self.app = app
        self.request = request
        self.evt_close = threading.Event()
        self.f = None
        self.q_recv = None
        self.threads = []

    def html(self):
        f = self.request.connection.detach()
        resp = ['HTTP/1.1 403 handshake fail', '', '']
        f.write('\r\n'.join(resp))
        f.close()
        return self

    def fail(self):
        f = self.request.connection.detach()
        resp = ['HTTP/1.1 400 handshake fail', '', '']
        f.write('\r\n'.join(resp))
        f.close()
        return self

    def handshake(self):
        request = self.request
        headers = request.headers
        h_upgrade = headers.get('upgrade', '').lower()
        h_connection = headers.get('connection', '').lower()
        h_connection = h_connection.split(',')
        h_connection = [c.strip() for c in h_connection if c.strip()]
        h_key = headers.get('sec-websocket-key', '')
        if h_upgrade != 'websocket':
            self.fail()
            return
        elif 'upgrade' not in h_connection:
            self.fail()
            return
        elif not h_key:
            self.fail()
            return
        # -- handshake
        protocol = headers.get('sec-websocket-protocol', '')
        version = headers.get('sec-websocket-version', '')
        key_hash = '%s%s' % (h_key, ws_uid)
        key_hash = base64.b64encode(hashlib.sha1(key_hash).digest())
        _headers = [('upgrade', 'websocket'),
                    ('connection', 'upgrade'),
                    ('sec-websocket-accept', key_hash),
                    ('x-handshake-by', '_BaseWsSock'),]
        f = request.connection.detach()
        self.f = f
        f.set_close_callback(self.on_connection_close)
        resp = ['HTTP/1.1 101 Switching protocols']
        resp.extend([': '.join(h) for h in _headers])
        resp.extend(['', ''])
        f.write('\r\n'.join(resp))
        # f.write(make_frame(1, OP_PING, ''))
        # f.write(make_frame(1, OP_TEXT, 'hello from tornado!'))        
        return True

    # --------------------
    # for flask view
    # --------------------

    def handle(self, handler, values):
        self.q_recv = Queue()
        f = self.f
        th = threading.Thread(target=self._recv, 
                              args=(f,))
        th.setDaemon(True)
        th.start()
        th_h = threading.Thread(target=self._handler, 
                                args=(handler, values))
        th_h.setDaemon(True)
        th_h.start()
        self.threads.append(th)
        self.threads.append(th_h)
        return self

    def _handler(self, handler, values):
        try:
            handler(self, values)
        except Exception, e:
            logging.error('_handler -> handler()')
            logging.error(traceback.format_exc(e))
        self._abort()

    def _recv(self, f):
        while not self.evt_close.is_set():
            frame = None
            try:
                frame = parse_frame(f)
            except (StreamClosedError, WsIOError):
                break
            except WsClosedByRemote:
                break
            except Exception, e:
                logging.error('_recv -> parse_frame(f)')
                logging.error(traceback.format_exc(e))
                break
            if not frame:
                continue
            self.q_recv.put(frame)
        self._abort()

    def recv(self, timeout=5.0):
        if self.evt_close.is_set():
            return
        t0 = time.time()
        while not self.evt_close.is_set():
            try:
                frame = self.q_recv.get(True, 0.1)
                if frame:
                    return frame
                if t0 + timeout > time.time():
                    return
            except Empty:
                pass

    # --------------------
    # for server class
    # --------------------

    def server(self, server):
        th = threading.Thread(target=self._recv_for_server, 
                              args=(self.f, server))
        th.setDaemon(True)
        th.start()
        self.threads.append(th)
        return self

    def _recv_for_server(self, f, server):
        self._safe_invoke(server, 'on_open', self)
        while not self.evt_close.is_set():
            frame = None
            try:
                frame = parse_frame(f)
            except (StreamClosedError, WsIOError):
                break
            except WsClosedByRemote:
                break
            except Exception, e:
                logging.error('_recv_for_server -> parse_frame')
                logging.error(traceback.format_exc(e))
                break
            if not frame:
                continue
            self._safe_invoke(server, 'on_message', self, frame)
        self._safe_invoke(server, 'on_close', self)
        self._abort()

    # --------------------

    def _safe_invoke(self, server, method, *args, **kargs):
        if not server or not method:
            return
        if not hasattr(server, method):
            return
        try:
            getattr(server, method)(*args, **kargs)
        except:
            logging.error('_safe_invoke: %s()' % method)
            logging.error(traceback.format_exc(e))

    # --------------------

    def send_json(self, v, fin=True, op=OP_TEXT):
        if isinstance(v, (unicode, str)):
            return self.send(v)
        else:
            return self.send(json.dumps(v))

    def send(self, msg, fin=True, op=OP_TEXT):
        if self.evt_close.is_set():
            return
        frame = make_frame(fin, op, msg or '')
        try:
            if self.f and not self.f.closed():
                self.f.write(frame)
        except (StreamClosedError, WsIOError):
            self._abort()
        except Exception, e:
            logging.error('send -> self.f.write(frame)')
            logging.error(traceback.format_exc(e))
            self._abort()

    def close(self):
        logging.info('TornadoWebSocketAdapter.close()')
        self.send('', op=OP_CLOSE)
        self._abort()

    def on_connection_close(self):
        logging.info('TornadoWebSocketAdapter.on_connection_close()')
        self._abort()

    def _abort(self):
        logging.info('TornadoWebSocketAdapter._abort(): start to clean up')
        if not self.evt_close.is_set():
            self.evt_close.set()
        if self.f:
            try:
                if not self.f.closed():
                    self.f.close()
            except Exception, e:
                logging.warn('_abort -> self.f.close()')
                logging.warn(traceback.format_exc(e))
            self.f = None
        try:
            while True:
                th = self.threads.pop()
                if not th:
                    continue
                th.join()
        except IndexError:
            pass
        logging.info('TornadoWebSocketAdapter._abort(): done cleanning.')

    
class WsMiddleware(object):

    def __init__(self, wsgi_app, *args, **kargs):
        self.wsgi_app = wsgi_app
        self.app = args[0] if args else None
        self.wss = args[1] if len(args) > 1 else None
        self.use_tornado = kargs.get('use_tornado', False)

    def __call__(self, environ, start_response):
        # if self.wss and self.app:
        #     adapter = self.wss.map.bind_to_environ(environ)
        #     try:
        #         handler, values = adapter.match()
        #         sock = WsSocket(environ, handler, values)
        #         if sock.handshake(environ, start_response):
        #             return sock()
        #         else:
        #             return sock.html(environ, start_response)
        #     except NotFound:
        #         pass
        try:
            return self.wsgi_app(environ, start_response)
        except WsDeliver, e:
            if uwsgi:
                key = environ.get('HTTP_SEC_WEBSOCKET_KEY', '')
                origin = environ.get('HTTP_ORIGIN', '')
                origin = ''
                # print key
                # print origin
                uwsgi.websocket_handshake(key, origin)
            resp = e.response
            return resp.run(environ, start_response, self.use_tornado)


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

    def run(self, environ, start_response, use_tornado=False):
        if use_tornado:
            _t_app = environ['x.tornado.app']
            _t_request = environ['x.tornado.request']
            sock = TornadoWebSocketAdapter(environ, _t_app, _t_request)
            if sock.handshake():
                return sock.handle(self.handler, self.values)
            else:
                return sock.html()
        if uwsgi:
            sock = UwsgiWsSock(environ)
            return sock.serve_handler(self.handler, self.values)
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

    def run(self, environ, start_response, use_tornado):
        if use_tornado:
            _t_app = environ['x.tornado.app']
            _t_request = environ['x.tornado.request']
            sock = TornadoWebSocketAdapter(environ, _t_app, _t_request)
            if sock.handshake():
                return sock.server(self.server_cls(sock, **self.values))
            else:
                return sock.html()
        if uwsgi:
            # key = environ.get('HTTP_SEC_WEBSOCKET_KEY', '')
            # origin = environ.get('HTTP_ORIGIN', '')
            # uwsgi.websocket_handshake(key, origin)
            sock = UwsgiWsSock(environ)
            return sock.server(self.server_cls(sock, **self.values))
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
