import sys
import subprocess
import getpass


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


