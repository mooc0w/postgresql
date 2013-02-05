#!/usr/bin/env python
# vim: et ai ts=4 sw=4:

import json
import yaml
import os
import glob
import random
import re
import shutil
import string
import subprocess
import sys
import time
from yaml.constructor import ConstructorError
import commands
from pwd import getpwnam
from grp import getgrnam

# These modules may not be importable until after the install hook has
# run.
try:
    import psycopg2
    from jinja2 import Template
except ImportError:
    pass


###############################################################################
# Supporting functions
###############################################################################
MSG_CRITICAL = "CRITICAL"
MSG_DEBUG = "DEBUG"
MSG_INFO = "INFO"
MSG_ERROR = "ERROR"
MSG_WARNING = "WARNING"


def juju_log(level, msg):
    subprocess.call(['/usr/bin/juju-log', '-l', level, msg])


###############################################################################

# Volume managment
###############################################################################
#------------------------------
# Get volume-id from juju config "volume-map" dictionary as
#     volume-map[JUJU_UNIT_NAME]
# @return  volid
#
#------------------------------
def volume_get_volid_from_volume_map():
    volume_map = {}
    try:
        volume_map = yaml.load(config_data['volume-map'])
        if volume_map:
            return volume_map.get(os.environ['JUJU_UNIT_NAME'])
    except ConstructorError as e:
        juju_log(MSG_WARNING, "invalid YAML in 'volume-map': %s", e)
    return None


# Is this volume_id permanent ?
# @returns  True if volid set and not --ephemeral, else:
#           False
def volume_is_permanent(volid):
    if volid and volid != "--ephemeral":
        return True
    return False


#------------------------------
# Returns a mount point from passed vol-id, e.g. /srv/juju/vol-000012345
#
# @param  volid          volume id (as e.g. EBS volid)
# @return mntpoint_path  eg /srv/juju/vol-000012345
#------------------------------
def volume_mount_point_from_volid(volid):
    if volid and volume_is_permanent(volid):
        return "/srv/juju/%s" % volid
    return None


# Do we have a valid storage state?
# @returns  volid
#           None    config state is invalid - we should not serve
def volume_get_volume_id():
    ephemeral_storage = config_data['volume-ephemeral-storage']
    volid = volume_get_volid_from_volume_map()
    juju_unit_name = os.environ['JUJU_UNIT_NAME']
    if ephemeral_storage in [True, 'yes', 'Yes', 'true', 'True']:
        if volid:
            juju_log(MSG_ERROR, "volume-ephemeral-storage is True, but " +
                     "volume-map['%s'] -> %s" % (juju_unit_name, volid))
            return None
        else:
            return "--ephemeral"
    else:
        if not volid:
            juju_log(MSG_ERROR, "volume-ephemeral-storage is False, but " +
                     "no volid found for volume-map['%s']" % (juju_unit_name))
            return None
    return volid


# Initialize and/or mount permanent storage, it straightly calls
# shell helper
def volume_init_and_mount(volid):
    command = ("scripts/volume-common.sh call " +
              "volume_init_and_mount %s" % volid)
    output = run(command)
    if output.find("ERROR") >= 0:
        return False
    return True


def volume_get_all_mounted():
    command = ("mount |egrep /srv/juju")
    status, output = commands.getstatusoutput(command)
    if status != 0:
        return None
    return output


#------------------------------------------------------------------------------
# Enable/disable service start by manipulating policy-rc.d
#------------------------------------------------------------------------------
def enable_service_start(service):
    ### NOTE: doesn't implement per-service, this can be an issue
    ###       for colocated charms (subordinates)
    juju_log(MSG_INFO, "NOTICE: enabling %s start by policy-rc.d" % service)
    if os.path.exists('/usr/sbin/policy-rc.d'):
        os.unlink('/usr/sbin/policy-rc.d')
        return True
    return False


def disable_service_start(service):
    juju_log(MSG_INFO, "NOTICE: disabling %s start by policy-rc.d" % service)
    policy_rc = '/usr/sbin/policy-rc.d'
    policy_rc_tmp = "%s.tmp" % policy_rc
    open('%s' % policy_rc_tmp, 'w').write("""#!/bin/bash
[[ "$1"-"$2" == %s-start ]] && exit 101
exit 0
EOF
""" % service)
    os.chmod(policy_rc_tmp, 0755)
    os.rename(policy_rc_tmp, policy_rc)


#------------------------------------------------------------------------------
# run: Run a command, return the output
#------------------------------------------------------------------------------
def run(command, exit_on_error=True):
    try:
        juju_log(MSG_INFO, command)
        return subprocess.check_output(
            command, stderr=subprocess.STDOUT, shell=True)
    except subprocess.CalledProcessError, e:
        juju_log(MSG_ERROR, "status=%d, output=%s" % (e.returncode, e.output))
        if exit_on_error:
            sys.exit(e.returncode)
        else:
            raise


#------------------------------------------------------------------------------
# install_file: install a file resource. overwites existing files.
#------------------------------------------------------------------------------
def install_file(contents, dest, owner="root", group="root", mode=0600):
        uid = getpwnam(owner)[2]
        gid = getgrnam(group)[2]
        dest_fd = os.open(dest, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, mode)
        os.fchown(dest_fd, uid, gid)
        with os.fdopen(dest_fd, 'w') as destfile:
            destfile.write(str(contents))


#------------------------------------------------------------------------------
# install_dir: create a directory
#------------------------------------------------------------------------------
def install_dir(dirname, owner="root", group="root", mode=0700):
    command = \
    '/usr/bin/install -o {} -g {} -m {} -d {}'.format(owner, group, oct(mode),
        dirname)
    return run(command)


#------------------------------------------------------------------------------
# postgresql_stop, postgresql_start, postgresql_is_running:
# wrappers over invoke-rc.d, with extra check for postgresql_is_running()
#------------------------------------------------------------------------------
def postgresql_is_running():
    # init script always return true (9.1), add extra check to make it useful
    status, output = commands.getstatusoutput("invoke-rc.d postgresql status")
    if status != 0:
        return False
    # e.g. output: "Running clusters: 9.1/main"
    vc = "%s/%s" % (config_data["version"], config_data["cluster_name"])
    return vc in output.decode('utf8').split()


def postgresql_stop():
    status, output = commands.getstatusoutput("invoke-rc.d postgresql stop")
    if status != 0:
        return False
    return not postgresql_is_running()


def postgresql_start():
    status, output = commands.getstatusoutput("invoke-rc.d postgresql start")
    if status != 0:
        juju_log(MSG_CRITICAL, output)
        return False
    return postgresql_is_running()


def postgresql_restart():
    if postgresql_is_running():
        status, output = \
            commands.getstatusoutput("invoke-rc.d postgresql restart")
        if status != 0:
            return False
    else:
        postgresql_start()
    return postgresql_is_running()


def postgresql_reload():
    # reload returns a reliable exit status
    status, output = commands.getstatusoutput("invoke-rc.d postgresql reload")
    return (status == 0)


#------------------------------------------------------------------------------
# config_get:  Returns a dictionary containing all of the config information
#              Optional parameter: scope
#              scope: limits the scope of the returned configuration to the
#                     desired config item.
#------------------------------------------------------------------------------
def config_get(scope=None):
    try:
        config_cmd_line = ['config-get']
        if scope is not None:
            config_cmd_line.append(scope)
        config_cmd_line.append('--format=json')
        config_data = json.loads(subprocess.check_output(config_cmd_line))
    except:
        config_data = None
    finally:
        return(config_data)


#------------------------------------------------------------------------------
# get_service_port:   Convenience function that scans the existing postgresql
#                     configuration file and returns a the existing port
#                     being used.  This is necessary to know which port(s)
#                     to open and close when exposing/unexposing a service
#------------------------------------------------------------------------------
def get_service_port(postgresql_config):
    postgresql_config = load_postgresql_config(postgresql_config)
    if postgresql_config is None:
        return(None)
    port = re.search("port.*=(.*)", postgresql_config).group(1).strip()
    try:
        return int(port)
    except:
        return None


#------------------------------------------------------------------------------
# relation_json:  Returns json-formatted relation data
#                Optional parameters: scope, relation_id
#                scope:        limits the scope of the returned data to the
#                              desired item.
#                unit_name:    limits the data ( and optionally the scope )
#                              to the specified unit
#                relation_id:  specify relation id for out of context usage.
#------------------------------------------------------------------------------
def relation_json(scope=None, unit_name=None, relation_id=None):
    try:
        relation_cmd_line = ['relation-get', '--format=json']
        if relation_id is not None:
            relation_cmd_line.extend(('-r', relation_id))
        if scope is not None:
            relation_cmd_line.append(scope)
        else:
            relation_cmd_line.append('-')
        relation_cmd_line.append(unit_name)
        relation_data = run(" ".join(relation_cmd_line), exit_on_error=False)
    except:
        relation_data = None
    finally:
        return(relation_data)


#------------------------------------------------------------------------------
# relation_get:  Returns a dictionary containing the relation information
#                Optional parameters: scope, relation_id
#                scope:        limits the scope of the returned data to the
#                              desired item.
#                unit_name:    limits the data ( and optionally the scope )
#                              to the specified unit
#------------------------------------------------------------------------------
def relation_get(scope=None, unit_name=None):
    try:
        relation_cmd_line = ['relation-get', '--format=json']
        if scope is not None:
            relation_cmd_line.append(scope)
        else:
            relation_cmd_line.append('-')
        if unit_name is not None:
            relation_cmd_line.append(unit_name)
        relation_data = json.loads(subprocess.check_output(relation_cmd_line))
    except:
        relation_data = None
    finally:
        return(relation_data)


def relation_set(keyvalues, relation_id=None):
    args = []
    if relation_id:
        args.extend(['-r', relation_id])
    args.extend(["{}='{}'".format(k, v) for k, v in keyvalues.items()])
    run("relation-set {}".format(' '.join(args)))


#------------------------------------------------------------------------------
# relation_ids:  Returns a list of relation ids
#                optional parameters: relation_type
#                relation_type: return relations only of this type
#------------------------------------------------------------------------------
def relation_ids(relation_types=('db',)):
    # accept strings or iterators
    if isinstance(relation_types, basestring):
        reltypes = [relation_types, ]
    else:
        reltypes = relation_types
    relids = []
    for reltype in reltypes:
        relid_cmd_line = ['relation-ids', '--format=json', reltype]
        relids.extend(json.loads(subprocess.check_output(relid_cmd_line)))
    return relids


#------------------------------------------------------------------------------
# relation_get_all:  Returns a dictionary containing the relation information
#                optional parameters: relation_type
#                relation_type: limits the scope of the returned data to the
#                               desired item.
#------------------------------------------------------------------------------
def relation_get_all(*args, **kwargs):
    relation_data = []
    try:
        relids = relation_ids(*args, **kwargs)
        for relid in relids:
            units_cmd_line = ['relation-list', '--format=json', '-r', relid]
            units = json.loads(subprocess.check_output(units_cmd_line))
            for unit in units:
                unit_data = \
                    json.loads(relation_json(relation_id=relid,
                        unit_name=unit))
                for key in unit_data:
                    if key.endswith('-list'):
                        unit_data[key] = unit_data[key].split()
                unit_data['relation-id'] = relid
                unit_data['unit'] = unit
            relation_data.append(unit_data)
    except Exception, e:
        subprocess.call(['juju-log', str(e)])
        relation_data = []
    finally:
        return(relation_data)


#------------------------------------------------------------------------------
# apt_get_install( package ):  Installs a package
#------------------------------------------------------------------------------
def apt_get_install(packages=None):
    if packages is None:
        return(False)
    cmd_line = ['apt-get', '-y', 'install', '-qq']
    cmd_line.append(packages)
    return(subprocess.call(cmd_line))


#------------------------------------------------------------------------------
# create_postgresql_config:   Creates the postgresql.conf file
#------------------------------------------------------------------------------
def create_postgresql_config(postgresql_config):
    if config_data["performance_tuning"] == "auto":
        # Taken from:
        # http://wiki.postgresql.org/wiki/Tuning_Your_PostgreSQL_Server
        # num_cpus is not being used ... commenting it out ... negronjl
        #num_cpus = run("cat /proc/cpuinfo | grep processor | wc -l")
        total_ram = run("free -m | grep Mem | awk '{print $2}'")
        config_data["effective_cache_size"] = \
            "%sMB" % (int(int(total_ram) * 0.75),)
        if total_ram > 1023:
            config_data["shared_buffers"] = \
                "%sMB" % (int(int(total_ram) * 0.25),)
        else:
            config_data["shared_buffers"] = \
                "%sMB" % (int(int(total_ram) * 0.15),)
        # XXX: This is very messy - should probably be a subordinate charm
        # file overlaps with __builtin__.file ... renaming to conf_file
        # negronjl
        conf_file = open("/etc/sysctl.d/50-postgresql.conf", "w")
        conf_file.write("kernel.sem = 250 32000 100 1024\n")
        conf_file.write("kernel.shmall = %s\n" %
            ((int(total_ram) * 1024 * 1024) + 1024),)
        conf_file.write("kernel.shmmax = %s\n" %
            ((int(total_ram) * 1024 * 1024) + 1024),)
        conf_file.close()
        run("sysctl -p /etc/sysctl.d/50-postgresql.conf")

    # If we are replicating, some settings may need to be overridden to
    # certain minimum levels.
    TODO("Relation check always returns True - fix")
    num_slaves = len(relation_ids(relation_types=replication_relation_types))
    modified_config_data = dict(config_data)
    if num_slaves > 0:
        juju_log(
            MSG_DEBUG, 'Replicated. Enforcing minimal replication settings')
        modified_config_data['hot_standby'] = 'on'
        modified_config_data['wal_level'] = 'hot_standby'
        if modified_config_data['archive_mode'] is False:
            # If archive_mode was not configured, we need to override it
            # to keep repmgr happy despite the fact it doesn't really
            # need it. We also need set a noop archive_command. If
            # archive_mode was already set, we don't mess with the
            # archive_command setting.
            modified_config_data['archive_mode'] = 'True'
            if not config_data['archive_command']:
                modified_config_data['archive_command'] = 'cd .'
        if config_data['max_wal_senders']:
            modified_config_data['max_wal_senders'] = max(
                config_data['max_wal_senders'], num_slaves)
        else:
            modified_config_data['max_wal_senders'] = num_slaves
        modified_config_data['wal_keep_segments'] = max(
            modified_config_data['wal_keep_segments'],
            modified_config_data['replicated_wal_keep_segments'])

    # Send config data to the template
    # Return it as pg_config
    pg_config = \
        Template(
            open("templates/postgresql.conf.tmpl").read()).render(
                modified_config_data)
    install_file(pg_config, postgresql_config)


#------------------------------------------------------------------------------
# create_postgresql_ident:  Creates the pg_ident.conf file
#------------------------------------------------------------------------------
def create_postgresql_ident(postgresql_ident):
    ident_data = {}
    pg_ident_template = \
        Template(
            open("templates/pg_ident.conf.tmpl").read()).render(ident_data)
    with open(postgresql_ident, 'w') as ident_file:
        ident_file.write(str(pg_ident_template))


#------------------------------------------------------------------------------
# generate_postgresql_hba:  Creates the pg_hba.conf file
#------------------------------------------------------------------------------
def generate_postgresql_hba(postgresql_hba, do_reload=True):
    relation_data = relation_get_all(relation_types=['db', 'db-admin'])
    config_change_command = config_data["config_change_command"]
    for relation in relation_data:
        relation_id = relation['relation-id']
        if relation_id.startswith('db-admin:'):
            relation['user'] = 'all'
            relation['database'] = 'all'
        elif relation_id.startswith('db:'):
            relation['user'] = user_name(relation['relation-id'],
                                         relation['unit'])
            relation['schema_user'] = user_name(relation['relation-id'],
                                                relation['unit'],
                                                schema=True)
        else:
            raise RuntimeError(
                'Unknown relation type {}'.format(repr(relation_id)))

    # Replication connections. Each unit needs to be able to connect to
    # every other unit's repmgr database and the magic replication
    # database. It also needs to be able to connect to its own repmgr
    # database.
    replication_relations = relation_get_all(
        relation_types=replication_relation_types)
    for relation in replication_relations:
        remote_replication = {
            'database': 'replication,repmgr', 'user': 'repmgr',
            'private-address': relation['private-address'],
            'relation-id': relation['relation-id'],
            'unit': relation['private-address'],
            }
        relation_data.append(remote_replication)
    if replication_relations:
        local_repmgr = {
            'database': 'repmgr', 'user': 'repmgr',
            'private-address': get_unit_host(),
            'relation-id': relation['relation-id'],
            'unit': get_unit_host(),
            }
        relation_data.append(local_repmgr)

    juju_log(MSG_INFO, str(relation_data))
    pg_hba_template = Template(
        open("templates/pg_hba.conf.tmpl").read()).render(
            access_list=relation_data)
    with open(postgresql_hba, 'w') as hba_file:
        hba_file.write(str(pg_hba_template))
    if do_reload:
        if config_change_command in ["reload", "restart"]:
            subprocess.call(['invoke-rc.d', 'postgresql',
                config_data["config_change_command"]])


#------------------------------------------------------------------------------
# install_postgresql_crontab:  Creates the postgresql crontab file
#------------------------------------------------------------------------------
def install_postgresql_crontab(postgresql_ident):
    crontab_data = {
        'backup_schedule': config_data["backup_schedule"],
        'scripts_dir': postgresql_scripts_dir,
        'backup_days': config_data["backup_retention_count"],
    }
    from jinja2 import Template
    crontab_template = Template(
        open("templates/postgres.cron.tmpl").read()).render(crontab_data)
    install_file(str(crontab_template), "/etc/cron.d/postgres", mode=0644)


#------------------------------------------------------------------------------
# load_postgresql_config:  Convenience function that loads (as a string) the
#                          current postgresql configuration file.
#                          Returns a string containing the postgresql config or
#                          None
#------------------------------------------------------------------------------
def load_postgresql_config(postgresql_config):
    if os.path.isfile(postgresql_config):
        return(open(postgresql_config).read())
    else:
        return(None)


#------------------------------------------------------------------------------
# open_port:  Convenience function to open a port in juju to
#             expose a service
#------------------------------------------------------------------------------
def open_port(port=None, protocol="TCP"):
    if port is None:
        return(None)
    return(subprocess.call(['/usr/bin/open-port', "%d/%s" %
        (int(port), protocol)]))


#------------------------------------------------------------------------------
# close_port:  Convenience function to close a port in juju to
#              unexpose a service
#------------------------------------------------------------------------------
def close_port(port=None, protocol="TCP"):
    if port is None:
        return(None)
    return(subprocess.call(['/usr/bin/close-port', "%d/%s" %
        (int(port), protocol)]))


#------------------------------------------------------------------------------
# update_service_ports:  Convenience function that evaluate the old and new
#                        service ports to decide which ports need to be
#                        opened and which to close
#------------------------------------------------------------------------------
def update_service_port(old_service_port=None, new_service_port=None):
    if old_service_port is None or new_service_port is None:
        return(None)
    if new_service_port != old_service_port:
        close_port(old_service_port)
        open_port(new_service_port)


#------------------------------------------------------------------------------
# pwgen:  Generates a random password
#         pwd_length:  Defines the length of the password to generate
#                      default: 20
#------------------------------------------------------------------------------
def pwgen(pwd_length=None):
    if pwd_length is None:
        pwd_length = random.choice(range(20, 30))
    alphanumeric_chars = [l for l in (string.letters + string.digits)
        if l not in 'Iil0oO1']
    random_chars = [random.choice(alphanumeric_chars)
        for i in range(pwd_length)]
    return(''.join(random_chars))


def set_password(user, password):
    if not os.path.isdir("passwords"):
        os.makedirs("passwords")
    old_umask = os.umask(0o077)
    try:
        with open("passwords/%s" % user, "w") as pwfile:
            pwfile.write(password)
    finally:
        os.umask(old_umask)


def get_password(user):
    try:
        with open("passwords/%s" % user) as pwfile:
            return pwfile.read()
    except IOError:
        return None


def db_cursor(autocommit=False, db='template1'):
    conn = psycopg2.connect("dbname={} user=postgres".format(db))
    conn.autocommit = autocommit
    return conn.cursor()


def run_sql_as_postgres(sql, *parameters):
    cur = db_cursor(autocommit=True)
    try:
        cur.execute(sql, parameters)
        return cur.statusmessage
    except psycopg2.ProgrammingError:
        juju_log(MSG_CRITICAL, sql)
        raise


def run_select_as_postgres(sql, *parameters):
    cur = db_cursor()
    cur.execute(sql, parameters)
    return (cur.rowcount, cur.fetchall())


#------------------------------------------------------------------------------
# Core logic for permanent storage changes:
# NOTE the only 2 "True" return points:
#   1) symlink already pointing to existing storage (no-op)
#   2) new storage properly initialized:
#     - volume: initialized if not already (fdisk, mkfs),
#       mounts it to e.g.:  /srv/juju/vol-000012345
#     - if fresh new storage dir: rsync existing data
#     - manipulate /var/lib/postgresql/VERSION/CLUSTER symlink
#------------------------------------------------------------------------------
def config_changed_volume_apply():
    data_directory_path = postgresql_cluster_dir
    assert(data_directory_path)
    volid = volume_get_volume_id()
    if volid:
        if volume_is_permanent(volid):
            if not volume_init_and_mount(volid):
                juju_log(MSG_ERROR, "volume_init_and_mount failed, " +
                     "not applying changes")
                return False

        if not os.path.exists(data_directory_path):
            juju_log(MSG_CRITICAL, ("postgresql data dir = %s not found, " +
                     "not applying changes.") % data_directory_path)
            return False

        mount_point = volume_mount_point_from_volid(volid)
        new_pg_dir = os.path.join(mount_point, "postgresql")
        new_pg_version_cluster_dir = os.path.join(new_pg_dir,
            config_data["version"], config_data["cluster_name"])
        if not mount_point:
            juju_log(MSG_ERROR, "invalid mount point from volid = \"%s\", " +
                     "not applying changes." % mount_point)
            return False

        if (os.path.islink(data_directory_path) and
            os.readlink(data_directory_path) == new_pg_version_cluster_dir and
            os.path.isdir(new_pg_version_cluster_dir)):
            juju_log(MSG_INFO,
                "NOTICE: postgresql data dir '%s' already points to '%s', \
                skipping storage changes." %
                (data_directory_path, new_pg_version_cluster_dir))
            juju_log(MSG_INFO,
                "existing-symlink: to fix/avoid UID changes from previous "
                "units, doing: chown -R postgres:postgres %s" % new_pg_dir)
            run("chown -R postgres:postgres %s" % new_pg_dir)
            return True

        # Create a directory structure below "new" mount_point, as e.g.:
        #   /srv/juju/vol-000012345/postgresql/9.1/main  , which "mimics":
        #   /var/lib/postgresql/9.1/main
        curr_dir_stat = os.stat(data_directory_path)
        for new_dir in [new_pg_dir,
                    os.path.join(new_pg_dir, config_data["version"]),
                    new_pg_version_cluster_dir]:
            if not os.path.isdir(new_dir):
                juju_log(MSG_INFO, "mkdir %s" % new_dir)
                os.mkdir(new_dir)
                # copy permissions from current data_directory_path
                os.chown(new_dir, curr_dir_stat.st_uid, curr_dir_stat.st_gid)
                os.chmod(new_dir, curr_dir_stat.st_mode)
        # Carefully build this symlink, e.g.:
        # /var/lib/postgresql/9.1/main ->
        # /srv/juju/vol-000012345/postgresql/9.1/main
        # but keep previous "main/"  directory, by renaming it to
        # main-$TIMESTAMP
        if not postgresql_stop():
            juju_log(MSG_ERROR,
                "postgresql_stop() returned False - can't migrate data.")
            return False
        if not os.path.exists(os.path.join(new_pg_version_cluster_dir,
            "PG_VERSION")):
            juju_log(MSG_WARNING, "migrating PG data %s/ -> %s/" % (
                     data_directory_path, new_pg_version_cluster_dir))
            # void copying PID file to perm storage (shouldn't be any...)
            command = "rsync -a --exclude postmaster.pid %s/ %s/" % \
                (data_directory_path, new_pg_version_cluster_dir)
            juju_log(MSG_INFO, "run: %s" % command)
            #output = run(command)
            run(command)
        try:
            os.rename(data_directory_path, "%s-%d" % (
                          data_directory_path, int(time.time())))
            juju_log(MSG_INFO, "NOTICE: symlinking %s -> %s" %
                (new_pg_version_cluster_dir, data_directory_path))
            os.symlink(new_pg_version_cluster_dir, data_directory_path)
            juju_log(MSG_INFO,
                "after-symlink: to fix/avoid UID changes from previous "
                "units, doing: chown -R postgres:postgres %s" % new_pg_dir)
            run("chown -R postgres:postgres %s" % new_pg_dir)
            return True
        except OSError:
            juju_log(MSG_CRITICAL, "failed to symlink \"%s\" -> \"%s\"" % (
                          data_directory_path, mount_point))
            return False
    else:
        juju_log(MSG_ERROR, "ERROR: Invalid volume storage configuration, " +
                 "not applying changes")
    return False


###############################################################################
# Hook functions
###############################################################################
def config_changed(postgresql_config):
    config_change_command = config_data["config_change_command"]
    # Trigger volume initialization logic for permanent storage
    volid = volume_get_volume_id()
    if not volid:
        ## Invalid configuration (whether ephemeral, or permanent)
        disable_service_start("postgresql")
        postgresql_stop()
        mounts = volume_get_all_mounted()
        if mounts:
            juju_log(MSG_INFO, "FYI current mounted volumes: %s" % mounts)
        juju_log(MSG_ERROR,
            "Disabled and stopped postgresql service, \
            because of broken volume configuration - check \
            'volume-ephermeral-storage' and 'volume-map'")
        sys.exit(1)

    if volume_is_permanent(volid):
        ## config_changed_volume_apply will stop the service if it founds
        ## it necessary, ie: new volume setup
        if config_changed_volume_apply():
            enable_service_start("postgresql")
            config_change_command = "restart"
        else:
            disable_service_start("postgresql")
            postgresql_stop()
            mounts = volume_get_all_mounted()
            if mounts:
                juju_log(MSG_INFO, "FYI current mounted volumes: %s" % mounts)
            juju_log(MSG_ERROR,
                "Disabled and stopped postgresql service \
                (config_changed_volume_apply failure)")
            sys.exit(1)
    current_service_port = get_service_port(postgresql_config)
    create_postgresql_config(postgresql_config)
    generate_postgresql_hba(postgresql_hba, do_reload=False)
    create_postgresql_ident(postgresql_ident)
    updated_service_port = config_data["listen_port"]
    update_service_port(current_service_port, updated_service_port)
    update_nrpe_checks()
    juju_log(MSG_INFO,
        "about reconfigure service with config_change_command = '%s'" %
        config_change_command)
    if config_change_command == "reload":
        return postgresql_reload()
    elif config_change_command == "restart":
        return postgresql_restart()
    juju_log(MSG_ERROR, "invalid config_change_command = '%s'" %
        config_change_command)
    return False


def token_sql_safe(value):
    # Only allow alphanumeric + underscore in database identifiers
    if re.search('[^A-Za-z0-9_]', value):
        return False
    return True


def install(run_pre=True):
    if run_pre:
        for f in glob.glob('exec.d/*/charm-pre-install'):
            if os.path.isfile(f) and os.access(f, os.X_OK):
                subprocess.check_call(['sh', '-c', f])
    for package in ["postgresql", "pwgen", "python-jinja2", "syslinux",
        "python-psycopg2",
        "postgresql-%s-debversion" % config_data["version"]]:
        apt_get_install(package)
    from jinja2 import Template
    install_dir(postgresql_backups_dir, owner="postgres", mode=0755)
    install_dir(postgresql_scripts_dir, owner="postgres", mode=0755)
    install_dir(postgresql_logs_dir, owner="postgres", mode=0755)
    paths = {
        'base_dir': postgresql_data_dir,
        'backup_dir': postgresql_backups_dir,
        'scripts_dir': postgresql_scripts_dir,
        'logs_dir': postgresql_logs_dir,
    }
    dump_script = Template(
        open("templates/dump-pg-db.tmpl").read()).render(paths)
    backup_job = Template(
        open("templates/pg_backup_job.tmpl").read()).render(paths)
    install_file(dump_script, '{}/dump-pg-db'.format(postgresql_scripts_dir),
        mode=0755)
    install_file(backup_job, '{}/pg_backup_job'.format(postgresql_scripts_dir),
        mode=0755)
    install_postgresql_crontab(postgresql_crontab)
    open_port(5432)


def user_name(relid, remote_unit, admin=False, schema=False):
    def sanitize(s):
        s = s.replace(':', '_')
        s = s.replace('-', '_')
        s = s.replace('/', '_')
        s = s.replace('"', '_')
        s = s.replace("'", '_')
        return s
    components = [sanitize(relid), sanitize(remote_unit)]
    if admin:
        components.append("admin")
    elif schema:
        components.append("schema")
    return "_".join(components)


def database_names(admin=False):
    omit_tables = ['template0', 'template1']
    sql = \
    "SELECT datname FROM pg_database WHERE datname NOT IN (" + \
    ",".join(["%s"] * len(omit_tables)) + ")"
    return [t for (t,) in run_select_as_postgres(sql, *omit_tables)[1]]


def user_exists(user):
    sql = "SELECT rolname FROM pg_roles WHERE rolname = %s"
    if run_select_as_postgres(sql, user)[0] > 0:
        return True
    else:
        return False


def create_user(user, admin=False, replication=False):
    password = get_password(user)
    if password is None:
        password = pwgen()
        set_password(user, password)
    if user_exists(user):
        action = ["ALTER ROLE"]
    else:
        action = ["CREATE ROLE"]
    action.append('"{}"'.format(user))
    action.append('WITH LOGIN')
    if admin:
        action.append('SUPERUSER')
    else:
        action.append('NOSUPERUSER')
    if replication:
        action.append('REPLICATION')
    else:
        action.append('NOREPLICATION')
    action.append('PASSWORD %s')
    sql = ' '.join(action)
    run_sql_as_postgres(sql, password)
    return password


def grant_roles(user, roles):
    # Delete previous roles
    sql = ("DELETE FROM pg_auth_members WHERE member IN ("
           "SELECT oid FROM pg_roles WHERE rolname = %s)")
    run_sql_as_postgres(sql, user)

    for role in roles:
        ensure_role(role)
        sql = "GRANT {} to {}".format(role, user)
        run_sql_as_postgres(sql)


def ensure_role(role):
    sql = "SELECT oid FROM pg_roles WHERE rolname = %s"
    if run_select_as_postgres(sql, role)[0] != 0:
        # role already exists
        pass
    else:
        sql = "CREATE ROLE %s INHERIT"
        run_sql_as_postgres(sql, role)
        sql = "ALTER ROLE %s NOLOGIN"
        run_sql_as_postgres(sql, role)


def ensure_database(user, schema_user, database):
    sql = "SELECT datname FROM pg_database WHERE datname = %s"
    if run_select_as_postgres(sql, database)[0] != 0:
        # DB already exists
        pass
    else:
        sql = "CREATE DATABASE {}".format(database)
        run_sql_as_postgres(sql)
    sql = "GRANT ALL PRIVILEGES ON DATABASE {} TO {}".format(database,
        schema_user)
    run_sql_as_postgres(sql)
    sql = "GRANT CONNECT ON DATABASE {} TO {}".format(database, user)
    run_sql_as_postgres(sql)


def get_relation_host():
    remote_host = run("relation-get ip")
    if not remote_host:
        # remote unit $JUJU_REMOTE_UNIT uses deprecated 'ip=' component of
        # interface.
        remote_host = run("relation-get private-address")
    return remote_host


def get_unit_host():
    this_host = run("unit-get private-address")
    return this_host.strip()


def db_relation_joined_changed(user, database, roles):
    if not user_exists(user):
        password = create_user(user)
        run("relation-set user='%s' password='%s'" % (user, password))
    grant_roles(user, roles)
    schema_user = "{}_schema".format(user)
    if not user_exists(schema_user):
        schema_password = create_user(schema_user)
        run("relation-set schema_user='%s' schema_password='%s'" % (
            schema_user, schema_password))
    ensure_database(user, schema_user, database)
    config_data = config_get()
    host = get_unit_host()
    run("relation-set host='%s' database='%s' port='%s'" % (
        host, database, config_data["listen_port"]))
    generate_postgresql_hba(postgresql_hba)


def db_admin_relation_joined_changed(user, database='all'):
    if not user_exists(user):
        password = create_user(user, admin=True)
        run("relation-set user='%s' password='%s'" % (user, password))
    host = get_unit_host()
    config_data = config_get()
    run("relation-set host='%s' port='%s'" % (
        host, config_data["listen_port"]))
    generate_postgresql_hba(postgresql_hba)


def db_relation_broken(user, database):
    sql = "REVOKE ALL PRIVILEGES ON {} FROM {}".format(database, user)
    run_sql_as_postgres(sql)
    sql = "REVOKE ALL PRIVILEGES ON {} FROM {}_schema".format(database, user)
    run_sql_as_postgres(sql)


def db_admin_relation_broken(user):
    sql = "ALTER USER {} NOSUPERUSER".format(user)
    run_sql_as_postgres(sql)
    generate_postgresql_hba(postgresql_hba)


def TODO(msg):
    juju_log(MSG_WARNING, 'TODO> %s' % msg)


def install_repmgr():
    '''Install the repmgr package if it isn't already.'''
    TODO('Get repmgr packages in official repository')
    extra_repos = config_get('extra_archives')
    if extra_repos:
        for repo in extra_repos.split():
            run("add-apt-repository --yes '{}'".format(repo))
        run('apt-get update')
    apt_get_install('repmgr')
    apt_get_install('postgresql-9.1-repmgr')


def ensure_local_ssh():
    """Generate SSH keys for postgres user.

    The public key is stored in public_ssh_key on the relation.

    Bidirectional SSH access is required by repmgr.
    """
    comment = 'repmgr key for {}'.format(os.environ['JUJU_RELATION_ID'])
    if not os.path.isdir(postgres_ssh_dir):
        install_dir(postgres_ssh_dir, "postgres", "postgres", 0700)
    if not os.path.exists(postgres_ssh_private_key):
        run("sudo -u postgres -H ssh-keygen -q -t rsa -C '{}' -N '' "
            "-f '{}'".format(comment, postgres_ssh_private_key))
    public_key = open(postgres_ssh_public_key, 'r').read().strip()
    host_key = open('/etc/ssh/ssh_host_ecdsa_key.pub').read().strip()
    run("relation-set public_ssh_key='{}' ssh_host_key='{}'".format(
        public_key, host_key))


def authorize_remote_ssh():
    """Add the remote's public SSH key to authorized_keys."""
    relation = relation_get()
    if not relation.has_key('public_ssh_key'):
        # No public key. The -changed hook was invoked before the remote
        # -joined hook completed. We are fine though, as this -changed
        # hook will be reinvoked.
        juju_log(
            MSG_DEBUG,'Public SSH key for {} not found'.format(
                os.environ['JUJU_REMOTE_UNIT']))
            relation_set(dict(ping='{} {}'.format(
                os.environ['JUJU_REMOTE_UNIT'], time.time())))
        return False
    juju_log(
        MSG_INFO, 'Authorizing SSH access from {} to {}'.format(
            os.environ['JUJU_REMOTE_UNIT'], os.environ['JUJU_UNIT_NAME']))

    # Store a copy of the remote SSH public key, so
    # generate_ssh_authorized_keys() will find it, even
    # when run from a -broken hook.
    if not os.path.isdir("ssh_keys"):
        install_dir("ssh_keys", 0o700)
    install_file(
        relation['public_ssh_key'],
        os.path.join("ssh_keys", os.environ['JUJU_RELATION_ID']))

    # Regenerate the authorized_keys file.
    generate_ssh_authorized_keys()

    host_key = '{} {}'.format(
        relation['private-address'], relation['ssh_host_key'],
        os.environ['JUJU_REMOTE_UNIT'])
    if os.path.exists(postgres_ssh_known_hosts):
        # Suck in all lines, except those that will conflict with the
        # newly distributed host key. Perhaps an IP address has been
        # reused?
        known_hosts = [
            host for host in open(postgres_ssh_known_hosts).readlines()
            if not host.startswith(relation['private-address'] + ' ')]
    else:
        known_hosts = []
    known_hosts.append(host_key)
    install_file(
        '\n'.join(known_hosts), postgres_ssh_known_hosts,
        owner="postgres", group="postgres", mode=0o644)
    return True


def generate_ssh_authorized_keys():
    """Generate the SSH authorized_keys file.

    This will overwrite the existing authorized_keys file, so you can't
    run multiple copies of this charm on the same server.
    """
    # Generate a list of keys that are no longer authorized.
    all_relation_ids = os.listdir('ssh_keys')
    valid_relation_ids = relation_ids(replication_relation_types)

    wanted_keys = []
    for relid in valid_relation_ids:
        key_path = os.path.join('ssh_keys', relid)
        # Look before you leap, as we might be in the process of adding
        # multiple relationships at the same time.
        if os.path.exists(key_path):
            wanted_keys.append(open(key_path).read())

    install_file(
        '\n'.join(wanted_keys), postgres_ssh_authorized_keys,
        owner="postgres", group="postgres", mode=0o400)

    # Cleanup
    for relid in all_relation_ids:
        if relid not in valid_relation_ids:
            os.unlink(os.path.join('ssh_keys', relid))


def generate_repmgr_config(node_id, password):
    """Regenerate the repmgr config file.

    node_id is an integer, and must be a unique in the cluster.
    """
    params = {
        'node_id': node_id,
        'node_name': os.environ['JUJU_UNIT_NAME'],
        'host': get_unit_host(),
        'user': 'repmgr',
        }
    config = Template(
        open("templates/repmgr.conf.tmpl").read()).render(params)
    install_file(
        config, repmgr_config, owner="postgres", group="postgres", mode=0o400)

    pgpass = "*:*:*:repmgr:{}".format(password)
    install_file(
        pgpass, postgres_pgpass,
        owner="postgres", group="postgres", mode=0o400)


def run_repmgr(cmd, exit_on_error=True):
    full_command = "sudo -u postgres repmgr -f '{}' {}".format(
        repmgr_config, cmd)
    juju_log(MSG_DEBUG, full_command)
    try:
        output = subprocess.check_output(
            full_command, stderr=subprocess.STDOUT, shell=True)
        returncode = 0
    except subprocess.CalledProcessError, x:
        if exit_on_error:
            juju_log(MSG_ERROR, x.output)
            raise SystemExit(x.returncode)
        returncode = x.returncode
        output = x.output

    ## Too noisy for slow logging. Enable if clone becomes less noisy.
    ## juju_log(MSG_DEBUG, output)
    return returncode, output


def drop_database(dbname, warn=True):
    timeout = 120
    now = time.time()
    while True:
        try:
            db_cursor(autocommit=True).execute(
                'DROP DATABASE IF EXISTS "{}"'.format(dbname))
        except psycopg2.DatabaseError:
            if time.time() > now + timeout:
                if warn:
                    juju_log(
                        MSG_WARN, "Unable to drop database {}".format(dbname))
                else:
                    raise
            time.sleep(0.5)
        else:
            break


def get_repmgr_node_id():
    '''Return the repmgr node id, or None if it is not known.'''
    # If the repmgr schema has not yet been setup, then nobody has a
    # node id.
    cur = db_cursor(autocommit=True, db='repmgr')
    cur.execute("""
        SELECT TRUE FROM information_schema.tables
        WHERE table_schema='repmgr_juju' AND table_name='repl_nodes'
        """)
    if cur.fetchone() is None:
        return None

    cur.execute("""
        SELECT id from repmgr_juju.repl_nodes
        WHERE name=%s
        """, (os.environ['JUJU_UNIT_NAME'],))
    result = cur.fetchone()
    if result is not None:
        return result[0]
    return None


def get_next_repmgr_node_id():
    cur = db_cursor(autocommit=True, db='repmgr')
    # We use a sequence for generating a unique id per node, as required
    # by repmgr. Create it if necessary.
    # TODO: Using a sequence creates a race condition where a new id is
    # allocated on the master and we failover before that information
    # is replicated. This is nearly impossible to hit. We could simply
    # bump the sequence by 100 after every failover.
    cur.execute('''
        SELECT TRUE FROM information_schema.sequences
        WHERE sequence_catalog = 'repmgr' AND sequence_schema='public'
            AND sequence_name = 'juju_node_id'
        ''')
    if cur.fetchone() is None:
        cur.execute('CREATE SEQUENCE juju_node_id')
    cur.execute("SELECT nextval('juju_node_id')")
    return cur.fetchone()[0]


def repmgr_gc():
    """Remove old nodes from the repmgr database, tear down if no slaves"""
    wanted_units = []
    for relid in relation_ids(replication_relation_types):
        units = json.loads(subprocess.check_output([
            'relation-list', '--format=json', '-r', relid]))
        wanted_units.extend(units)

    if len(wanted_units) == 0:
        # No relationships. Trash repmgr database.
        if os.path.exists(repmgr_config):
            juju_log(MSG_INFO, "No longer replicated. Dropping repmgr.")
            os.unlink(repmgr_config)
        if os.path.exists(postgres_pgpass):
            os.unlink(postgres_pgpass)
        drop_database('repmgr')

    elif is_master():
        # At least one other slave.
        wanted_node_ids.append(str(get_repmgr_node_id()))
        cur = db_cursor(autocommit=True, db='repmgr')
        cur.execute(
            "DELETE FROM repmgr_juju.repl_nodes WHERE name NOT IN %s",
            wanted_node_ids)


def is_master():
    '''True if we are, or should be, the master.

    Return True if we are the active master, or if neither myself nor
    the remote unit is and I win an election.
    '''
    # Do I know what I am?
    local_role = relation_get('role', os.environ['JUJU_UNIT_NAME'])
    if local_role == 'master':
        return True
    elif local_role == 'hot standby':
        return False
    elif local_role:
        raise AssertionError("Unknown replication role {}".format(local_role))

    # I don't know what I am. Work it out.
    TODO(
        "Detect incompatible relationships, such as cascading or mixed "
        "peer & master/slave relationships")
    relation_type = os.environ['JUJU_RELATION']
    if relation_type == 'master':
        # I'm explicitly the master in a master/slave relationship.
        relation_set(dict(role='master'))
        return True

    elif relation_type == 'slave':
        # I'm explicitly the slave in a master/slave relationship.
        relation_set(dict(role='hot standby'))
        return False

    elif relation_type and relation_type != 'replication':
        raise AssertionError('Unknown relation type {}'.format(relation_type))

    # If there are any other peers claiming to be the master, then I am
    # not the master.
    units = json.loads(
        subprocess.check_output(['relation-list', '--format=json']))
    for unit in units:
        remote_role = relation_get('role', unit)
        if remote_role == 'master':
            relation_set(dict(role="hot standby"))
            return False

    # It must be election time. The unit with the lowest numeric
    # component in its unit name gets to be the master.
    remote_nums = sorted(int(unit.split('/', 1)[1]) for unit in units)
    my_num = int(os.environ['JUJU_UNIT_NAME'].split('/', 1)[1])
    if my_num < remote_nums[0]:
        relation_set(dict(role="master"))
        return True
    else:
        relation_set(dict(role="hot standby"))
        return False


def replication_relation_joined():
    juju_log(MSG_INFO, "Hook replication_relation_joined()")
    ensure_local_ssh()
    authorize_remote_ssh()
    relation_set(dict(state='standalone'))
    install_repmgr()

    # Adding nodes requires changing configuration values like
    # max_wal_senders.
    config_changed(postgresql_config)
    TODO("Should not need to force restart after config change")
    postgresql_restart()

    if is_master():
        # The user repmgr connects as for both replication and
        # administration.
        repmgr_password = create_user('repmgr', admin=True, replication=True)
        ensure_database('repmgr', 'repmgr', 'repmgr')

        master_node_id = get_repmgr_node_id()
        if master_node_id is None:
            # Initial setup of the master. Register it with repmgr.
            master_node_id = get_next_repmgr_node_id()
            generate_repmgr_config(master_node_id, repmgr_password)
            run_repmgr('master register')

        # Publish required repmgr information to the slave, and save
        # our state.
        remote_node_id = get_next_repmgr_node_id()
        relation_set(dict(
            repmgr_node_id=remote_node_id, repmgr_password=repmgr_password))


def replication_relation_changed():
    juju_log(MSG_INFO, "Hook replication_relation_changed()")
    if authorize_remote_ssh():
        relation_set(dict(
            state='authorized'))
    else:
        juju_log(
            MSG_DEBUG,'Public SSH key for {} not yet available'.format(
                os.environ['JUJU_REMOTE_UNIT']))
        # Nothing else to do until we have authorization from the other end.
        return

    generate_postgresql_hba(postgresql_hba)

    relation = relation_get()

    remote_is_master = (relation_get('role') == 'master')
    remote_has_authorized = (relation['state'] == 'authorized')
    local_state = relation_get('state', os.environ['JUJU_UNIT_NAME'])

    TODO("state==authorized doesn't work for 2nd peer")
    if remote_is_master and remote_has_authorized:
        if local_state == 'authorized':
            # We are just joining replication, and have found a master.
            # Clone and follow it.
            generate_repmgr_config(
                relation['repmgr_node_id'], relation['repmgr_password'])
            juju_log(MSG_INFO, "Destroying existing cluster on slave")
            postgresql_stop()
            shutil.rmtree(postgresql_cluster_dir)
            try:
                run_repmgr(
                    '-D {} -d repmgr -p 5432 -U repmgr -R postgres '
                    'standby clone {}'.format(
                        postgresql_cluster_dir,
                        relation_get('private-address')))
            except subprocess.CalledProcessError:
                # We failed, and this cluster is broken. Rebuild a working
                # cluster so start/stop etc. works and we can retry hooks
                # again. Even assuming the charm is functioning correctly,
                # the clone may still fail due to eg. lack of disk space.
                shutil.rmtree(postgresql_cluster_dir)
                run('pg_createcluster 9.1 main')
                raise
            finally:
                postgresql_start()
            juju_log(MSG_INFO, "Cloned cluster")
            wait_for_db()
            run_repmgr('standby register')
            relation_set(dict(state='cloned'))
            juju_log(MSG_INFO, "Registered cluster with repmgr")

        elif local_state == 'cloned':
            TODO("Handle failover automatically")

        else:
            raise RuntimeError("Unknown state {}".format(local_state))


def wait_for_db(timeout=120):
    '''Wait until the db is fully up.'''
    start = time.time()
    while True:
        try:
            cur = db_cursor(autocommit=True)
        except psycopg2.DatabaseError:
            if time.time() > start + timeout:
                raise
        time.sleep(0.3)


def replication_relation_broken():
    repmgr_gc()
    config_changed(postgresql_config)
    generate_ssh_authorized_keys()

    if is_master():
        TODO("Failover if master unit is removed")

    else:
        # Restore the hot standby to a standalone configuration.
        if cluster_is_in_recovery():
            pg_ctl = os.path.join(postgresql_bin_dir, 'pg_ctl')
            run("sudo -u postgres {} promote -D '{}'".format(
                pg_ctl, postgresql_cluster_dir))
        if os.path.exists(repmgr_config):
            os.unlink(repmgr_config)
        if os.path.exists(postgres_pgpass):
            os.unlink(postgres_pgpass)

        # Once promotion has completed and the cluster is writable, drop the
        # repmgr database.
        timeout = 120
        start = time.time()
        while cluster_is_in_recovery():
            if time.time() > start + timeout:
                juju_log(MSG_ERROR, "Failed to promote slave to standalone")
                sys.exit(1)
            time.sleep(0.5)
        juju_log(MSG_INFO, "Slave promoted to standalone. Dropping repmgr db.")
        drop_database('repmgr')


def cluster_is_in_recovery():
    cur = db_cursor(autocommit=True)
    cur.execute("SELECT pg_is_in_recovery()")
    return cur.fetchone()[0]


def update_nrpe_checks():
    config_data = config_get()
    try:
        nagios_uid = getpwnam('nagios').pw_uid
        nagios_gid = getgrnam('nagios').gr_gid
    except:
        subprocess.call(['juju-log', "Nagios user not set up. Exiting."])
        return

    unit_name = os.environ['JUJU_UNIT_NAME'].replace('/', '-')
    nagios_hostname = "%s-%s" % (config_data['nagios_context'], unit_name)
    nagios_logdir = '/var/log/nagios'
    nrpe_service_file = \
        '/var/lib/nagios/export/service__{}_check_pgsql.cfg'.format(
            nagios_hostname)
    if not os.path.exists(nagios_logdir):
        os.mkdir(nagios_logdir)
        os.chown(nagios_logdir, nagios_uid, nagios_gid)
    for f in os.listdir('/var/lib/nagios/export/'):
        if re.search('.*check_pgsql.cfg', f):
            os.remove(os.path.join('/var/lib/nagios/export/', f))

    # --- exported service configuration file
    from jinja2 import Environment, FileSystemLoader
    template_env = Environment(
        loader=FileSystemLoader(os.path.join(os.environ['CHARM_DIR'],
        'templates')))
    templ_vars = {
        'nagios_hostname': nagios_hostname,
        'nagios_servicegroup': config_data['nagios_context'],
    }
    template = \
        template_env.get_template('nrpe_service.tmpl').render(templ_vars)
    with open(nrpe_service_file, 'w') as nrpe_service_config:
        nrpe_service_config.write(str(template))

    # --- nrpe configuration
    # pgsql service
    nrpe_check_file = '/etc/nagios/nrpe.d/check_pgsql.cfg'
    with open(nrpe_check_file, 'w') as nrpe_check_config:
        nrpe_check_config.write("# check pgsql\n")
        nrpe_check_config.write(
            "command[check_pgsql]=/usr/lib/nagios/plugins/check_pgsql -p {}"
            .format(config_data['listen_port']))
    # pgsql backups
    nrpe_check_file = '/etc/nagios/nrpe.d/check_pgsql_backups.cfg'
    backup_log = "%s/backups.log".format(postgresql_logs_dir)
    # XXX: these values _should_ be calculated from the backup schedule
    #      perhaps warn = backup_frequency * 1.5, crit = backup_frequency * 2
    warn_age = 172800
    crit_age = 194400
    with open(nrpe_check_file, 'w') as nrpe_check_config:
        nrpe_check_config.write("# check pgsql backups\n")
        nrpe_check_config.write(
            "command[check_pgsql_backups]=/usr/lib/nagios/plugins/\
check_file_age -w {} -c {} -f {}".format(warn_age, crit_age, backup_log))

    if os.path.isfile('/etc/init.d/nagios-nrpe-server'):
        subprocess.call(['service', 'nagios-nrpe-server', 'reload'])

###############################################################################
# Global variables
###############################################################################
config_data = config_get()
version = config_data['version']
cluster_name = config_data['cluster_name']
postgresql_data_dir = "/var/lib/postgresql"
postgresql_cluster_dir = os.path.join(
    postgresql_data_dir, version, cluster_name)
postgresql_bin_dir = os.path.join('/usr/lib/postgresql', version, 'bin')
postgresql_config_dir = os.path.join("/etc/postgresql", version, cluster_name)
postgresql_config = os.path.join(postgresql_config_dir, "postgresql.conf")
postgresql_ident = os.path.join(postgresql_config_dir, "pg_ident.conf")
postgresql_hba = os.path.join(postgresql_config_dir, "pg_hba.conf")
postgresql_crontab = "/etc/cron.d/postgresql"
postgresql_service_config_dir = "/var/run/postgresql"
postgresql_scripts_dir = os.path.join(postgresql_data_dir, 'scripts')
postgresql_backups_dir = os.path.join(postgresql_data_dir, 'backups')
postgresql_logs_dir = os.path.join(postgresql_data_dir, 'logs')
postgres_ssh_dir = os.path.expanduser('~postgres/.ssh')
postgres_ssh_public_key = os.path.join(postgres_ssh_dir, 'id_rsa.pub')
postgres_ssh_private_key = os.path.join(postgres_ssh_dir, 'id_rsa')
postgres_ssh_authorized_keys = os.path.join(postgres_ssh_dir, 'authorized_keys')
postgres_ssh_known_hosts = os.path.join(postgres_ssh_dir, 'known_hosts')
postgres_pgpass = os.path.expanduser('~postgres/.pgpass')
repmgr_config = os.path.expanduser('~postgres/repmgr.conf')
hook_name = os.path.basename(sys.argv[0])
replication_relation_types = ['master', 'slave', 'replication']

###############################################################################
# Main section
###############################################################################
if hook_name == "install":
    install()
#-------- config-changed
elif hook_name == "config-changed":
    config_changed(postgresql_config)
#-------- upgrade-charm
elif hook_name == "upgrade-charm":
    install(run_pre=False)
    config_changed(postgresql_config)
#-------- start
elif hook_name == "start":
    if not postgresql_restart():
        sys.exit(1)
#-------- stop
elif hook_name == "stop":
    if not postgresql_stop():
        sys.exit(1)
#-------- db-relation-joined, db-relation-changed
elif hook_name in ["db-relation-joined", "db-relation-changed"]:
    roles = filter(None, relation_get('roles').split(","))
    database = relation_get('database')
    if database == '':
        # Missing some information. We expect it to appear in a
        # future call to the hook.
        juju_log(MSG_WARNING, "No database set in relation, exiting")
        sys.exit(0)
    user = \
        user_name(os.environ['JUJU_RELATION_ID'],
            os.environ['JUJU_REMOTE_UNIT'])
    if user != '' and database != '':
        db_relation_joined_changed(user, database, roles)
#-------- db-relation-broken
elif hook_name == "db-relation-broken":
    database = relation_get('database')
    user = \
        user_name(os.environ['JUJU_RELATION_ID'],
            os.environ['JUJU_REMOTE_UNIT'])
    db_relation_broken(user, database)
#-------- db-admin-relation-joined, db-admin-relation-changed
elif hook_name in ["db-admin-relation-joined", "db-admin-relation-changed"]:
    user = user_name(os.environ['JUJU_RELATION_ID'],
        os.environ['JUJU_REMOTE_UNIT'], admin=True)
    db_admin_relation_joined_changed(user, 'all')
#-------- db-admin-relation-broken
elif hook_name == "db-admin-relation-broken":
    # XXX: Fix: relation is not set when it is already broken
    # cannot determine the user name
    user = user_name(os.environ['JUJU_RELATION_ID'],
        os.environ['JUJU_REMOTE_UNIT'], admin=True)
    db_admin_relation_broken(user)
elif hook_name == "nrpe-external-master-relation-changed":
    update_nrpe_checks()
elif hook_name in (
    'master-relation-joined', 'slave-relation-joined',
    'replication-relation-joined'):
    replication_relation_joined()
elif hook_name in (
    'master-relation-changed', 'slave-relation-changed',
    'replication-relation-changed'):
    replication_relation_changed()
elif hook_name in (
    'master-relation-broken', 'slave-relation-broken',
    'replication-relation-broken', 'replication-relation-departed'):
    replication_relation_broken()
#-------- persistent-storage-relation-joined,
#         persistent-storage-relation-changed
#elif hook_name in ["persistent-storage-relation-joined",
#    "persistent-storage-relation-changed"]:
#    persistent_storage_relation_joined_changed()
#-------- persistent-storage-relation-broken
#elif hook_name == "persistent-storage-relation-broken":
#    persistent_storage_relation_broken()
else:
    print "Unknown hook {}".format(hook_name)
    sys.exit(1)
