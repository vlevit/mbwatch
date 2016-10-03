import ctypes.util
import getpass
import logging
import sys
import subprocess


logger = logging.getLogger(__name__)


class PasswordError(Exception):
    pass


def get_password(store):
    passwd = None
    if 'pass' in store:
        passwd = store['pass']
    elif 'passcmd' in store:
        try:
            passwd = subprocess.check_output(store['passcmd'], shell=True)
            passwd = passwd.decode(sys.stdout.encoding or 'UTF-8')
        except (OSError, subprocess.CalledProcessError,
                UnicodeDecodeError) as e:
            raise PasswordError('getting password failed: ' + str(e))
    else:
        passwd = getpass("Password (%s):" % store.get('imapstore'))
    return passwd


def __load_res_init():

    c = None
    so = ctypes.util.find_library('c')
    res_init = lambda: -1

    if so:
        try:
            libc = ctypes.cdll.LoadLibrary(so)
            res_init = libc.__res_init
        except (OSError, AttributeError) as e:
            logger.warn("can't load res_init: %s")
    else:
        logger.warn("can't load res_init: c library not found")

    return res_init


res_init = __load_res_init()
