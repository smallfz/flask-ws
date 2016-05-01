#-*- coding: utf-8 -*-

import threading
from tornado.wsgi import WSGIContainer
from tornado import httputil
from ws import TornadoWebSocketAdapter


class WsWSGIContainer(WSGIContainer):

    def __init__(self, app):
        super(WsWSGIContainer, self).__init__(app)

    def __call__(self, request):
        data = {}
        response = []
        def _start_response(status, headers):
            data['status'] = status
            data['headers'] = headers
            return response.append
        environ = WSGIContainer.environ(request)
        environ['x.tornado.request'] = request
        environ['x.tornado.app'] = self.wsgi_application
        app_response = self.wsgi_application(environ, _start_response)
        if app_response:
            if isinstance(app_response, TornadoWebSocketAdapter):
                return
        try:
            response.extend(app_response)
            body = b''.join(response)
        finally:
            if hasattr(app_response, 'close'):
                app_response.close()
        if not data:
            raise Exception("WSGI app did not call start_response")
        code, reason = data['status'].split(' ', 1)
        code = int(code)
        _headers = data['headers']
        if code != 304:
            _hkeys = [k[0].lower() for k in _headers]
            if 'content-length' not in _hkeys:
                _headers.append(('content-length', '%d' % len(body)))
            if 'content-type' not in _hkeys:
                _headers.append(('content-type', 'text/plain; charset=utf-8'))
        line0 = httputil.ResponseStartLine('http/1.1', code, reason)
        headers = httputil.HTTPHeaders()
        for k, v in _headers:
            headers.add(k, v)
        request.connection.write_headers(line0, headers, chunk=body)
        request.connection.finish()
