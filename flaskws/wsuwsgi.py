#-*- coding: utf-8 -*-

from threading import Thread, Event
from Queue import Queue, Empty
from defs import *
from protocol import parse_frame, make_frame
from wssock import _BaseWsSock

try:
    import uwsgi
except ImportError:
    uwsgi = None

has_uwsgi = True if uwsgi else False


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
