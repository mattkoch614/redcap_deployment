"""
This fab file packages, deploys, and upgrades REDCap. The fab file uses a
settings file to define the parameters of each deployed instance/environment.

Usage:

  fab package<:redcapM.N.O.zip>
  fab <instance_name> deploy<:redcap-M.N.O.tgz>
  fab <instance_name> upgrade<:redcap-M.N.O.tgz>
  fab instance:<instance_name> upgrade<:redcap-M.N.O.tgz>

Instances

Each environment is a separate REDCap instance. Each instance can be deployed
or upgraded. The valid instances are:

  vagrant - a local development instance
  stage - a staging test instance
  prod - the production instance

Each instance requires a same-named file at the local path settings/<name>.ini

The *instance* function can be used to use an arbitrary instance by providing
the instance name as a parameter to the instance function:

  fab instance:vagrant deploy:redcap-6.18.1.tgz
  fab instance:stage2 deploy:redcap-7.1.0.tgz


Deploying

When (re)deploying the instance named 'vagrant', be aware that deploy will
drop the instance database.


Upgrading

Upgrade packages must be created using one of Vanderbilt's 'upgrade' zip files
or database credentials will not be preserved.

"""

from fabric.api import *
from fabric.contrib.files import exists
from fabric.utils import abort
from datetime import datetime
import configparser, string, random, os
from tempfile import mkstemp
import re
import fnmatch
import server_setup
import package


def write_my_cnf():
    _, file = mkstemp()
    f = open(file, 'w')
    f.write("[mysqldump]" + "\n")
    f.write("user=" + env.database_user + "\n")
    f.write("password='" + env.database_password + "'\n")
    f.write("" + "\n")
    f.write("[client]" + "\n")
    f.write("user=" + env.database_user + "\n")
    f.write("password='" + env.database_password + "'\n")
    f.write("database=" + env.database_name + "\n")
    f.write("host=" + env.database_host + "\n")
    f.close()
    return file


@task
def write_remote_my_cnf():
    """
    Write a .my.cnf into the deploy user's home directory.
    """
    global w_counter
    file = write_my_cnf()
    with settings(user=env.deploy_user):
        target_path = '/home/%s/.my.cnf' % get_config('deploy_user')
        put(file, target_path , use_sudo=False)
        run('chmod 600 %s' % target_path)
    os.unlink(file)
    w_counter = w_counter+1


@task
def delete_remote_my_cnf():
    """
    Delete .my.cnf from the deploy user's home directory.
    """
    global w_counter
    w_counter = w_counter-1
    if w_counter == 0:
        my_cnf = '/home/%s/.my.cnf' % get_config('deploy_user')
        with settings(user=env.deploy_user):
            if run("test -e %s" % my_cnf).succeeded:
                run('rm -rf %s' % my_cnf)


def timestamp():
    return datetime.now().strftime("%Y%m%dT%H%M%Z")


@task(alias='backup')
def backup_database(options=""):
    """
    Backup a mysql database from the remote host with mysqldump options in *options*.

    The backup file will be time stamped with a name like 'redcap-<instance_name>-20170126T1620.sql.gz'
    The latest backup file will be linked to name 'redcap-<instance_name>-latest.sql.gz'
    """
    write_remote_my_cnf()
    now = timestamp()
    with settings(user=env.deploy_user):
        run("mysqldump --skip-lock-tables %s -u %s -h %s %s | gzip > redcap-%s-%s.sql.gz" % \
            (options, env.database_user, env.database_host, env.database_name, env.instance_name, now))
        run("ln -sf redcap-%s-%s.sql.gz redcap-%s-latest.sql.gz" % (env.instance_name, now, env.instance_name))
    delete_remote_my_cnf()

##########################


def update_redcap_connection(db_settings_file="database.php", salt="abc"):
    """
    Update the database.php file with settings from the chosen environment
    """

    redcap_database_settings_path = "/".join([env.backup_pre_path, env.remote_project_name, db_settings_file])
    with settings(user=env.deploy_user):
        run('echo \'$hostname   = "%s";\' >> %s' % (env.database_host, redcap_database_settings_path))
        run('echo \'$db   = "%s";\' >> %s' % (env.database_name, redcap_database_settings_path))
        run('echo \'$username   = "%s";\' >> %s' % (env.database_user, redcap_database_settings_path))
        run('echo \'$password   = "%s";\' >> %s' % (env.database_password, redcap_database_settings_path))
        run('echo \'$salt   = "%s";\' >> %s' % (salt, redcap_database_settings_path))


def create_database():
    """
    Create an empty database in MySQL dropping the existing database if need be.
    """

    # Only run on a local testing environment
    if not env.vagrant_instance:
        abort("create_database can only be run against the Vagrant instance")

    # generate the DROP/CREATE command
    create_database_sql="""
    DROP DATABASE IF EXISTS %(database_name)s;
    CREATE DATABASE %(database_name)s;

    GRANT
        SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, EXECUTE, CREATE VIEW, SHOW VIEW
    ON
        %(database_name)s.*
    TO
        '%(database_user)s'@'%(database_host)s'
    IDENTIFIED BY
        '%(database_password)s';""" % env

    # run the DROP/CREATE command as root
    with settings(user=env.deploy_user):
        run('echo "%s" | mysql -u root -p%s' % (create_database_sql, env.database_root_password))


def is_affirmative(response):
    """
    Turn strings that mean 'yes' into a True value, else False
    """

    if  re.match("^(force|true|t|yes|y)$", response, re.IGNORECASE):
        return True
    else:
        return False


@task
def delete_all_tables(confirm=""):
    """
    Delete all tables for the database specified in the instance. You must confirm this command.
    """
    if is_affirmative(confirm):
        write_remote_my_cnf()
        with settings(user=env.deploy_user):
            run("mysqldump --add-drop-table --no-data --single-transaction --databases %s | grep -e '^DROP \| FOREIGN_KEY_CHECKS' | mysql %s" \
                % (env.database_name, env.database_name))
        delete_remote_my_cnf()
    else:
        print "\nProvide a confirmation string (e.g. 'y', 'yes') if you want to delete all MySQL tables for this instance."


def set_redcap_base_url():
    """
    Set the REDCap base url
    """

    set_redcap_config('redcap_base_url', env.url_of_deployed_app)


def set_redcap_config(field_name="", value=""):
    """
    Update a single values in the redcap config table
    """
    with settings(user=env.deploy_user):
        run('echo "update redcap_config set value=\'%s\' where field_name = \'%s\';" | mysql' % (value, field_name))


def set_hook_functions_file():
    """
    Sets the hook_functions_file
    """
    value = '%s/%s' % (env.live_project_full_path,env.hooks_framework_path)
    set_redcap_config('hook_functions_file',value)


def create_redcap_tables(resource_path = "Resources/sql"):
    """
    Create redcap tables via the remote host
    """
    print("Creating redcap tables")
    redcap_sql_root_dir = os.path.join(env.backup_pre_path,env.remote_project_name)
    with settings(user=env.deploy_user):
        redcap_name = run("ls %s | grep 'redcap_v[0-9]\{1,2\}\.[0-9]\{1,2\}\.[0-9]\{1,2\}' | sort -n | tail -n 1" % redcap_sql_root_dir)
    redcap_sql_dir = os.path.join(redcap_sql_root_dir,redcap_name,resource_path)
    match = re.search('redcap_v(\d+.\d+.\d+)', redcap_name)
    version = match.group(1)
    with settings(user=env.deploy_user):
        run('mysql < %s/install.sql' % redcap_sql_dir)
        run('mysql < %s/install_data.sql' % redcap_sql_dir)
        run('mysql -e "UPDATE %s.redcap_config SET value = \'%s\' WHERE field_name = \'redcap_version\' "' % (env.database_name, version))

        files = run('ls -v1 %s/create_demo_db*.sql' % redcap_sql_dir)
        for file in files.splitlines():
            print("Executing sql file %s" % file)
            run('mysql < %s' % file)


@task
def apply_sql_to_db(sql_file=""):
    """
    Copy a local SQL file to the remote host and run it against mysql

    """
    if local('test -e %s' % sql_file).succeeded:
        with settings(user=env.deploy_user):
            write_remote_my_cnf()
            remote_sql_path = run('mktemp')
            put(sql_file, remote_sql_path)
            if run('mysql < %s' % remote_sql_path).succeeded:
                run('rm %s' % remote_sql_path)
            delete_remote_my_cnf()


def configure_redcap_cron(deploy=False, force_deployment_of_redcap_cron=False):
    crond_for_redcap = '/etc/cron.d/%s' % env.project_path
    with settings(warn_only=True):
        if deploy:
            if run("test -e %s" % crond_for_redcap).failed or force_deployment_of_redcap_cron:
                sudo('echo "# REDCap Cron Job (runs every minute)" > %s' % crond_for_redcap)
                sudo('echo "* * * * * root /usr/bin/php %s/cron.php > /dev/null" >> %s' \
                    % (env.live_project_full_path, crond_for_redcap))
        else:
            warn("Not deploying REDCap Cron. Set deploy_redcap_cron=True in instance's ini to deploy REDCap Cron.")


def move_edocs_folder():
    """
    Move the redcap/edocs folder out of web space.
    """
    default_edoc_path = '%s/edocs' % env.live_project_full_path
    with settings(user=env.deploy_user):
        with settings(warn_only=True):
            if run("test -e %s" % env.edoc_path).succeeded:
                set_redcap_config('edoc_path',env.edoc_path)
            if run("test -e %s" % default_edoc_path).succeeded:
                with settings(warn_only=False):
                    file_name = run('ls -1 %s' % default_edoc_path)
                    if file_name == "index.html":
                        run('rm -r %s' % default_edoc_path)


def extract_version_from_string(string):
    """
    extracts version number from string
    """
    match = re.search(r"(\d+\.\d+\.\d+)", string)
    version=match.group(1)
    return version

######################


@task
def upgrade(name):
    """
    Upgrade an existing redcap instance using the <name> package.

    This input file should be in the TGZ format
    as packaged by this fabfile
    """

    make_upload_target()
    copy_running_code_to_backup_dir()
    upload_package_and_extract(name)
    write_remote_my_cnf()
    offline()
    move_software_to_live()
    new = extract_version_from_string(name)
    old = get_current_redcap_version()
    apply_incremental_db_changes(old,new)
    online()
    delete_remote_my_cnf()


def make_upload_target():
    """
    Make the directory from which new software will be deployed,
    e.g., /var/www.backup/redcap-20160117T1543/
    """
    env.upload_target_backup_dir = '/'.join([env.upload_project_full_path, env.remote_project_name])
    with settings(user=env.deploy_user):
        run("mkdir -p %(upload_target_backup_dir)s" % env)


def copy_running_code_to_backup_dir():
    """
    Copy the running code e.g. /var/www/redcap/* to the directory from which the
    the new software will be deployed, e.g., /var/www.backup/redcap-20160117T1543/.
    This will allow the new software to be overlain on the old software without
    risk of corrupting the old software.
    """
    with settings(user=env.deploy_user):
        with settings(warn_only=True):
            if run("test -e %(live_project_full_path)s/cron.php" % env).succeeded:
                run("cp -r -P %(live_project_full_path)s/* %(upload_target_backup_dir)s" % env)


def upload_package_and_extract(name):
    """
    Upload the redcap package and extract it into the directory from which new
    software will be deployed, e.g., /var/www.backup/redcap-20160117T1543/
    """
    # NOTE: run as $ fab <env> package make_upload_target upe ...necessary env
    # variables are set by package and make_upload_target funcitons
    with settings(user=env.deploy_user):
        # Make a temp folder to upload the tar to
        temp1 = run('mktemp -d')
        put(name, temp1)
        # Test where temp/'receiving' is
        temp2 = run('mktemp -d')
        # Extract in temp ... -C specifies what directory to extract to
        # Extract to temp2 so the tar is not included in the contents
        run('tar -xzf %s/%s -C %s' % (temp1, name, temp2))
        # Transfer contents from temp2/redcap to ultimate destination
        with settings(warn_only=True):
            if run('test -d %s/webtools2/pdf/font/unifont' % env.upload_target_backup_dir).succeeded:
                run('chmod ug+w %s/webtools2/pdf/font/unifont/*' % env.upload_target_backup_dir)
        run('rsync -rc %s/redcap/* %s' % (temp2, env.upload_target_backup_dir))
        # Remove the temp directories
        run('rm -rf %s %s' % (temp1, temp2))


@task
def offline():
    """
    Take REDCap offline
    """

    change_online_status('Offline')


def move_software_to_live():
    '''Replace the symbolic link to the old code with symbolic link to new code.'''
    with settings(user=env.deploy_user):
        with settings(warn_only=True):
            if run("test -d %(live_project_full_path)s" % env).succeeded:
                # we need to back this directory up on the fly, destroy it and then symlink it back into existence
                with settings(warn_only=False):
                    new_backup_dir = env.upload_target_backup_dir + "-previous"
                    run("mkdir -p %s" % new_backup_dir)
                    run("cp -rf -P %s/* %s" % (env.live_project_full_path, new_backup_dir))
                    run("rm -rf  %s" % env.live_project_full_path)

        # now switch the new code to live
        run('ln -s %s %s' % (env.upload_target_backup_dir,env.live_project_full_path))


def convert_version_to_int(version):
    """
    Convert a redcap version number to integer
    """
    version = int("%d%02d%02d" % tuple(map(int,version.split('.'))))
    return version


def get_current_redcap_version():
    """
    gets the current redcap version from database
    """
    with settings(user=env.deploy_user):
        with hide('output'):
            current_version = run('mysql -s -N -e "SELECT value from redcap_config WHERE field_name=\'redcap_version\'"')
    return current_version


def apply_incremental_db_changes(old, new):
    """
    Upgrade the database from the <old> REDCap version to the <new> version.

    Applying the needed upgrade_M.NN.OO.sql and upgrade_M.NN.OO.ph files in
    sequence. The arguments old and new must be version numbers (i.e., 6.11.5)
    """
    old = convert_version_to_int(old)
    redcap_sql_dir = '/'.join([env.live_pre_path, env.project_path, 'redcap_v' + new, 'Resources/sql'])
    with settings(user=env.deploy_user):
        with hide('output'):
            files = run('ls -1 %s/upgrade_*.sql %s/upgrade_*.php  | sort --version-sort ' % (redcap_sql_dir, redcap_sql_dir))
    path_to_sql_generation = '/'.join([env.live_pre_path, env.project_path, 'redcap_v' + new, 'generate_upgrade_sql_from_php.php'])
    for file in files.splitlines():
        match = re.search(r"(upgrade_)(\d+.\d+.\d+)(.)(php|sql)", file)
        version = match.group(2)
        version = convert_version_to_int(version)
        if(version > old):
            if fnmatch.fnmatch(file, "*.php"):
                print (file + " is a php file!\n")
                with settings(user=env.deploy_user):
                    run('php %s %s | mysql' % (path_to_sql_generation,file))
            else:
                print("Executing sql file %s" % file)
                with settings(user=env.deploy_user):
                    run('mysql < %s' % file)
    # Finalize upgrade
    set_redcap_config('redcap_last_install_date', datetime.now().strftime("%Y-%m-%d"))
    set_redcap_config('redcap_version', new)


@task
def online():
    """
    Put REDCap back online
    """

    change_online_status('Online')


def change_online_status(state):
    """
    Set the online/offline status with <state>.
    """

    with settings(user=env.deploy_user):
        if state == "Online":
            offline_binary = 0
            offline_message = 'The system is online.'
        elif state == "Offline":
            offline_binary = 1
            offline_message = 'The system is offline.'
        else:
            abort("Invald state provided. Specify 'Online' or 'Offline'.")

        write_remote_my_cnf()
        set_redcap_config('system_offline', '%s' % offline_binary)
        set_redcap_config('system_offline_message', '%s' % offline_message)
        delete_remote_my_cnf()


@task
def test():
    """
    Run all tests against a running REDCap instance
    """
    write_remote_my_cnf()
    version = get_current_redcap_version()
    delete_remote_my_cnf()
    local("python tests/test.py %s/ redcap_v%s/" % (env.url_of_deployed_app,version))


##########################

def get_config(key, section="instance"):
    return config.get(section, key)


def define_default_env(settings_file_path="settings/defaults.ini"):
    """
    This function sets up some global variables
    """

    #first, copy the secrets file into the deploy directory
    if os.path.exists(settings_file_path):
        config.read(settings_file_path)
    else:
        print("The secrets file path cannot be found. It is set to: %s" % settings_file_path)
        abort("Secrets File not set")

    section="DEFAULT"
    for (name,value) in config.items(section):
        env[name] = value


def define_env(settings_file_path=""):
    """
    This function sets up some global variables
    """

    #Set defaults
    env.deploy_redcap_cron = False

    #first, copy the secrets file into the deploy directory
    if os.path.exists(settings_file_path):
        config.read(settings_file_path)
    else:
        print("The secrets file path cannot be found. It is set to: %s" % settings_file_path)
        abort("Secrets File not set")

    # if get_config('deploy_user') != "":
    #     env.user = get_config('deploy_user')

    section="instance"
    for (name,value) in config.items(section):
        env[name] = value
    # Set variables that do not have corresponding values in vagrant.ini file
    time = timestamp()
    env.remote_project_name = '%s-%s' % (env.project_path,time)
    env.live_project_full_path = get_config('live_pre_path') + "/" + get_config('project_path') #
    env.backup_project_full_path = get_config('backup_pre_path') + "/" + get_config('project_path')
    env.upload_project_full_path = get_config('backup_pre_path')

    env.hosts = [get_config('host')]
    env.port = get_config('host_ssh_port')


@task(alias='dev')
def vagrant():
    """
    Set up deployment for vagrant
    """
    instance('vagrant')


@task
def stage():
    """
    Set up deployment for staging server
    """
    instance('stage')


@task
def prod():
    """
    Set up deployment for production server
    """
    instance('prod')


@task
def instance(name = ""):
    """
    Set up deployment for vagrant/stage/prod server
    """

    if(name == ""):
        abort("Please provide an instance name")
    settings_file_path = 'settings/%s.ini' % name
    if(name == 'vagrant'):
        env.vagrant_instance = True
    else:
        env.vagrant_instance = False
    define_env(settings_file_path)


@task
def deploy(name,force=""):
    """
    Deploy a new REDCap instance defined by <package_name>, optionally forcing redcap cron deployment

    """
    make_upload_target()
    upload_package_and_extract(name)
    update_redcap_connection()
    write_remote_my_cnf()
    if env.vagrant_instance:
        create_database()
    create_redcap_tables()
    move_software_to_live()
    move_edocs_folder()
    set_redcap_base_url()
    set_hook_functions_file()
    force_deployment_of_redcap_cron = is_affirmative(force)
    configure_redcap_cron(env.deploy_redcap_cron, force_deployment_of_redcap_cron)
    delete_remote_my_cnf()
    #TODO: Run tests


config = configparser.ConfigParser()
w_counter = 0
default_settings_file_path = 'settings/defaults.ini' #path to where app is looking for settings.ini
define_default_env(default_settings_file_path) # load default settings

