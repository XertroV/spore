import asyncio
import traceback
import threading
import sys
import socket
import collections
import random
import encodium
from .fields import Message, Peer, Info

class Spore(object):
    class Protocol(asyncio.Protocol):
        def __init__(self, spore):
            self._spore = spore
            self._buffer = bytearray()
            self.data = dict()
            self._transport = None

        def connection_made(self, transport):
            # TODO: check if we're full, and let them know.
            self._spore._protocols.append(self)
            self.address = transport.get_extra_info('peername')
            assert self._transport is None
            self._transport = transport

            info = Info.make(version=0, nonce=self._spore._nonce)
            if self._spore._address:
                info.port = self._spore._address[1]
            self.send('spore_info', info)


        def send(self, method, payload=b''):
            if hasattr(payload, 'serialize'):
                payload = payload.serialize()
            message = Message.make(method=method, payload=payload).serialize()
            self._transport.write(len(message).to_bytes(4, 'big'))
            self._transport.write(message)

        def data_received(self, data):
            for byte in data:
                self._buffer.append(byte)
                length = len(self._buffer)
                if length > 4 and length - 4 == int.from_bytes(self._buffer[0:4], 'big'):
                    message = Message.make(bytes(self._buffer[4:]))
                    for callback, deserialize in self._spore._on_message_callbacks[message.method]:
                        if deserialize:
                            if deserialize.__class__ == type and issubclass(deserialize, encodium.Field):
                                sys.stderr.write("Warning: passing encodium fields into spore is deprecated and will be removed in version 1.0\n")
                                deserialize = deserialize.make
                            callback(self, deserialize(message.payload))
                        else:
                            callback(self, message.payload)
                    self._buffer.clear()

        def connection_lost(self, exc):
            for callback in self._spore._on_disconnect_callbacks:
                callback(self)
            self._spore._protocols.remove(self)
            if len(self._spore._protocols) == 0:
                asyncio.Task(self._spore._notify_protocols_empty(), loop=self._spore._loop)

    def __init__(self, seeds=[], address=None):
        self._loop = None
        self._main_thread = None
        self._protocols = []
        self._clients = []
        self._server = None
        self._known_addresses = seeds
        self._try_new_connections = None
        self._banned_addresses = []
        self._address = address
        self._main_task = None
        self._on_connect_callbacks = []
        self._on_disconnect_callbacks = []
        self._on_message_callbacks = collections.defaultdict(list)
        self._nonce = random.randint(0,2**32-1)

        @self.on_message('peer', Peer.make)
        def receive_peer(from_peer, new_peer):
            # TODO: track which peers know about which peers to reduce traffic by a factor of two.
            # TODO: Do not relay this peer if it's on a network that is unreachable.
            address = (socket.inet_ntoa(new_peer.ip), new_peer.port)
            if address not in self._known_addresses:
                self.broadcast('peer', new_peer, exclude=from_peer)
                self._known_addresses.append(address)
                self._try_new_connections.set()


        @self.on_message('spore_info', Info.make)
        def info(peer, info):
            if info.nonce == self._nonce:
                if (peer.address[0], info.port) not in self._banned_addresses:
                    self._banned_addresses.append((peer.address[0], info.port))
                peer._transport.close()
            else:
                if info.port:
                    receive_peer(peer, Peer.make(ip=socket.inet_aton(peer.address[0]), port=info.port))
                for address in self._known_addresses:
                    peer.send('peer', Peer.make(ip=socket.inet_aton(address[0]), port=address[1]))
                for callback in self._on_connect_callbacks:
                    callback(peer)


    def on_connect(self, func):
        self._on_connect_callbacks.append(func)
        return func

    def on_disconnect(self, func):
        self._on_disconnect_callbacks.append(func)
        return func

    def on_message(self, method, deserialize=None):
        def wrapper(func):
            self._on_message_callbacks[method].append((func, deserialize))
            return func

        return wrapper

    def handler(self, method):
        return self.on_message(method)

    def broadcast(self, method, data, exclude=[]):
        if hasattr(data, 'serialize'):
            data = data.serialize()
        for protocol in self._protocols:
            if not isinstance(exclude, list):
                exclude = [exclude]
            if protocol not in exclude:
                protocol.send(method, data)

    def num_connected_peers(self):
        return len(self._protocols)

    def run(self):

        # First, set the event loop.
        self._loop = asyncio.new_event_loop()
        self._try_new_connections = asyncio.Event(loop=self._loop)
        self._try_new_connections.set()
        self._protocols_empty_cv = asyncio.Condition(loop=self._loop)
        self._main_thread = threading.current_thread()

        # Sanity check.
        assert self._main_task is None

        # Set up main task.
        coroutines = [self._create_server(), self._connect_loop()]
        self._main_task = asyncio.Task(asyncio.wait(coroutines, loop=self._loop), loop=self._loop)

        # Run them
        try:
            self._loop.run_until_complete(self._main_task)
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._loop.run_until_complete(self._clean_up())

        self._main_task = None
        self._loop.close()
        self._loop = None

    def shutdown(self):
        assert self._loop is not None, "spore instance run() must be called before shutdown()"
        if self._main_task is None:
            sys.stderr.write("Warning: shutdown called on spore instance that is stopped.\n")
        else:
            self._loop.call_soon_threadsafe(self._main_task.cancel)
            self._main_thread.join()
            self._main_thread = None

    def _protocol_factory(self):
        return Spore.Protocol(self)

    def _should_connect_to(self, address):
        if address in self._banned_addresses:
            return False
        for protocol in self._protocols:
            if protocol.address == address:
                return False
            # TODO: check other ways in which this peer might prevent us from connecting to address
        return True

    @asyncio.coroutine
    def _clean_up(self):
        for protocol in self._protocols:
            protocol._transport.close()
        if self._server:
            self._server.close()
        yield from self._wait_protocols_empty()

    @asyncio.coroutine
    def _connect_loop(self):
        while True:
            yield from self._try_new_connections.wait()
            self._try_new_connections.clear()

            try_again = False

            # Try connecting to all peers.
            for address in self._known_addresses:
                if self._should_connect_to(address):
                    try:
                        result = yield from self._loop.create_connection(self._protocol_factory, address[0], address[1])
                    except ConnectionRefusedError:
                        # If there's something to try that failed, try again real quick.
                        # This is mostly useful for tests.
                        try_again = True
                        # TODO: mark that this peer should not be tried for a while
                        #       (perhaps there is a better way to do this logic anyway, on a per-known-address basis?)
                        # TODO: increase misbehaving for that peer.

            if try_again:
                self._loop.call_later(0.01, self._try_new_connections.set)

    @asyncio.coroutine
    def _create_server(self):
        if self._address:
            #print("Listening on",self._address)
            self._server = yield from self._loop.create_server(self._protocol_factory, *self._address)
            #print('serving on {}'.format(self._server.sockets[0].getsockname()))

    @asyncio.coroutine
    def _wait_protocols_empty(self):
        if len(self._protocols) != 0:
            with (yield from self._protocols_empty_cv):
                yield from  self._protocols_empty_cv.wait()

    @asyncio.coroutine
    def _notify_protocols_empty(self):
        with (yield from self._protocols_empty_cv):
            self._protocols_empty_cv.notify_all()
