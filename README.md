# Websocket for Flask


## Server

### define a websocket server entry

create a "Sever" class like this:

```python

	from flaskws import ws_server, WsError, OP_TEXT
	
	@app.route('/ws/<int:some_id>')
	@ws_server
	class WebsocketServer(object):

		def __init__(self, ws_sock, **req_args):
			self.ws_sock = ws_sock
			print req_args['some_id']

		def on_open(self, ws_sock): pass

		def on_message(self, ws_sock, frame):
			fin, op, payload = frame

		def on_close(self, ws_sock): pass
```

or just use a view function to act as a websocket server:

```python

	from ws import ws_server_view

	@app.route('/ws/<int:some_id>')
	@ws_server_view
	def my_ws_echo_server(ws_sock, some_id=None):
		while True:
			frame = ws_sock.recv(timeout=5.0)
			if frame:
				fin, op, msg = frame
				if msg:
					ws_sock.send(msg)
					if msg == 'close':
						break

```

## Client

The client is standalone. it's a very simple implementation, using the old-good socket library. SSL is not supported currently.

```python

	from flaskws import ws_connect

	with ws_connect('ws://test.host/ws/123') as c:
		if c.handshake():
			c.send('hello!')
			for frame in c:
				print frame

```
