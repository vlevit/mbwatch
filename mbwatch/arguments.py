import sys

from . import __version__


def get_version():
    return "mbwatch %s" % __version__


def print_version():
    print(get_version())


def print_help():
    print(
"""%(version)s - mailbox watcher
Copyright (C) 2015 Vyacheslav Levit <dev@vlevit.org>
usage:
 mbwatch [flags] {{channel[:box,...]|group} ...|-a}
  -e, --command         syncing command (default is mbsync)
  -a, --all             operate on all defined channels
  -l, --list            list mailboxes instead of syncing them
  -c, --config CONFIG   read an alternate config file (default: ~/.mbsyncrc)
  -D, --debug           print debugging messages
  -V, --verbose         verbose mode (display network traffic)
  -q, --quiet           print only errors
  -v, --version         display version
  -h, --help            display this help message

""") % {'version': get_version()}


class Arguments:
    command = "mbsync"
    mbsyncrc = "~/.mbsyncrc"
    all_ = False
    list_ = False
    debug = False
    verbose = False
    quiet = False
    pos_args = []
    version = False
    help = False
    error = None


def get_arguments():
    args = Arguments()
    skip = False
    cmd = sys.argv[1:]
    for i, arg in enumerate(cmd):
        if skip:
            skip = False
            continue
        elif arg in ('-e', '--command'):
            if len(cmd) > i + 1:
                args.command = cmd[i + 1]
            skip = True
        elif arg in ('-a', '--all'):
            args.all_ = True
        elif arg in ('-l', '--list'):
            args.list_ = True
        elif arg in ('-c', '--config'):
            if len(args) > i + 1:
                args.mbsyncrc = cmd[i + 1]
            skip = True
        elif arg in ('-D', '--debug'):
            args.debug = True
        elif arg in ('-V', '--verbose'):
            args.verbose = True
        elif arg in ('-q', '--quiet'):
            args.quiet = True
        elif arg in ('-v', '--version'):
            args.version = True
        elif arg in ('-h', '--help'):
            args.help = True
            break
        elif arg.startswith('-'):
            args.error = "unknown option '%s', see --help" % arg
            break
        else:
            args.pos_args.append(arg)
    return args
