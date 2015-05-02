from collections import OrderedDict, defaultdict
from copy import copy
import os
import re
import logging

from .util import get_password


logger = logging.getLogger(__name__)


class ChannelError(Exception):
    pass


class MailboxError(Exception):
    pass


def get_channels(args, config):
    # TODO: channels should not share objects with config,
    # but channels with the same stores should share store objects
    channels = OrderedDict()
    if args.all_:
        channels = copy(config['channel'])
    for arg in args.pos_args:
        if arg in config['group']:
            for channel in config['group'][arg]:
                channels[channel] = copy(config['channel'][channel])
        else:
            channel = arg
            boxes = []
            if ':' in arg:
                channel, boxes = tuple(arg.split(':', 1))
                boxes = boxes.split(',')
            if channel in config['channel']:
                channels[channel] = copy(config['channel'][channel])
                if boxes:
                    channels[channel]['boxes'] = boxes
            else:
                raise ChannelError("unknown group or channel '%s'" % channel)
    for channel in channels.values():
        if 'patterns' in channel:
            patterns = channel['patterns']
            channel['regexps'] = list(map(pattern_to_regex, patterns))
    return channels


def iterate_stores(channels):
    stnames = set()
    for channel in channels.values():
        for store in (channel['master'], channel['slave']):
            stname = store.get('imapstore') or store['maildirstore']
            if stname not in stnames:
                stnames.add(stname)
                yield stname, store


box_re = re.compile(r'(?P<attr>\(.*\)) +"(?P<delim>.+?)" +"?(?P<name>.+?)"?$')
ns_re = re.compile(r'NIL|\(\("(?P<prefix>.*)"\ (NIL|"(?P<delim>.)")\)')


def populate_stores_w_mailboxes(stores, cpool):
    """Populate stores with mailboxes, delimiters and passwords"""
    for stname, store in stores.items():
        store['mailboxes'], store['delimiters'] = [], []
        if 'imapstore' in store:
            passwd = get_password(store)
            store['pass'] = passwd
            con = cpool.get_or_create_connection(
                store['host'], store['user'], passwd,
                store.get('useimaps', False))
            try:
                ns = con.namespace()
                m = ns_re.match(ns[1][0])
                if m:
                    prefix, delim = m.group('prefix'), m.group('delim')
                    delim = store.get('pathdelimiter', delim)
                    if delim:
                        store['delimiter'] = delim
                    store.setdefault('path', prefix or '')
            except con.error as e:
                logger.warning('namespace command failed: %s', e)
                store.setdefault('path', '')
            resp = con.list()
            for box in (b.decode() for b in resp[1]):
                m = box_re.match(box)
                if not m:
                    cpool.release(con)  # REMOVE
                    raise con.error("unexpected response from server: %s" % box)
                if '\\Noselect' not in m.group('attr'):
                    name = m.group('name')
                    if name.startswith(store['path']):
                        name = name[len(store['path']):]
                        store['mailboxes'].append(name)
                        store.setdefault('delimiter', m.group('delim'))
            cpool.release(con)
        else:
            store['inbox'] = os.path.expanduser(store['inbox'])
            store['path'] = os.path.expanduser(store['path'])
            store['delimiter'] = store.get('flatten', '/')
            for root, dirs, _ in os.walk(store['path']):
                if 'new' in dirs:
                    if root == store['inbox']:
                        box = 'INBOX'
                    else:
                        box = os.path.relpath(root, store['path'])
                    store['mailboxes'].append(box)
        logger.debug("store '%s' mailboxes: %s", stname, store['mailboxes'])


def pattern_to_regex(pattern, delimiter='/'):
    """Transform a mailbox pattern to a regex."""
    neg = pattern.startswith('!')
    patre = pattern[1:] if neg else pattern
    patre = re.escape(patre)
    patre = patre.replace('\\*', '.*').replace('\\%', '[^' + delimiter + ']')
    patre += '$'
    return neg, re.compile(patre)


def get_normalized_box(mailbox, prefix, delimiter):
    """Transform prefixed mailbox to slash-delimited unprefixed one."""
    mailbox = mailbox.replace(delimiter, '/')
    if mailbox.startswith(prefix):
        mailbox = mailbox[len(prefix):]
    return mailbox


def get_store_box(mailbox, prefix, delimiter):
    """Transform unprefixed slash-delimited mailbox to the prefixed one. """
    return (prefix + mailbox).replace('/', delimiter)


def get_box_path(sbox, store):
    return store['path'] + sbox if sbox != 'INBOX' else store.get('inbox', 'INBOX')


def get_syncmap(channels):
    """Return a bi-directional mapping between (store, mailbox, path) tuples.
    The right side tuple is (store, mailbox, path, channelname).
    """
    syncmap = {}
    for chname, channel in channels.items():
        pairs = defaultdict(list)
        for stype in ('master', 'slave'):
            store = channel[stype]
            stname = store.get('imapstore') or store['maildirstore']
            prefix = channel[stype + '_box']
            delim = store['delimiter']
            if 'boxes' in channel:
                for box in channel['boxes']:
                    sbox = get_store_box(box, prefix, delim)
                    if sbox not in store['mailboxes']:
                        raise MailboxError("mailbox '%s' not found in "
                                           "store '%s'" % (box, stname))
                    path = get_store_box(sbox, store)
                    pairs[box].append((stname, box, path))
            elif 'regexps' in channel:
                for sbox in store['mailboxes']:
                    box = get_normalized_box(sbox, prefix, delim)
                    for neg, regex in reversed(channel['regexps']):
                        m = regex.match(box)
                        if m:
                            logger.debug("box %s matches %s", box, regex.pattern)
                            if not neg:
                                path = get_box_path(sbox, store)
                                pairs[box].append((stname, box, path))
                            break
            else:  # single box channel
                box = prefix or 'INBOX'
                sbox = get_store_box(box, '', delim)
                path = get_box_path(sbox, store)
                pairs[''].append((stname, box, path))
        # bi-directional map of (storename, mailbox) pairs
        for pair in pairs.values():
            if len(pair) != 2:
                stname, box, path = pair[0]
                raise MailboxError(
                    "No matching mailbox for '%s:%s' in channel '%s'" %
                    (stname, box, chname))
            syncmap.update({pair[0]: pair[1] + (chname,),
                            pair[1]: pair[0] + (chname,)})
    return syncmap
