#!/usr/bin/env python

from collections import defaultdict
from imaplib import IMAP4
import logging
import subprocess
import socket
import time
import os
import sys
import ssl
try:
    import queue
except ImportError:
    import Queue as queue
from threading import Thread

from .arguments import get_arguments, print_help, print_version
from .channels import (get_channels, get_syncmap, iterate_stores,
                       populate_stores_w_mailboxes, ChannelError, MailboxError)
from .config import read_config, ConfigError
from .imapidle import ConnectionPool, IMAPTimeout, watch
from .util import PasswordError


logger = logging.getLogger(__name__)


class Task:
    """Base class for all tasks."""


class ErrorTask(Task):
    """Indicates unexpected exception in some thread."""

    def __init__(self, exc, exc_info):
        self.exc = exc
        self.exc_info = exc_info


class SyncTask(Task):
    """Run sync command task."""

    def __init__(self, syncpairs):
        """synpairs is a list of (storename, mailbox, path) tuples to sync."""
        self.syncpairs = syncpairs


class LocalMailTask(Task):
    """Check file changes in maildirs."""


def get_watch_callback(tasks, stname, mailbox, path):

    def callback(tasks=tasks, stname=stname, mailbox=mailbox):
        tasks.put_nowait(SyncTask([(stname, mailbox, path)]))

    return callback


def watch_errors(makecon, mailbox, callback, tasks):

    def errortask(e):
        tasks.put_nowait(ErrorTask(e, exc_info=sys.exc_info()))

    con = None
    while True:
        connected = False
        try:
            con = makecon(con)
            connected = True
            watch(con, mailbox, callback)
        except (ssl.SSLError, socket.error, IMAP4.abort, IMAPTimeout) as e:
            logger.log(logging.DEBUG if con.terminating else logging.ERROR,
                       '%s: %s', type(e), e,
                       exc_info=logger.isEnabledFor(logging.DEBUG))
            if con.terminating:
                break
            if con and isinstance(e, con.abort) and 'EOF' not in e.args[0]:
                errortask(e)
                break
            if not connected:
                logger.debug('reconnect in 30s')
                time.sleep(30)
        except Exception as e:
            errortask(e)
            break
        else:
            break               # watch was stopped


def watch_local(tasks, period=60):
    while True:
        time.sleep(period)
        tasks.put_nowait(LocalMailTask())


def start_watching(tasks, syncmap, stores, cpool, period=60):
    # for imap stores run threads tracking remote mailboxes
    for stname, box, path in syncmap:
        store = stores[stname]
        if 'imapstore' in store:

            def makecon(con, store=store):
                if con:
                    logger.debug('trying to reconnect')
                    return cpool.reconnect(
                        con, store['pass'], store['ssltype'])
                else:
                    return cpool.get_or_create_connection(
                        store['host'], store['user'], store['pass'],
                        store['port'], store['ssltype'])

            callback = get_watch_callback(tasks, stname, box, path)
            t = Thread(target=watch_errors,
                       args=(makecon, path, callback, tasks))
            t.daemon = True
            t.start()
    # run a single thread tracking all maildir changes
    t = Thread(target=watch_local, args=(tasks, period))
    t.daemon = True
    t.start()


def run_sync_command(command, mailboxes):
    """Sync. mailboxes is a dict {channel: [box1, box2, ...]}."""
    args = [ch + (':' + ','.join(boxes) if boxes else '')
            for ch, boxes in mailboxes.items()]
    if ' ' in command:
        shell = True
        command = ' '.join([command] + ["'%s'" % arg for arg in args])
    else:
        shell = False
        command = [command] + args
    logger.info(command if shell else ' '.join(command))
    subprocess.check_call(command, shell=shell)
    logger.debug("command completed")


def task_loop(tasks, syncmap, channels, stores, command):
    dircache = {}
    while True:

        try:
            # do not block to make keyboard interrupts work instantly
            task = tasks.get(True, 1e9)
        except queue.Empty:
            continue

        if isinstance(task, ErrorTask):
            logger.error(task.exc, exc_info=task.exc_info)
            raise SystemExit(1)
        elif isinstance(task, LocalMailTask):
            logger.debug("check maildir changes")
            pairs = []
            for stname, box, path in syncmap:
                store = stores[stname]
                if 'maildirstore' in store:
                    cur = os.path.join(path, 'cur')
                    dirset = set(os.listdir(cur))
                    if dircache.get(cur) != dirset:
                        logger.info("%s updated", path)
                        pairs.append(syncmap[(stname, box, path)][:-1])
                    dircache[cur] = dirset
            if pairs:
                tasks.put_nowait(SyncTask(pairs))
            logger.debug("check completed")
        elif isinstance(task, SyncTask):
            # sync
            mailboxes = defaultdict(list)
            for st, box, path in task.syncpairs:
                ch = syncmap[(st, box, path)][-1]
                if 'patterns' in channels[ch]:
                    mailboxes[ch].append(box)
                else:
                    mailboxes[ch] = []
            run_sync_command(command, mailboxes)

            # update parts of dircache
            for st, box, path in task.syncpairs:
                st2, bx2, pt2, _ = syncmap[(st, box, path)]
                store = stores[st2]
                if 'maildirstore' in store:
                    cur = os.path.join(pt2, 'cur')
                    dircache[cur] = set(os.listdir(cur))
        else:
            raise TypeError('task must be instance of some derivative of Task')
        tasks.task_done()


def make_sync_all_task(syncmap, stores):
    # prefer imap stores over maildirs, so the sync will be update dircache
    return SyncTask(list(set([p1 if 'imapstore' in stores[p1[0]] else p2[:-1]
                              for p1, p2 in syncmap.items()])))


def main():

    rt = logging.getLogger()
    rt.setLevel(logging.INFO)
    rt.addHandler(logging.StreamHandler())

    args = get_arguments()

    if args.help:
        print_help()
        raise SystemExit(0)
    elif args.version:
        print_version()
        raise SystemExit(0)
    elif args.debug:
        rt.setLevel(logging.DEBUG)
    elif args.quiet:
        rt.setLevel(logging.ERROR)
    elif args.error:
        logger.error(args.error)
        raise SystemExit(2)

    if not args.pos_args:
        logger.error("No channel specified. Try 'mbwatch -h'")
        raise SystemExit(1)

    try:
        config = read_config(args.mbsyncrc)
        channels = get_channels(args, config)
        logger.debug("channels: %s", channels)
    except (ConfigError, ChannelError) as e:
        logger.error(e)
        raise SystemExit(1)

    stores = dict(iterate_stores(channels))
    logger.debug("stores: %s", stores)

    cpool = ConnectionPool(debug=args.verbose)
    try:
        populate_stores_w_mailboxes(stores, cpool)
        syncmap = get_syncmap(channels)
        logger.debug("syncmap: %s", syncmap)

        tasks = queue.Queue()

        start_watching(tasks, syncmap, stores, cpool)

        syncall = make_sync_all_task(syncmap, stores)
        tasks.put_nowait(syncall)

        task_loop(tasks, syncmap, channels, stores, args.command)

    except (IMAP4.error, PasswordError, MailboxError,
            subprocess.CalledProcessError, KeyboardInterrupt) as e:
        logger.error(e)
        raise SystemExit(1)
    finally:
        cpool.close_all()


if __name__ == '__main__':
    main()
