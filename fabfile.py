from __future__ import with_statement
from fabric.api import *
from fabric.utils import error
from fabric.contrib.console import confirm
from fabric.context_managers import settings
from fabric.contrib import files

from git import *

import os
import json
import getpass
import datetime
import time
import requests
from termcolor import colored
from pprint import pprint

debug = True

env.local_project_path = os.path.dirname(os.path.realpath(__file__))
# default to local override in env
env.remote_project_path = env.local_project_path

env.repo = Repo(env.local_project_path)

env.environment_class = 'local'
env.project = 'marketing'

env.dev_fixtures = 'dev-fixtures'
env.fixtures = 'sites tools'

env.SHA1_FILENAME = None
env.timestamp = time.time()
env.is_predeploy = False
env.local_user = getpass.getuser()
env.environment = 'local'

env.truthy = ['true','t','y','yes','1',1]
env.falsy = ['false','f','n','no','0',0]

env.dist_path = os.path.join(env.local_project_path, '.build')
env.zip_extracted_folder_name = os.path.basename(os.path.normpath(env.dist_path))

@task
def production():
    env.environment = 'production'
    env.environment_class = 'production'

    env.remote_project_path = '/var/apps/marketing/'
    env.deploy_archive_path = '/var/apps/'
    env.virtualenv_path = None

    env.newrelic_api_token = 'ec2a185854e15d572186b246961e0ed11378cc249d0a0cd'
    env.newrelic_app_name = 'Marketing'
    env.newrelic_application_id = '1858111'

    # change from the default user to 'vagrant'
    env.user = 'ubuntu'
    env.application_user = 'app'
    # connect to the port-forwarded ssh
    env.hosts = ['ec2-184-169-191-190.us-west-1.compute.amazonaws.com',
                 'ec2-184-72-21-48.us-west-1.compute.amazonaws.com',] if not env.hosts else env.hosts
    env.celery_name = 'celery-production' # taken from chef cookbook

    env.key_filename = '%s/../lawpal-chef/chef-machines.pem' % env.local_project_path

    env.start_service = None
    env.stop_service = None
    env.light_restart = None


env.roledefs.update({
    'web': ['ec2-50-18-33-186.us-west-1.compute.amazonaws.com'],
    'worker': ['ec2-54-241-222-221.us-west-1.compute.amazonaws.com'],
})

def env_run(cmd):
    return sudo(cmd) if env.environment_class in ['production', 'celery'] else run(cmd)

def get_sha1():
  cd(env.local_project_path)
  return local('git rev-parse --short --verify HEAD', capture=True)

@task
def git_tags():
    """ returns list of tags """
    tags = env.repo.tags
    return tags

@task
def git_previous_tag():
    # last tag in list
    previous = git_tags()[-1]
    return previous

@task
def git_suggest_tag():
    """ split into parts v1.0.0 drops v converts to ints and increaments and reassembles v1.0.1"""
    previous = git_previous_tag().name.split('.')
    mapped = map(int, previous[1:]) # convert all digits to int but exclude the first one as it starts with v and cant be an int
    next = [int(previous[0].replace('v',''))] + mapped #remove string v and append mapped list
    next_rev = next[2] = mapped[-1] + 1 # increment the last digit
    return {
        'next': 'v%s' % '.'.join(map(str,next)), 
        'previous': '.'.join(previous)
    }

@task
@runs_once
def git_set_tag():
    proceed = prompt(colored('Do you want to tag this realease?', 'red'), default='y')
    if proceed in env.truthy:
        suggested = git_suggest_tag()
        tag = prompt(colored('Please enter a tag: previous: %s suggested: %s' % (suggested['previous'], suggested['next']), 'yellow'), default=suggested['next'])
        if tag:
            tag = 'v%s' % tag if tag[0] != 'v' else tag # ensure we start with a "v"
            env.repo.create_tag(tag)

@task
def git_export(branch='master'):
  env.SHA1_FILENAME = get_sha1()

  if not os.path.exists('/tmp/%s.zip' % env.SHA1_FILENAME):
        cmd = 'cd %s;git archive --format zip --output /tmp/%s.zip --prefix=%s %s %s' % (env.local_project_path, env.SHA1_FILENAME, env.SHA1_FILENAME, branch, env.zip_extracted_folder_name,)
        local(cmd, capture=False)

@task
@runs_once
def current_version_sha():
    current = '%s%s' % (env.remote_project_path, env.project)
    realpath = run('ls -al %s' % current)
    current_sha = realpath.split('/')[-1]
    return current_sha

@task
@runs_once
def diff_outgoing_with_current():
    diff = local('git diff %s %s' % (get_sha1(), current_version_sha(),), capture=True)
    print(diff)

@task
def prepare_deploy():
    git_export()


# ------ RESTARTERS ------#

@task
def stop_nginx():
    with settings(warn_only=True):
        sudo('service nginx stop')

@task
def start_nginx():
    with settings(warn_only=True):
        sudo('service nginx start')

@task
def restart_nginx():
    with settings(warn_only=True):
        sudo('service nginx restart')

# ------ END-RESTARTERS ------#


@task
def deploy_archive_file():
    filename = env.get('SHA1_FILENAME', None)
    if filename is None:
        filename = env.SHA1_FILENAME = get_sha1()
    file_name = '%s.zip' % filename
    if not files.exists('%s/%s' % (env.deploy_archive_path, file_name)):
        as_sudo = env.environment_class in ['production', 'celery']
        put('/tmp/%s' % file_name, env.deploy_archive_path, use_sudo=as_sudo)
        env_run('chown %s:%s %s' % (env.application_user, env.application_user, env.deploy_archive_path) )


def clean_zip():
    file_name = '%s.zip' % env.SHA1_FILENAME
    if files.exists('%s%s' % (env.deploy_archive_path, file_name)):
        env_run('rm %s%s' % (env.deploy_archive_path, file_name,))

@task
def relink():
    if not env.SHA1_FILENAME:
        env.SHA1_FILENAME = get_sha1()

    version_path = '%sversions' % env.remote_project_path
    project_path = '%s%s' % (env.remote_project_path, env.project,)

    if not env.is_predeploy:
        if files.exists('%s/%s%s' % (version_path, env.SHA1_FILENAME, env.zip_extracted_folder_name)): # check the sha1 dir exists
            #if files.exists(project_path, use_sudo=True): # unlink the glynt dir
            if files.exists('%s/%s' % (env.remote_project_path, env.project)): # check the current glynt dir exists
                env_run('unlink %s' % project_path)
            env_run('ln -s %s/%s%s %s' % (version_path, env.SHA1_FILENAME, env.zip_extracted_folder_name, project_path,)) # relink

@task
def clean_start():
    restart_nginx()
    clean_zip()

@task
def do_deploy():
    if env.SHA1_FILENAME is None:
        env.SHA1_FILENAME = get_sha1()

    version_path = '%sversions' % env.remote_project_path
    full_version_path = '%s/%s%s' % (version_path, env.SHA1_FILENAME, env.zip_extracted_folder_name)
    project_path = '%s%s' % (env.remote_project_path, env.project,)

    if env.environment_class in ['production', 'celery']:
        if not files.exists(version_path):
            env_run('mkdir -p %s' % version_path )
        sudo('chown -R %s:%s %s' % (env.application_user, env.application_user, env.remote_project_path) )

    deploy_archive_file()

    # extract project zip file:into a staging area and link it in
    if not files.exists('%s/manage.py' % full_version_path):
        unzip_archive()


@task
def update_env_conf():
    if env.SHA1_FILENAME is None:
        env.SHA1_FILENAME = get_sha1()

    version_path = '%sversions' % env.remote_project_path
    full_version_path = '%s/%s' % (version_path, env.SHA1_FILENAME)
    project_path = '%s%s' % (env.remote_project_path, env.project,)

    if not env.is_predeploy:
        # copy the live local_settings
        with cd(project_path):
            env_run('cp %s/conf/%s.local_settings.py %s/%s/local_settings.py' % (full_version_path, env.environment, full_version_path, env.project))
            env_run('cp %s/conf/%s.wsgi.py %s/%s/wsgi.py' % (full_version_path, env.environment, full_version_path, env.project))
            #env_run('cp %s/conf/%s.newrelic.ini %s/%s/newrelic.ini' % (full_version_path, env.environment, full_version_path, env.project))

@task
def unzip_archive():
    version_path = '%sversions' % env.remote_project_path
    with cd('%s' % version_path):
        env_run('unzip %s%s.zip -d %s' % (env.deploy_archive_path, env.SHA1_FILENAME, version_path,))

@task
def requirements():
    sha = env.get('SHA1_FILENAME', None)
    if sha is None:
        env.SHA1_FILENAME = get_sha1()
    
    project_path = '%sversions/%s' % (env.remote_project_path, env.SHA1_FILENAME,)
    requirements_path = '%s/requirements/dev.txt' % (project_path, )

    env_run('pip install -r %s' % requirements_path )


@task
def clean_versions(delete=False, except_latest=3):
    current_version = get_sha1()

    versions_path = '%sversions' % env.remote_project_path
    #
    # cd into the path so we can use xargs
    # tail the list except the lastest N
    # exclude the known current version
    #
    cmd = "cd {path};ls -t1 {path} | tail -n+{except_latest} | grep -v '{current_version}'".format(path=versions_path, except_latest=except_latest, current_version=current_version)
    #
    # optionally delete them
    #
    if delete in env.truthy:
        cmd = cmd + ' | xargs rm -Rf'

    env_run(cmd)


@task
@serial
@runs_once
def diff():
    diff = prompt(colored("View diff? [y,n]", 'magenta'), default="y")
    if diff.lower() in ['y','yes', 1, '1']:
        print(diff_outgoing_with_current())


@task
def deploy(is_predeploy='False',full='False',db='False',search='False'):
    """
    :is_predeploy=True - will deploy the latest MASTER SHA but not link it in: this allows for assets collection
    and requirements update etc...
    """
    env.is_predeploy = is_predeploy.lower() in env.truthy
    full = full.lower() in env.truthy
    db = db.lower() in env.truthy
    search = search.lower() in env.truthy

    diff()
    git_set_tag()

    prepare_deploy()
    do_deploy()

    relink()
    clean_start()
