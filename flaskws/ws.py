#-*- coding: utf-8 -*-

import functools
import json
import logging
import traceback
from werkzeug.routing import Map, Rule
# from werkzeug.wrappers import Response as BaseResponse
# from werkzeug.datastructures import Headers
import threading
from defs import *
from wssock import WsSocket, _BaseWsSock
from wsuwsgi import UwsgiWsSock, has_uwsgi
from wstornado import TornadoWebSocketAdapter

    
class WsDeliver(Exception):

    def __init__(self, response):
        self.response = response

    
class WsMiddleware(object):

    def __init__(self, wsgi_app, *args, **kargs):
        self.wsgi_app = wsgi_app
        self.app = args[0] if args else None
        self.wss = args[1] if len(args) > 1 else None
        self.use_tornado = kargs.get('use_tornado', False)

    def __call__(self, *args):
        try:
            return self._call(*args)
        finally:
            pass

    def _call(self, environ, start_response):
        logging.debug('WsMiddleware.__call__')
        # join dead threads
        curr_th = threading.current_thread()
        for th in threading.enumerate():
            if not th or th == curr_th:
                continue
            if th.is_alive():
                continue
            th.join()
            logging.debug('thread %s joinned.' % th.name)
        try:
            return self.wsgi_app(environ, start_response)
        except WsDeliver, e:
            if has_uwsgi:
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
        if has_uwsgi:
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
            logging.debug('WsResponseForServer.run(), use_tornado.')
            _t_app = environ['x.tornado.app']
            _t_request = environ['x.tornado.request']
            sock = TornadoWebSocketAdapter(environ, _t_app, _t_request)
            if sock.handshake():
                logging.debug('WsResponseForServer.run(), handshake ok.')
                return sock.server(self.server_cls(sock, **self.values))
            else:
                logging.debug('WsResponseForServer.run(), handshake fail.')
                return sock.html()
        if has_uwsgi:
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
        logging.debug('WsResponseForServer.__call__: raise deliver')
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


# class WsAppDirect(object):
#     u'''用于直接使用socket实现http服务器io的情形
#     例如from werkzeug.serving import run_simple'''

#     def __init__(self, server_cls, **kargs):
#         self.server_cls = server_cls
#         self.kargs = kargs

#     def __call__(self, environ, start_response):
#         sock = WsSocket(environ, None, None)
#         resp = None
#         if not sock.handshake(environ, start_response):
#             resp = sock.html(environ, start_response)
#         else:
#             print 'handshake ok.'
#             resp = sock.server(self.server_cls(sock, **self.kargs))
#         print type(resp)
#         print resp
#         for i in resp:
#             print i
#             yield i
#         print '--- done. ---'

# def ws_server_direct(server_cls):
#     u'''用于直接使用socket实现http服务器io的情形
#     例如from werkzeug.serving import run_simple'''
#     @functools.wraps(server_cls)
#     def wrapper(**kargs):
#         return WsAppDirect(server_cls, **kargs)
#     return wrapper
