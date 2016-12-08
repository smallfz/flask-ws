#-*- coding: utf-8 -*-

import json
import hashlib
import base64
import traceback

from threading import Thread, Event
from Queue import Queue, Empty
from defs import *


try:
    import tornado
    from tornado.iostream import StreamClosedError
except ImportError:
    tornado = None
    StreamClosedError = None

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
        if f:
            resp = ['HTTP/1.1 403 handshake fail', '', '']
            f.write('\r\n'.join(resp))
            f.close()
        return self

    def fail(self):
        f = self.request.connection.detach()
        if f:
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
                    ('x-handshake-by', 'TornadoWebSocketAdapter'),]
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
        pid = os.getpid()
        tid = th.ident
        th.setName('ws-%s-handle-recv-%s' % (pid, tid));
        th_h = threading.Thread(target=self._handler, 
                                args=(handler, values))
        th_h.setDaemon(True)
        th_h.start()
        tid = th_h.ident
        th_h.setName('ws-%s-handler-%s' % (pid, tid));
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
        pid = os.getpid()
        th = threading.Thread(target=self._recv_for_server, 
                              args=(self.f, server))
        th.setDaemon(True)
        th.start()
        tid = th.ident
        th.setName('ws-%s-server-recv-%s' % (pid, tid))
        # self.threads.append(th)
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
            curr_th = threading.current_thread()
            while self.threads:
                th = self.threads.pop()
                if not th:
                    continue
                if th == curr_th:
                    continue
                if th.is_alive():
                    pass
                th.join()
        except IndexError:
            pass
        logging.info('TornadoWebSocketAdapter._abort(): done cleanning.')
