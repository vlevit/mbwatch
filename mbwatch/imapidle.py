from collections import defaultdict
from functools import wraps
import logging
import imaplib
import socket
import ssl
from threading import RLock

from .six import b, s, string_types, PY3

logger = logging.getLogger(__name__)
logger.propagate = False
# allow everything since verbosity is controlled by imaplib variables
logger.setLevel(logging.DEBUG)
lh = logging.StreamHandler()
logger.addHandler(lh)
lh.setFormatter(logging.Formatter(
    '  %(threadName)s %(asctime)s.%(msecs)02d %(message)s', '%M:%S'))


class IMAPTimeout(Exception):
    pass


class StopIdle(Exception):
    pass


def idle_terminate(f):
    """
    If connection is terminating receive data from the connection
    in a loop and raise StopIdle when socket is closed. Expect the
    first argument to be an IMAP connection.

    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        con = args[0]
        if con.terminating:
            try:
                while _recv_simple(con):
                    pass
            except (socket.error, con.error) as e:
                logger.debug('idle terminate: %s' % e)
            raise StopIdle
        else:
            return f(*args, **kwargs)

    return wrapper


def _mesg(dat, secs=None):
    dat = s(dat) if isinstance(dat, bytes) else dat
    if ' LOGIN ' in dat:          # do not log passwords
        dat = dat[:dat.find(' LOGIN ') + 11] + '...'
    logger.debug(dat)


def _send_simple(con, data):
    log = con._mesg if con.debug >= 4 else con._log
    log('> ' + data)
    try:
        con.send(b(data + '\r\n'))
    except (socket.error, OSError) as val:
        raise con.abort('socket error: %s' % val)


@idle_terminate
def _send(con, data):
    return _send_simple(con, data)


def _recv_simple(con):
    try:
        resp = s(con._get_line()).rstrip()
    except (socket.error, ssl.SSLError) as e:
        if isinstance(e.args[0], string_types) and "timed out" in e.args[0]:
            # Socket files are not readable after timeout occurred.
            # https://bugs.python.org/issue7322
            # Should we use additional threads/gevent for long polling
            # instead of socket timeouts?
            if PY3:
                con.file = con.sock.makefile('rb')
            raise IMAPTimeout(e.args[0])
        raise
    parts = resp.split(None, 2)
    if len(parts) < 2:
        raise con.abort('unexpected response: %s' % resp)
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ''


@idle_terminate
def _recv(con):
    return _recv_simple(con)


def _logout(con):
    """Send LOGOUT and shutdown, but don't try to receive any response."""
    tag = s(con._new_tag())
    _send_simple(con, '%s %s' % (tag, 'LOGOUT'))
    con.shutdown()


def idle(con, timeout=29*60):
    con.sock.settimeout(timeout)
    while True:
        tag = s(con._new_tag())
        _send(con, '%s %s' % (tag, 'IDLE'))
        con.idling = True
        token = None
        # wait for '+ [idling]' response
        while token != '+':
            token, resp, text = _recv(con)
            if token not in ('+', '*'):
                raise con.abort('unexpected response: %s %s %s' %
                                (token, resp, text))
            if resp in ('NO', 'BAD'):
                raise con.abort('idle is not known or allowed')
        # wait for '* <X> EXISTS' response
        if token == '+' and text != 'EXISTS':
            while True:
                try:
                    token, resp, text = _recv(con)
                except IMAPTimeout:
                    break
                if text == 'EXISTS':
                    break
        _send(con, 'DONE')
        con.idling = False
        # wait for '<TAG> OK [IDLE terminated]'
        while True:
            tk, ok, txt = _recv(con)
            if tk == tag:
                if ok == 'OK':
                    break
                else:
                    raise con.abort('idle failed: %s %s %s' % (tk, ok, txt))
        if text == 'EXISTS':
            yield


def watch(con, mailbox, callback):
    if 'IDLE' in con.capabilities:
        con.select(con._quote(mailbox), True)
        try:
            for m in idle(con):
                callback()
        except StopIdle:
            logger.debug("watch loop stopped")
    else:
        raise con.abort("idle is not supported")


def starttls(con, ssl_context=None):
    """Python3's imaplib starttls port for Python2."""
    name = 'STARTTLS'
    if getattr(con, '_tls_established', False):
        raise con.abort('TLS session already established')
    if name not in con.capabilities:
        raise con.abort('TLS not supported by server')
    # Generate a default SSL context if none was passed.
    if ssl_context is None:
        ssl_context = ssl._create_stdlib_context()
    tag = con._new_tag()
    _send(con, '%s %s' % (tag, name))
    token = None
    while token != tag:
        token, resp, text = _recv(con)
    if resp == 'OK':
        con.sock = ssl_context.wrap_socket(con.sock, server_hostname=con.host)
        con.file = con.sock.makefile('rb')
        con._tls_established = True
        # update capabilities
        typ, dat = con.capability()
        if dat == [None]:
            raise con.error('no CAPABILITY response from server')
        con.capabilities = tuple(dat[-1].upper().split())
    else:
        raise con.error("Couldn't establish TLS session")


class ConnectionPool:

    _busy = defaultdict(list)
    _released = defaultdict(list)
    _con_key_map = {}

    def __init__(self, debug=False):
        self.debug = 4 if debug else 0
        self.lock = RLock()

    def get_or_create_connection(self, host, user, password, port=143,
                                 ssltype='STARTTLS'):
        key = (host, port, user)
        # get free connection if available
        with self.lock:
            if self._released.get(key):
                imap = self._released[key].pop()
                self._busy[key].append(imap)
                return imap
        # otherwise create new
        imap = self._connect(host, port, user, password, ssltype)
        self._add_connection(imap, key)
        return imap

    def reconnect(self, con, password, ssltype):
        key = self._con_key_map[con]
        host, port, user = key
        imap = self._connect(host, port, user, password, ssltype)
        with self.lock:
            self._add_connection(imap, key)
            self._remove_connection(con)
        return imap

    def release(self, con):
        with self.lock:
            key = self._con_key_map[con]
            self._released[key].append(self._busy[key].pop())

    def count(self):
        return len(self._con_key_map)

    def close(self, con):
        # avoid long locks in case of errors
        con.terminating = True
        con.sock.settimeout(3)
        try:
            if con.idling:
                _send_simple(con, 'DONE')
            _logout(con)
        except (imaplib.IMAP4.error, socket.error, OSError) as e:
            logger.error("error on shutting down the connection %s ", e)
        self._remove_connection(con)

    def close_all(self):
        for con in list(self._con_key_map):
            self.close(con)

    def _connect(self, host, port, user, password, ssltype):
        if ssltype == 'STARTTLS':
            imap = imaplib.IMAP4(host, port)
        else:
            imap = imaplib.IMAP4_SSL(host, port)
        imap.debug = self.debug
        imap._mesg = _mesg
        imap.idling = False
        imap.terminating = False
        if ssltype == 'STARTTLS':
            if hasattr(imap, 'starttls'):
                imap.starttls()
            else:
                starttls(imap)
        imap.login(user, password)
        return imap

    def _add_connection(self, con, key):
        with self.lock:
            self._busy[key].append(con)
            self._con_key_map[con] = key

    def _remove_connection(self, con):
        with self.lock:
            key = self._con_key_map[con]
            if con in self._released[key]:
                self._released[key].remove(con)
            elif con in self._busy[key]:
                self._busy[key].remove(con)
            del self._con_key_map[con]
