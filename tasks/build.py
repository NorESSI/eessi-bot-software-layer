# This file is part of the EESSI build-and-deploy bot,
# see https://github.com/EESSI/eessi-bot-software-layer
#
# The bot helps with requests to add software installations to the
# EESSI software layer, see https://github.com/EESSI/software-layer
#
# author: Kenneth Hoste (@boegel)
# author: Bob Droege (@bedroge)
# author: Hafsa Naeem (@hafsa-naeem)
# author: Thomas Roeblitz (@trz42)
#
# license: GPLv2
#
import configparser
import json
import os
import shutil
import sys

from collections import namedtuple
from connections import github
from datetime import datetime, timezone
from pyghee.utils import log, error
from retry.api import retry_call
from tools import config, run_cmd, pr_comments
from tools.job_metadata import create_metadata_file

APP_NAME = "app_name"
AWAITS_RELEASE = "awaits_release"
BUILDENV = "buildenv"
BUILD_JOB_SCRIPT = "build_job_script"
CONTAINER_CACHEDIR = "container_cachedir"
DEFAULT_JOB_TIME_LIMIT = "24:00:00"
CVMFS_CUSTOMIZATIONS = "cvmfs_customizations"
GITHUB = "github"
HTTP_PROXY = "http_proxy"
HTTPS_PROXY = "https_proxy"
INITIAL_COMMENT = "initial_comment"
JOBS_BASE_DIR = "jobs_base_dir"
LOAD_MODULES = "load_modules"
LOCAL_TMP = "local_tmp"
SLURM_PARAMS = "slurm_params"
SUBMITTED_JOB_COMMENTS = "submitted_job_comments"
SUBMIT_COMMAND = "submit_command"
BUILD_PERMISSION = "build_permission"
NO_BUILD_PERMISSION_COMMENT = "no_build_permission_comment"
ARCHITECTURE_TARGETS = "architecturetargets"
REPO_TARGETS = "repo_targets"
REPO_TARGET_MAP = "repo_target_map"
REPOS_CFG_DIR = "repos_cfg_dir"
REPOS_ID = "repo_id"
REPOS_REPO_NAME = "repo_name"
REPOS_REPO_VERSION = "repo_version"
REPOS_CONFIG_BUNDLE = "config_bundle"
REPOS_CONFIG_MAP = "config_map"
REPOS_CONTAINER = "container"

JOB_SITECONFIG = "site_config"
JOB_LOCAL_TMP = "local_tmp"
JOB_HTTP_PROXY = "http_proxy"
JOB_HTTPS_PROXY = "https_proxy"
JOB_LOAD_MODULES = "load_modules"
JOB_REPOSITORY = "repository"
JOB_CONTAINER = "container"
JOB_REPO_ID = "repo_id"
JOB_REPO_NAME = "repo_name"
JOB_REPO_VERSION = "repo_version"
JOB_REPOS_CFG_DIR = "repos_cfg_dir"
JOB_ARCHITECTURE = "architecture"
JOB_SOFTWARE_SUBDIR = "software_subdir"
JOB_OS_TYPE = "os_type"

Job = namedtuple('Job', ('working_dir', 'arch_target', 'repo_id', 'slurm_opts', 'year_month', 'pr_id'))

# global repo_cfg
repo_cfg = {}


def get_build_env_cfg(cfg):
    """Gets build environment values

    Args:
        cfg (dict): dictionary holding full configuration (by default defined in app.cfg)

    Returns:
         dict(str, dict): dictionary of configuration data
    """
    fn = sys._getframe().f_code.co_name

    # cfg = config.read_config()
    buildenv = cfg[BUILDENV]

    jobs_base_dir = buildenv.get(JOBS_BASE_DIR)
    log(f"{fn}(): jobs_base_dir '{jobs_base_dir}'")
    config_data = {JOBS_BASE_DIR: jobs_base_dir}
    local_tmp = buildenv.get(LOCAL_TMP)
    log(f"{fn}(): local_tmp '{local_tmp}'")
    config_data[LOCAL_TMP] = local_tmp

    build_job_script = buildenv.get(BUILD_JOB_SCRIPT)
    log(f"{fn}(): build_job_script '{build_job_script}'")
    config_data[BUILD_JOB_SCRIPT] = build_job_script

    submit_command = buildenv.get(SUBMIT_COMMAND)
    log(f"{fn}(): submit_command '{submit_command}'")
    config_data[SUBMIT_COMMAND] = submit_command

    slurm_params = buildenv.get(SLURM_PARAMS)
    # always submit jobs with hold set, so job manager can release them
    slurm_params += ' --hold'
    log(f"{fn}(): slurm_params '{slurm_params}'")
    config_data[SLURM_PARAMS] = slurm_params

    container_cachedir = buildenv.get(CONTAINER_CACHEDIR)
    log(f"{fn}(): container_cachedir '{container_cachedir}'")
    config_data[CONTAINER_CACHEDIR] = container_cachedir

    cvmfs_customizations = {}
    try:
        cvmfs_customizations_str = buildenv.get(CVMFS_CUSTOMIZATIONS)
        log("{fn}(): cvmfs_customizations '{cvmfs_customizations_str}'")

        if cvmfs_customizations_str is not None:
            cvmfs_customizations = json.loads(cvmfs_customizations_str)

        log(f"{fn}(): cvmfs_customizations '{json.dumps(cvmfs_customizations)}'")
    except json.JSONDecodeError as e:
        print(e)
        error(f"{fn}(): Value for cvmfs_customizations ({cvmfs_customizations_str}) could not be decoded.")

    config_data[CVMFS_CUSTOMIZATIONS] = cvmfs_customizations

    http_proxy = buildenv.get(HTTP_PROXY, None)
    log(f"{fn}(): http_proxy '{http_proxy}'")
    config_data[HTTP_PROXY] = http_proxy

    https_proxy = buildenv.get(HTTPS_PROXY, None)
    log(f"{fn}(): https_proxy '{https_proxy}'")
    config_data[HTTPS_PROXY] = https_proxy

    load_modules = buildenv.get(LOAD_MODULES, None)
    log(f"{fn}(): load_modules '{load_modules}'")
    config_data[LOAD_MODULES] = load_modules

    return config_data


def get_architecture_targets(cfg):
    """get architecture_targets and set arch_target_map

    Args:
        cfg (dict): dictionary holding full configuration (by default defined in app.cfg)

    Returns:
        dict(str, dict): dictionary of arch_target_map which contains entries of the format
                         OS/SUBDIR : ADDITIONAL_SBATCH_PARAMETERS
    """
    fn = sys._getframe().f_code.co_name

    # cfg = config.read_config()
    architecture_targets = cfg[ARCHITECTURE_TARGETS]

    arch_target_map = json.loads(architecture_targets.get('arch_target_map'))
    log(f"{fn}(): arch target map '{json.dumps(arch_target_map)}'")
    return arch_target_map


def get_repo_cfg(cfg):
    """get repository config settings

    Args:
        cfg (dict): dictionary holding full configuration (by default defined in app.cfg)

    Returns:
        dict: dictionary with config entries
    """
    fn = sys._getframe().f_code.co_name

    global repo_cfg

    # if repo_cfg has already been initialized, just return it rather than reading it again
    if repo_cfg:
        return repo_cfg

    repo_cfg_org = cfg[REPO_TARGETS]
    repo_cfg = {}
    repo_cfg[REPOS_CFG_DIR] = repo_cfg_org.get(REPOS_CFG_DIR, None)

    repo_map = {}
    try:
        repo_map_str = repo_cfg_org.get(REPO_TARGET_MAP)
        log(f"{fn}(): repo_map '{repo_map_str}'")

        if repo_map_str is not None:
            repo_map = json.loads(repo_map_str)

        log(f"{fn}(): repo_map '{json.dumps(repo_map)}'")
    except json.JSONDecodeError as err:
        print(err)
        error(f"{fn}(): Value for repo_map ({repo_map_str}) could not be decoded.")

    repo_cfg[REPO_TARGET_MAP] = repo_map

    if repo_cfg[REPOS_CFG_DIR] is None:
        return repo_cfg

    # add entries for sections from repos.cfg (one dictionary per section)
    repos_cfg_file = os.path.join(repo_cfg[REPOS_CFG_DIR], 'repos.cfg')
    log(f"{fn}(): repos_cfg_file '{repos_cfg_file}'")
    try:
        repos_cfg = configparser.ConfigParser()
        repos_cfg.read(repos_cfg_file)
    except Exception as err:
        error(f"{fn}(): Unable to read repos config file {repos_cfg_file}!\n{err}")

    for repo_id in repos_cfg.sections():
        log(f"{fn}(): process repos.cfg section '{repo_id}'")
        if repo_id in repo_cfg:
            error(f"{fn}(): repo id '{repo_id}' in '{repos_cfg_file}' clashes with bot config")

        # repo_cfg[repo_id] = repos_cfg[repo_id]
        repo_cfg[repo_id] = {}
        for (key, val) in repos_cfg.items(repo_id):
            repo_cfg[repo_id][key] = val
            log(f"{fn}(): add ({key}:{val}) to repo_cfg[{repo_id}]")

        config_map = {}
        try:
            config_map_str = repos_cfg[repo_id].get(REPOS_CONFIG_MAP)
            log(f"{fn}(): config_map '{config_map_str}'")

            if config_map_str is not None:
                config_map = json.loads(config_map_str)

            log(f"{fn}(): config_map '{json.dumps(config_map)}'")
        except json.JSONDecodeError as err:
            print(err)
            error(f"{fn}(): Value for config_map ({config_map_str}) could not be decoded.")

        repo_cfg[repo_id][REPOS_CONFIG_MAP] = config_map

    # print full repo_cfg for debugging purposes
    log(f"{fn}(): complete repo_cfg that was just read: {json.dumps(repo_cfg, indent=4)}")

    return repo_cfg


def create_pr_dir(pr, cfg, event_info):
    """Create directory for Pull Request

    Args:
        pr (github.PullRequest.Pullrequest): object to interact with pull request
        cfg (dict): dictionary holding full configuration (by default defined in app.cfg)
        event_info (dict): event received by event_handler

    Returns:
        tuple of 3 elements containing

        - year_month (string): string with datestamp (<year>.<month>)
        - pr_id (int): pr number
        - run_dir (string): path to run_dir
    """
    # fn = sys._getframe().f_code.co_name

    # create directory structure according to alternative described in
    #   https://github.com/EESSI/eessi-bot-software-layer/issues/7
    #   jobs_base_dir/YYYY.MM/pr<id>/event_<id>/run_<id>/target_<cpuarch>

    build_env_cfg = get_build_env_cfg(cfg)
    jobs_base_dir = build_env_cfg[JOBS_BASE_DIR]

    year_month = datetime.today().strftime('%Y.%m')
    pr_id = 'pr_%s' % pr.number
    event_id = 'event_%s' % event_info['id']
    event_dir = os.path.join(jobs_base_dir, year_month, pr_id, event_id)
    # the makedirs cannot be deferred to a later os.makedirs because the
    # condition in the while loop below takes the state of the directory
    # contents into account
    os.makedirs(event_dir, exist_ok=True)

    run = 0
    while os.path.exists(os.path.join(event_dir, 'run_%03d' % run)):
        run += 1
    run_dir = os.path.join(event_dir, 'run_%03d' % run)
    os.makedirs(run_dir, exist_ok=True)

    return year_month, pr_id, run_dir


def download_pr(repo_name, branch_name, pr, arch_job_dir):
    """Download pull request to arch_job_dir

    Args:
        repo_name (string): pr base repo name
        branch_name (string): pr branch name
        pr (github.PullRequest.Pullrequest): object to interact with pull request
        arch_job_dir (string): location of arch_job_dir
    """
    # fn = sys._getframe().f_code.co_name

    # download pull request to arch_job_dir
    #  - PyGitHub doesn't seem capable of doing that (easily);
    #  - for now, keep it simple and just execute the commands (anywhere) (note 'git clone' requires that
    #    destination is an empty directory)
    #    * patching method
    #      git clone https://github.com/REPO_NAME arch_job_dir
    #      git checkout BRANCH (is stored as ref for base record in PR)
    #      curl -L https://github.com/REPO_NAME/pull/PR_NUMBER.patch > arch_job_dir/PR_NUMBER.patch
    #    (execute the next one in arch_job_dir)
    #      git am PR_NUMBER.patch
    #
    #  - REPO_NAME is repo_name
    #  - PR_NUMBER is pr.number
    git_clone_cmd = ' '.join(['git clone', f'https://github.com/{repo_name}', arch_job_dir])
    clone_output, clone_error, clone_exit_code = run_cmd(git_clone_cmd, "Clone repo", arch_job_dir)

    git_checkout_cmd = ' '.join([
        'git checkout',
        branch_name,
    ])
    checkout_output, checkout_err, checkout_exit_code = run_cmd(git_checkout_cmd,
                                                                "checkout branch '%s'" % branch_name, arch_job_dir)

    curl_cmd = f'curl -L https://github.com/{repo_name}/pull/{pr.number}.diff > {pr.number}.diff'
    curl_output, curl_error, curl_exit_code = run_cmd(curl_cmd, "Obtain patch", arch_job_dir)

    git_apply_cmd = f'git apply {pr.number}.diff'
    git_apply_output, git_apply_error, git_apply_exit_code = run_cmd(git_apply_cmd, "Apply patch", arch_job_dir)


def apply_cvmfs_customizations(cvmfs_customizations, arch_job_dir):
    """if cvmfs_customizations are defined then applies it

    Args:
        cvmfs_customizations (dictionary): maps a file name to an entry that needs to be appended to that file.
        arch_job_dir ((string): location of arch_job_dir
    """
    # fn = sys._getframe().f_code.co_name

    if len(cvmfs_customizations) > 0:
        # for each entry/key, append value to file
        for key in cvmfs_customizations.keys():
            basename = os.path.basename(key)
            jobcfgfile = os.path.join(arch_job_dir, basename)
            with open(jobcfgfile, "a") as file_object:
                file_object.write(cvmfs_customizations[key]+'\n')

            # TODO (maybe) create mappings_file to be used by
            #      bot-build.slurm to init SINGULARITY_BIND;
            #      for now, only existing mappings may be customized


def prepare_jobs(pr, cfg, event_info, action_filter):
    """prepare job directory with pull request and cfg/job.cfg as well as
       additional config files

    Args:
        pr (github.PullRequest.Pullrequest): object to interact with pull request
        cfg (dict): dictionary holding full configuration (by default defined in app.cfg)
        event_info (dict): event received by event_handler
        action_filter (EESSIBotActionFilter instance): used to filter which jobs shall be prepared

    Returns:
        jobs: list of the created jobs
    """
    fn = sys._getframe().f_code.co_name

    app_name = cfg[GITHUB].get(APP_NAME)
    build_env_cfg = get_build_env_cfg(cfg)
    arch_map = get_architecture_targets(cfg)
    repocfg = get_repo_cfg(cfg)

    base_repo_name = pr.base.repo.full_name
    log(f"{fn}(): pr.base.repo.full_name '{base_repo_name}'")

    base_branch_name = pr.base.ref
    log(f"{fn}(): pr.base.repo.ref '{base_branch_name}'")

    # create run dir (base directory for potentially several jobs)
    # TODO may still be too early (before we get to any actual job being
    #      prepared below when calling 'download_pr')
    # instead of using a run_dir, maybe just create a unique dir for each
    # job to be submitted? thus we could easily postpone the create_pr_dir
    # call to just before download_pr
    year_month, pr_id, run_dir = create_pr_dir(pr, cfg, event_info)

    jobs = []
    for arch, slurm_opt in arch_map.items():
        arch_dir = arch.replace('/', '_')
        # check if repo_target_map contains an entry for {arch}
        if arch not in repocfg[REPO_TARGET_MAP]:
            log(f"{fn}(): skipping arch {arch} because repo target map does not define repositories to build for")
            continue
        for repo_id in repocfg[REPO_TARGET_MAP][arch]:
            # ensure repocfg contains information about the repository repo_id if repo_id != EESSI-pilot
            # Note, EESSI-pilot is a bad/misleading name, it should be more like AS_IN_CONTAINER
            if repo_id != "EESSI-pilot" and repo_id not in repocfg:
                log(f"{fn}(): skipping repo {repo_id}, it is not defined in repo config {repocfg[REPOS_CFG_DIR]}")
                continue

            # if filter exists, check filter against context = (arch, repo, app_name)
            #   true --> log & go on in this iteration of for loop
            #   false --> log & continue to next iteration of for loop
            if action_filter:
                log(f"{fn}(): checking filter {action_filter.to_string()}")
                context = {"architecture": arch, "repository": repo_id, "instance": app_name}
                log(f"{fn}(): context is '{json.dumps(context, indent=4)}'")
                if not action_filter.check_filters(context):
                    log(f"{fn}(): context does NOT satisfy filter(s), skipping")
                    continue
                else:
                    log(f"{fn}(): context DOES satisfy filter(s), going on with job")
            job_dir = os.path.join(run_dir, arch_dir, repo_id)
            os.makedirs(job_dir, exist_ok=True)
            log(f"{fn}(): job_dir '{job_dir}'")

            # TODO optimisation? download once, copy and cleanup initial copy?
            download_pr(base_repo_name, base_branch_name, pr, job_dir)

            # prepare ./cfg/job.cfg
            cpu_target = '/'.join(arch.split('/')[1:])
            os_type = arch.split('/')[0]
            log(f"{fn}(): arch = '{arch}' => cpu_target = '{cpu_target}' , os_type = '{os_type}'")
            prepare_job_cfg(job_dir, build_env_cfg, repocfg, repo_id, cpu_target, os_type)

            # enlist jobs to proceed
            job = Job(job_dir, arch, repo_id, slurm_opt, year_month, pr_id)
            jobs.append(job)

    log(f"{fn}(): {len(jobs)} jobs to proceed after applying white list")
    if jobs:
        log(json.dumps(jobs, indent=4))

    return jobs


def prepare_job_cfg(job_dir, build_env_cfg, repos_cfg, repo_id, software_subdir, os_type):
    """
    Set up job configuration file 'cfg/job.cfg'

    Args:
        job_dir (string): directory of job
        build_env_cfg (dictionary): build environment configuration
        repos_cfg (dictionary):  configuration settings for all repositories
        repo_id (string):  identifier of the repository to build for
        software_subdir (string): software subdirectory to build for (CPU arch)
        os_type (string): type of the os (e.g., linux)
    """
    fn = sys._getframe().f_code.co_name

    jobcfg_dir = os.path.join(job_dir, 'cfg')
    # create ini file job.cfg with entries:
    # [site_config]
    # local_tmp = LOCAL_TMP_VALUE
    #
    # [repository]
    # repos_cfg_dir = JOB_CFG_DIR
    # repo_id = REPO_ID
    # container = CONTAINER
    # repo_name = REPO_NAME
    # repo_version = REPO_VERSION
    #
    # [architecture]
    # software_subdir = SOFTWARE_SUBDIR
    # os_type = OS_TYPE
    job_cfg = configparser.ConfigParser()
    job_cfg[JOB_SITECONFIG] = {}
    if build_env_cfg[CONTAINER_CACHEDIR]:
        job_cfg[JOB_SITECONFIG][CONTAINER_CACHEDIR] = build_env_cfg[CONTAINER_CACHEDIR]
    if build_env_cfg[LOCAL_TMP]:
        job_cfg[JOB_SITECONFIG][JOB_LOCAL_TMP] = build_env_cfg[LOCAL_TMP]
    if build_env_cfg[HTTP_PROXY]:
        job_cfg[JOB_SITECONFIG][JOB_HTTP_PROXY] = build_env_cfg[HTTP_PROXY]
    if build_env_cfg[HTTPS_PROXY]:
        job_cfg[JOB_SITECONFIG][JOB_HTTPS_PROXY] = build_env_cfg[HTTPS_PROXY]
    if build_env_cfg[LOAD_MODULES]:
        job_cfg[JOB_SITECONFIG][JOB_LOAD_MODULES] = build_env_cfg[LOAD_MODULES]

    job_cfg[JOB_REPOSITORY] = {}
    # directory for repos.cfg
    # note REPOS_CFG_DIR is a global cfg for all repositories, hence it is stored
    # in repos_cfg (not in config for a specific repository, i.e., repo_cfg)
    if REPOS_CFG_DIR in repos_cfg and repos_cfg[REPOS_CFG_DIR]:
        job_cfg[JOB_REPOSITORY][JOB_REPOS_CFG_DIR] = jobcfg_dir
    # repo id
    job_cfg[JOB_REPOSITORY][JOB_REPO_ID] = repo_id

    # settings for specific repo
    if repo_id in repos_cfg:
        repo_cfg = repos_cfg[repo_id]
        if repo_cfg[REPOS_CONTAINER]:
            job_cfg[JOB_REPOSITORY][JOB_CONTAINER] = repo_cfg[REPOS_CONTAINER]
        if repo_cfg[REPOS_REPO_NAME]:
            job_cfg[JOB_REPOSITORY][JOB_REPO_NAME] = repo_cfg[REPOS_REPO_NAME]
        if repo_cfg[REPOS_REPO_VERSION]:
            job_cfg[JOB_REPOSITORY][JOB_REPO_VERSION] = repo_cfg[REPOS_REPO_VERSION]

    job_cfg[JOB_ARCHITECTURE] = {}
    job_cfg[JOB_ARCHITECTURE][JOB_SOFTWARE_SUBDIR] = software_subdir
    job_cfg[JOB_ARCHITECTURE][JOB_OS_TYPE] = os_type

    # copy repository config bundle to directory cfg
    # TODO verify that app.cfg defines 'repos_cfg_dir'
    # copy repos_cfg[REPOS_CFG_DIR]/repos.cfg to jobcfg_dir
    # copy repos_cfg[REPOS_CFG_DIR]/*.tgz to jobcfg_dir
    if REPOS_CFG_DIR in repos_cfg and repos_cfg[REPOS_CFG_DIR] and os.path.isdir(repos_cfg[REPOS_CFG_DIR]):
        src = repos_cfg[REPOS_CFG_DIR]
        shutil.copytree(src, jobcfg_dir)
        log(f"{fn}(): copied {src} to {jobcfg_dir}")

    # make sure that job cfg dir exists
    os.makedirs(jobcfg_dir, exist_ok=True)

    jobcfg_file = os.path.join(jobcfg_dir, 'job.cfg')
    with open(jobcfg_file, "w") as jcf:
        job_cfg.write(jcf)

    # read back job cfg file so we can log contents
    with open(jobcfg_file, "r") as jcf:
        jobcfg_txt = jcf.read()
        log(f"{fn}(): created {jobcfg_file} with '{jobcfg_txt}'")


def submit_job(job, cfg):
    """Parse job id and submit jobs from directory

    Args:
        job (list): jobs to be submitted
        cfg (dict): dictionary holding full configuration (by default defined in app.cfg)

    Returns:
        tuple of 2 elements containing
            - job_id(string):  job_id of submitted job
            - symlink(string): symlink from main pr_<ID> dir to job dir (job[0])
    """
    fn = sys._getframe().f_code.co_name

    build_env_cfg = get_build_env_cfg(cfg)

    # Add a default time limit of 24h to the command if nothing else is specified by the user
    all_opts_str = " ".join([build_env_cfg[SLURM_PARAMS], job.slurm_opts])
    all_opts_list = all_opts_str.split(" ")
    if any([(opt.startswith("--time") or opt.startswith("-t")) for opt in all_opts_list]):
        time_limit = ""
    else:
        time_limit = f"--time={DEFAULT_JOB_TIME_LIMIT}"

    command_line = ' '.join([
        build_env_cfg[SUBMIT_COMMAND],
        build_env_cfg[SLURM_PARAMS],
        time_limit,
        job.slurm_opts,
        build_env_cfg[BUILD_JOB_SCRIPT],
    ])

    cmdline_output, cmdline_error, cmdline_exit_code = run_cmd(command_line,
                                                               "submit job for target '%s'" % job.arch_target,
                                                               working_dir=job.working_dir)

    # sbatch output is 'Submitted batch job JOBID'
    #   parse job id & add it to array of submitted jobs PLUS create a symlink from main pr_<ID> dir to job dir (job[0])
    log(f"{fn}(): sbatch out: {cmdline_output}")
    log(f"{fn}(): sbatch err: {cmdline_error}")

    job_id = cmdline_output.split()[3]

    symlink = os.path.join(build_env_cfg[JOBS_BASE_DIR], job.year_month, job.pr_id, job_id)
    log(f"{fn}(): create symlink {symlink} -> {job[0]}")
    os.symlink(job[0], symlink)

    return job_id, symlink


def create_pr_comment(job, job_id, app_name, pr, gh, symlink):
    """create pr comment for newly submitted job

    Args:
        job (named tuple): key data about job that has been submitted
        job_id (string): id of submitted job
        app_name (string): name of the app
        pr (github.PullRequest.Pullrequest): object to interact with pull request
        gh (object): github instance
        symlink (string): symlink from main pr_<ID> dir to job dir
    """
    fn = sys._getframe().f_code.co_name

    # obtain arch from job.arch_target which has the format OS/ARCH
    arch_name = '-'.join(job.arch_target.split('/')[1:])

    # get current data/time
    dt = datetime.now(timezone.utc)

    # construct initial job comment
    submitted_job_comments_cfg = config.read_config()[SUBMITTED_JOB_COMMENTS]
    job_comment = (f"{submitted_job_comments_cfg[INITIAL_COMMENT]}"
                   f"\n|date|job status|comment|\n"
                   f"|----------|----------|------------------------|\n"
                   f"|{dt.strftime('%b %d %X %Z %Y')}|"
                   f"submitted|"
                   f"{submitted_job_comments_cfg[AWAITS_RELEASE]}|").format(app_name=app_name,
                                                                            arch_name=arch_name,
                                                                            symlink=symlink,
                                                                            repo_id=job.repo_id,
                                                                            job_id=job_id)

    # create comment to pull request
    repo_name = pr.base.repo.full_name
    repo = gh.get_repo(repo_name)
    pull_request = repo.get_pull(pr.number)
    issue_comment = retry_call(pull_request.create_issue_comment, fargs=[job_comment],
                               exceptions=Exception, tries=3, delay=1, backoff=2, max_delay=10)
    if issue_comment:
        log(f"{fn}(): created PR issue comment with id {issue_comment.id}")
        return issue_comment
    else:
        log(f"{fn}(): failed to create PR issue comment for job {job_id}")
        return None


def submit_build_jobs(pr, event_info, action_filter):
    """Build from the pr by fetching data for build environment cofinguration, downloading pr,
       running jobs and adding comments

    Args:
        pr (github.PullRequest.Pullrequest instance): object to interact with pull request
        event_info (string): event received by event_handler
        action_filter (EESSIBotActionFilter instance): used to filter which jobs shall be prepared
    """
    fn = sys._getframe().f_code.co_name

    # retrieve some settings from 'app.cfg' in bot directory
    cfg = config.read_config()
    app_name = cfg[GITHUB].get(APP_NAME)

    # setup job directories (one per elem in product of architecture % repositories)
    jobs = prepare_jobs(pr, cfg, event_info, action_filter)

    # return if no jobs to be submitted
    if not jobs:
        log(f"{fn}(): no jobs ({len(jobs)}) to be submitted")
        return {}

    # obtain handle to GitHub
    gh = github.get_instance()

    # process prepared jobs: submit, create metadata file and add comment to PR
    job_id_to_comment_map = {}
    for job in jobs:
        # submit job
        job_id, symlink = submit_job(job, cfg)

        # report submitted job
        issue_comment = create_pr_comment(job, job_id, app_name, pr, gh, symlink)
        job_id_to_comment_map[job_id] = issue_comment

        pr_comment = pr_comments.PRComment(pr.base.repo.full_name, pr.number, issue_comment.id)

        # create _bot_job<jobid>.metadata file in submission directory
        create_metadata_file(job, job_id, pr_comment)

    # return f"created jobs: {', '.join(job_ids)}"
    return job_id_to_comment_map


def check_build_permission(pr, event_info):
    """check if the GH account is authorized to trigger build

    Args:
        pr (github.PullRequest.Pullrequest): object to interact with pull request
        event_info (string): event received by event_handler

    """
    fn = sys._getframe().f_code.co_name

    log(f"{fn}(): build for PR {pr.number}")

    cfg = config.read_config()

    buildenv = cfg[BUILDENV]

    # verify that the GH account that set label bot:build has the
    # permission to trigger the build
    build_permission = buildenv.get(BUILD_PERMISSION, '')

    log(f"{fn}(): build permission '{build_permission}'")

    build_labeler = event_info['raw_request_body']['sender']['login']
    if build_labeler not in build_permission.split():
        log(f"{fn}(): GH account '{build_labeler}' is not authorized to build")
        no_build_permission_comment = buildenv.get(NO_BUILD_PERMISSION_COMMENT)
        repo_name = event_info["raw_request_body"]["repository"]["full_name"]
        pr_comments.create_comment(repo_name,
                                   pr.number,
                                   no_build_permission_comment.format(build_labeler=build_labeler))
        return False
    else:
        log(f"{fn}(): GH account '{build_labeler}' is authorized to build")
        return True
