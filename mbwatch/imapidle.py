from collections import defaultdict
import logging
import imaplib
import socket
import ssl
from threading import RLock


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


def _mesg(s, secs=None):
    if ' LOGIN ' in s:          # do not log passwords
        s = s[:s.find(' LOGIN ') + 11] + '...'
    logger.debug(s)


def _send(con, data):
    log = con._mesg if con.debug >= 4 else con._log
    log('> ' + data)
    try:
        con.send(data + '\r\n')
    except (socket.error, OSError) as val:
        raise con.abort('socket error: %s' % val)


def _recv(imap):
    try:
        resp = imap._get_line().rstrip()
    except (socket.error, ssl.SSLError) as e:
        if "timed out" in e.args[0]:
            raise IMAPTimeout
        raise
    parts = resp.split(None, 2)
    if len(parts) < 2:
        raise imap.abort('unexpected response: %s' % resp)
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ''


def idle(con, timeout=29*60):
    con.sock.settimeout(timeout)
    while True:
        tag = con._new_tag()
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
        con.select(mailbox, True)
        for m in idle(con):
            callback()
        con.close()
    else:
        raise con.abort('idle is not supported')
    con.logout()


class ConnectionPool:

    _busy = defaultdict(list)
    _released = defaultdict(list)
    _con_key_map = {}

    def __init__(self, debug=False):
        self.debug = 4 if debug else 0
        self.lock = RLock()

    def get_or_create_connection(self, host, user, password, imaps=True):
        port = imaplib.IMAP4_SSL_PORT if imaps else imaplib.IMAP4_PORT
        key = (host, port, user)
        # get free connection if available
        with self.lock:
            if self._released.get(key):
                imap = self._released[key].pop()
                self._busy[key].append(imap)
                return imap
        # otherwise create new
        imap = self._connect(host, port, user, password, imaps)
        self._add_connection(imap, key)
        return imap

    def reconnect(self, con, password):
        key = self._con_key_map[con]
        host, port, user = key
        imap = self._connect(host, port, user, password)
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
        con.sock.settimeout(3)
        try:
            if getattr(con, 'idling', False):
                _send(con, 'DONE')
            con.logout()
        except (imaplib.IMAP4.error, socket.error, OSError) as e:
            logger.error("error on shutting down the connection %s ", e)
        self._remove_connection(con)

    def close_all(self):
        for con in list(self._con_key_map):
            self.close(con)

    def _connect(self, host, port, user, password, imaps=True):
        if imaps:
            imap = imaplib.IMAP4_SSL(host, port)
        else:
            imap = imaplib.IMAP4(host, port)
        imap.debug = self.debug
        imap._mesg = _mesg
        if not imaps and hasattr(imap, 'starttls'):
            imap.starttls()
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