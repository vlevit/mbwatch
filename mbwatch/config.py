from collections import OrderedDict
import os
import shlex


class ConfigError(Exception):

    def __str__(self):
        if len(self.args) > 1:
            return "line %d: %s" % (self.args[0], self.args[1])
        else:
            return "%s" % self.args[0]


def _read_config(file):
    config = {}
    config['group'] = {}
    lno = 0
    for l in file:
        lno += 1
        l = l.strip()
        if not l or l.startswith('#'):
            continue
        try:
            option, value = tuple(l.split(None, 1))
        except ValueError:
            raise ConfigError(lno, "option %s doesn't have any value" % l)
        option = option.lower()
        try:
            values = shlex.split(value)
        except ValueError as e:
            raise ConfigError(lno, "%s: %s" % (l, e))
        value = values[0]
        if option in ('maildirstore', 'imapstore', 'channel'):
            if option not in config:
                config[option] = OrderedDict()
            if value not in config[option]:
                config[option][value] = OrderedDict({option: value})
            current = config[option][value]
        elif option in ('master', 'slave'):
            value = value.split(':')[1:]
            if len(value) != 2:
                raise ConfigError(lno, "%s - %s value must be in format "
                                  ":store:[mailbox]" % (l, option))
            sname, box = tuple(value)
            if sname in config['maildirstore']:
                typ = 'maildirstore'
            elif sname in config['imapstore']:
                typ = 'imapstore'
            else:
                raise ConfigError(lno, "no store '%s'" % sname)
            current[option] = config[typ][sname]
            current[option + '_box'] = box
        elif option == 'patterns':
            current[option] = values
        elif option == 'group':
            config['group'][values[0]] = values[1:]
        else:
            if value == 'yes':
                value = True
            elif value == 'no':
                value = False
            current[option] = value
    return config


def read_config(path="~/.mbsyncrc"):
    try:
        with open(os.path.expanduser(path)) as file:
            return _read_config(file)
    except IOError as e:
        raise ConfigError(e)
