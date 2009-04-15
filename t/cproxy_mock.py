import sys
import string
import socket
import select
import threading
import time
import re

import unittest

# The dollar ($) char adds implicit carriage-return/newline (\r\n).
#
# c means client connection.
# P means proxy upstream connection.
# d means proxy downstream connection.
# S means connection on fake, mock memcached server.
#
def debug(x):
    if False:
        print(x)

class MockServer(threading.Thread):
    def __init__(self, port):
        threading.Thread.__init__(self)
        self.host     = ''
        self.port     = port
        self.backlog  = 5
        self.server   = None
        self.running  = False
        self.sessions = []
        self.received = []

    def closeSessions(self):
        for s in self.sessions:
            s.close()
        self.sessions = []

    def close(self):
        self.running = False
        self.closeSessions()
        if self.server:
            self.server.close()
        self.server = None

    def run(self):
        self.running = True
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((self.host, self.port))
            self.server.listen(self.backlog)

            while self.running:
                debug("MockServer running " + str(self.port))
                client, address = self.server.accept()
                c = MockSession(client, address, self)
                debug("MockServer accepted " + str(self.port))
                self.sessions.insert(0, c)
                c.start()

        except KeyboardInterrupt:
            self.close()
            raise
        except socket.error, (value, message):
            self.close()
            debug("MockServer socket error: " + message)
            sys.exit(1)

        self.close()

class MockSession(threading.Thread):
    def __init__(self, client, address, server):
        threading.Thread.__init__(self)
        self.server  = server
        self.client  = client
        self.address = address
        self.recvlen = 1000
        self.running     = 0
        self.running_max = 10

    def run(self):
        input = [self.client]

        try:
            self.running = 1
            while (self.running > 0 and
                   self.running < self.running_max):
                debug("MockSession running (" + str(self.running) + ")")
                self.running = self.running + 1

                iready, oready, eready = select.select(input, [], [], 1)
                if len(eready) > 0:
                    self.running = 0
                elif len(iready) > 0:
                    data = self.client.recv(self.recvlen)
                    if data and len(data) > 0:
                        self.latest()
                        self.server.received.append(data)
                    else:
                        self.close()

        except KeyboardInterrupt:
            raise
        except:
            1

        if self.running >= self.running_max:
            print "MockSession running too long, shutting down"

        self.close()

    def latest(self):
        if self in self.server.sessions:
            self.server.sessions.remove(self)
        self.server.sessions.insert(0, self)

    def close(self):
        self.running = 0
        self.client.close()
        if self in self.server.sessions:
            self.server.sessions.remove(self)

sys.setcheckinterval(0)
g_mock_server_port = 11311
g_mock_server = MockServer(g_mock_server_port)
g_mock_server.start()
time.sleep(1)

class TestProxy(unittest.TestCase):
    def __init__(self, x):
        unittest.TestCase.__init__(self, x)
        self.proxy_port = 11333
        self.client = None

    def mock_server(self):
        global g_mock_server
        return g_mock_server

    def setUp(self):
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client.connect(("127.0.0.1", self.proxy_port))

    def tearDown(self):
        self.mock_close()
        self.client_close()

    def client_send(self, src, dst, what):
        w = string.strip(what, '^')
        w = string.replace(w, '$', "\r\n")
        debug("client sending " + w)
        self.client.send(w)

    def mock_send(self, src, dst, what):
        w = string.strip(what, '^')
        w = string.replace(w, '$', "\r\n")
        debug("mock sending " + w)
        self.mock_server().sessions[0].client.send(w)

    def client_recv(self, src, dst, what):
        debug("client_recv: " + src + " " + dst + " " + what)

        s = self.client.recv(1024)

        w = string.strip(what, '^')
        w = string.replace(w, '$', "\r\n")
        debug("recv expecting: " + w)
        debug("recv got: " + s)
        self.assertTrue(w == s or re.match(what, s) is not None)

    def mock_recv(self, src, dst, what):
        debug("mock_recv: " + src + " " + dst + " " + what)

        wait_max = 5

        s = ""
        i = 1
        while len(self.mock_server().sessions) <= 0 and i < wait_max:
            debug("sleeping waiting for mock_recv " + str(i))
            time.sleep(i)
            i = i + 1

        if len(self.mock_server().sessions) <= 0 and i >= wait_max:
            debug("waiting too long for mock_recv " + str(i))

        if len(self.mock_server().received) > 0:
            s = self.mock_server().received.pop(0)

        w = string.strip(what, '^')
        w = string.replace(w, '$', "\r\n")
        debug("recv expecting: " + w)
        debug("recv got: " + s)
        self.assertTrue(w == s or re.match(what, s) is not None)

    def wait(self, x):
        debug("wait " + str(x))
        time.sleep(x)

    def client_close(self):
        if self.client:
            self.client.close()
        self.client = None

    def mock_close(self):
        if self.mock_server():
            self.mock_server().closeSessions()

    def mock_quiet(self):
        return len(self.mock_server().received) <= 0

    # -------------------------------------------------

    def testBasicVersion(self):
        """Test version command does not reach mock server"""
        self.client_send('c', 'P', '^version$')
        self.client_recv('P', 'c', '^VERSION .*$')
        self.assertTrue(self.mock_quiet())

    def testBogusCommand(self):
        """Test bogus commands do not reach mock server"""
        self.client_send('c', 'P', '^bogus$')
        self.client_recv('P', 'c', '^.*ERROR.*$')
        self.assertTrue(self.mock_quiet())

    def testSimpleSet(self):
        """Test simple set against mock server"""
        self.client_send('c', 'P', '^set a 0 0 1$')
        self.client_send('c', 'P', '^1$')
        self.mock_recv('d', 'S', "^set a 0 0 1\r\n1$")
        self.mock_send('S', 'd', '^STORED$')
        self.client_recv('P', 'c', '^STORED$')

    def testFlushAllBroadcast(self):
        """Test flush_all scatter/gather"""
        self.client_send('c', 'P', '^flush_all$')
        self.mock_recv('d', 'S', '^flush_all$')
        self.mock_send('S', 'd', '^OK$')
        self.client_recv('P', 'c', '^OK$')

    def testSplitResponseOverTwoWrites(self):
        """Test split a response over two writes"""
        self.client_send('c', 'P', '^set a 0 0 1$')
        self.client_send('c', 'P', '^1$')
        self.mock_recv('d', 'S', "^set a 0 0 1\r\n1$")
        self.mock_send('S', 'd', '^STO')
        self.wait(1)
        self.mock_send('S', 'd', 'RED$')
        self.client_recv('P', 'c', '^STORED$')

    def testResponseChopWithServerClose(self):
        """Test chop the response with a server close"""
        self.client_send('c', 'P', '^set a 0 0 1$')
        self.client_send('c', 'P', '^1$')
        self.mock_recv('d', 'S', "^set a 0 0 1\r\n1$")
        self.mock_close()
        self.client_recv('P', 'c', '^SERVER_ERROR .*$')

if __name__ == '__main__':
    unittest.main()
