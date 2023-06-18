# This file is part of the EESSI build-and-deploy bot,
# see https://github.com/EESSI/eessi-bot-software-layer
#
# The bot helps with requests to add software installations to the
# EESSI software layer, see https://github.com/EESSI/software-layer
#
# author: Thomas Roeblitz (@trz42)
# author: Jonas Qvigstad (@jonas-lq)
#
# license: GPLv2
#
import glob
import os
import re
import sys

from connections import github
from datetime import datetime, timezone
from pyghee.utils import log
from tasks.build import get_build_env_cfg
from tools import config, run_cmd, pr_comments
from tools.job_metadata import read_job_metadata_from_file

JOBS_BASE_DIR = "jobs_base_dir"
DEPLOYCFG = "deploycfg"
TARBALL_UPLOAD_SCRIPT = "tarball_upload_script"
ENDPOINT_URL = "endpoint_url"
BUCKET_NAME = "bucket_name"
UPLOAD_POLICY = "upload_policy"
DEPLOY_PERMISSION = "deploy_permission"
NO_DEPLOY_PERMISSION_COMMENT = "no_deploy_permission_comment"


def determine_job_dirs(pr_number):
    """Determine job directories of a pull request.

    Args:
        pr_number (string): number of the pull request

    Returns:
        job_directories (list): list of directory names
    """
    funcname = sys._getframe().f_code.co_name

    job_directories = []

    # job directories are any job IDs under JOBS_BASE_DIR/YYYY.MM/pr_<id>
    #  - we may have to scan multiple YYYY.MM directories if the PR was
    #    processed over more than one month
    #  - we assume that job IDs are positive integer numbers
    cfg = config.read_config()
    build_env_cfg = get_build_env_cfg(cfg)
    jobs_base_dir = build_env_cfg[JOBS_BASE_DIR]
    log(f"{funcname}(): jobs_base_dir = {jobs_base_dir}")

    date_pr_job_pattern = (f"[0-9][0-9][0-9][0-9].[0-9][0-9]/"
                           f"pr_{pr_number}/[0-9]*")
    log(f"{funcname}(): date_pr_job_pattern = {date_pr_job_pattern}")

    glob_str = os.path.join(jobs_base_dir, date_pr_job_pattern)
    log(f"{funcname}(): glob_str = {glob_str}")

    job_directories = glob.glob(glob_str)

    return job_directories


def determine_slurm_out(job_dir):
    """Determine path to slurm*out file for a job given by a job directory.

    Args:
        job_dir (string): directory of a job

    Returns:
        slurm_out (string): path to slurm*out file
    """
    funcname = sys._getframe().f_code.co_name

    # we assume that the last element (basename) is the job ID
    slurm_out = os.path.join(job_dir, f"slurm-{os.path.basename(job_dir)}.out")
    log(f"{funcname}(): slurm out path = '{slurm_out}'")

    return slurm_out


def determine_eessi_tarballs(job_dir):
    """Determine EESSI tarballs.

    Args:
        job_dir (string): directory of a job

    Returns:
        eessi_tarballs (list): list of paths to all tarballs in job_dir
    """
    # determine all tarballs that are stored in the directory job_dir
    #   and whose name matches a certain pattern
    tarball_pattern = "eessi-*software-*.tar.gz"
    glob_str = os.path.join(job_dir, tarball_pattern)
    eessi_tarballs = glob.glob(glob_str)

    return eessi_tarballs


def check_build_status(slurm_out, eessi_tarballs):
    """Check status of the job in a given directory.

    Args:
        slurm_out (string): path to slurm*out file
        eessi_tarballs (list): list of eessi tarballs found for job

    Returns:
        (bool): True -> job succeeded, False -> job failed
    """
    fn = sys._getframe().f_code.co_name

    # TODO use _bot_job<SLURM_JOBID>.result file to determine result status
    # cases:
    # (1) no result file --> add line with unknown status, found tarball xyz but no result file
    # (2) result file && status = SUCCESS --> return True
    # (3) result file && status = FAILURE --> return False

    # Function checks if all modules have been built and if a tarball has
    # been created.

    # set some initial values
    no_missing_modules = False
    targz_created = False

    # check slurm out for the below strings
    #   ^No missing modules!$ --> all software successfully installed
    #   ^/eessi_bot_job/eessi-.*-software-.*.tar.gz created!$ -->
    #     tarball successfully created
    if os.path.exists(slurm_out):
        re_missing_modules = re.compile(".*No missing installations, party time!.*")
        re_targz_created = re.compile("^/eessi_bot_job/eessi-.*-software-.*.tar.gz created!$")
        outfile = open(slurm_out, "r")
        for line in outfile:
            if re_missing_modules.match(line):
                # no missing modules
                no_missing_modules = True
                log(f"{fn}(): line '{line}' matches '.*No missing installations, party time!.*'")
            if re_targz_created.match(line):
                # tarball created
                targz_created = True
                log(f"{fn}(): line '{line}' matches '^/eessi_bot_job/eessi-.*-software-.*.tar.gz created!$'")

    log(f"{fn}(): found {len(eessi_tarballs)} tarballs for '{slurm_out}'")

    # we test results from the above check and if there is one tarball only
    if no_missing_modules and targz_created and len(eessi_tarballs) == 1:
        return True

    return False


def update_pr_comment(tarball, repo_name, pr_number, pr_comment_id, state, msg):
    """Update PR comment which contains specific tarball name.

    Args:
        tarball (string): name of tarball that is looked for in a PR comment
        repo_name (string): name of the repository (USER_ORG/REPOSITORY)
        pr_number (string): pull request number
        pr_comment_id (int): pull request comment id
        state (string): state (upload) to be used in update
        msg (string): msg (succeeded or failed) describing upload result
    """
    gh = github.get_instance()
    repo = gh.get_repo(repo_name)
    pull_request = repo.get_pull(pr_number)

    # adjust search string ".*{tarball}.*" if format of PR comment changed by event handler
    issue_comment = pr_comments.determine_issue_comment(pull_request, pr_comment_id, tarball)
    if issue_comment:
        dt = datetime.now(timezone.utc)
        comment_update = (f"\n|{dt.strftime('%b %d %X %Z %Y')}|{state}|"
                          f"transfer of `{tarball}` to S3 bucket {msg}|")

        # append update to existing comment
        issue_comment.edit(issue_comment.body + comment_update)


def append_tarball_to_upload_log(tarball, job_dir):
    """Append tarball to upload log.

       Args:
           tarball (string): name of tarball that has been uploaded
           job_dir (string): directory of the job that built the tarball
    """
    # upload log file is 'job_dir/../uploaded.txt'
    pr_base_dir = os.path.dirname(job_dir)
    uploaded_txt = os.path.join(pr_base_dir, 'uploaded.txt')
    with open(uploaded_txt, "a") as upload_log:
        job_plus_tarball = os.path.join(os.path.basename(job_dir), tarball)
        upload_log.write(f"{job_plus_tarball}\n")


def upload_tarball(job_dir, build_target, timestamp, repo_name, pr_number, pr_comment_id):
    """Upload built artefact to an S3 bucket.

    Args:
        job_dir (string): path to the job directory
        build_target (string): eessi-VERSION-COMPONENT-OS-ARCH
        timestamp (int): timestamp of the tarball
        repo_name (string): repository of the pull request
        pr_number (int): number of the pull request
        pr_comment_id (int): id of the pull request comment
    """
    funcname = sys._getframe().f_code.co_name

    tarball = f"{build_target}-{timestamp}.tar.gz"
    abs_path = os.path.join(job_dir, tarball)
    log(f"{funcname}(): deploying build '{abs_path}'")

    # obtain config settings
    cfg = config.read_config()
    deploycfg = cfg[DEPLOYCFG]
    tarball_upload_script = deploycfg.get(TARBALL_UPLOAD_SCRIPT)
    endpoint_url = deploycfg.get(ENDPOINT_URL) or ''
    bucket_name = deploycfg.get(BUCKET_NAME)

    # run 'eessi-upload-to-staging {abs_path}'
    # (1) construct command line
    #   script assumes a few defaults:
    #     bucket_name = 'eessi-staging'
    #     if endpoint_url not set use EESSI S3 bucket
    # (2) run command
    cmd_args = [tarball_upload_script, ]
    if len(bucket_name) > 0:
        cmd_args.extend(['--bucket-name', bucket_name])
    if len(endpoint_url) > 0:
        cmd_args.extend(['--endpoint-url', endpoint_url])
    cmd_args.extend(['--repository', repo_name])
    cmd_args.extend(['--pull-request', str(pr_number)])
    cmd_args.extend(['--pr-comment-id', str(pr_comment_id)])
    cmd_args.append(abs_path)
    upload_cmd = ' '.join(cmd_args)

    # run_cmd does all the logging we might need
    out, err, ec = run_cmd(upload_cmd, 'Upload tarball to S3 bucket', raise_on_error=False)

    if ec == 0:
        # add file to 'job_dir/../uploaded.txt'
        append_tarball_to_upload_log(tarball, job_dir)
        # update pr comment
        update_pr_comment(tarball, repo_name, pr_number, "uploaded",
                          "succeeded")
    else:
        # update pr comment
        update_pr_comment(tarball, repo_name, pr_number, "not uploaded",
                          "failed")


def uploaded_before(build_target, job_dir):
    """Determines if a tarball for a job has been uploaded before.
       Function scans the log named 'job_dir/../uploaded.txt' for
       the string '.*build_target-.*.tar.gz'.

    Args:
        build_target (string): eessi-VERSION-COMPONENT-OS-ARCH
        job_dir (string): directory of the job

    Returns:
        (string): name of the first tarball found if any or None.
    """
    funcname = sys._getframe().f_code.co_name

    log(f"{funcname}(): any previous uploads for {build_target}?")

    pr_base_dir = os.path.dirname(job_dir)
    uploaded_txt = os.path.join(pr_base_dir, "uploaded.txt")

    if os.path.exists(uploaded_txt):
        log(f"{funcname}(): upload log '{uploaded_txt}' exists")

        re_string = f".*{build_target}-.*.tar.gz.*"
        re_build_target = re.compile(re_string)

        with open(uploaded_txt, "r") as uploaded_log:
            log(f"{funcname}(): scan log for pattern '{re_string}'")
            for line in uploaded_log:
                if re_build_target.match(line):
                    log(f"{funcname}(): found earlier upload {line.strip()}")
                    return line.strip()
                else:
                    log(f"{funcname}(): upload '{line.strip()}' did NOT match")
            return None
    else:
        log(f"{funcname}(): upload log '{uploaded_txt}' does not exist")
        return None


def determine_successful_jobs(job_dirs):
    """Determine all successful jobs provided in job_dirs.

    Args:
        job_dirs (list): list of job directories

    Returns:
        successful_jobs (list): list of dictionaries representing successful jobs
    """
    funcname = sys._getframe().f_code.co_name

    successful_jobs = []
    for job_dir in job_dirs:
        slurm_out = determine_slurm_out(job_dir)
        eessi_tarballs = determine_eessi_tarballs(job_dir)
        pr_comment_id = determine_pr_comment_id(job_dir)

        if check_build_status(slurm_out, eessi_tarballs):
            log(f"{funcname}(): SUCCESSFUL build in '{job_dir}'")
            successful_jobs.append({'job_dir': job_dir,
                                    'slurm_out': slurm_out,
                                    'pr_comment_id': pr_comment_id,
                                    'eessi_tarballs': eessi_tarballs})
        else:
            log(f"{funcname}(): FAILED build in '{job_dir}'")

    return successful_jobs


def determine_pr_comment_id(job_dir):
    """Determines pr_comment_id by reading _bot_job{JOBID}.metadata in job_dir."""
    # assumes that last part of job_dir encodes the job's id
    job_id = os.path.basename(os.path.normpath(job_dir))
    job_metadata_file = os.path.join(job_dir, f"_bot_job{job_id}.metadata")
    job_metadata = read_job_metadata_from_file(job_metadata_file)
    if job_metadata and "pr_comment_id" in job_metadata:
        return int(job_metadata["pr_comment_id"])
    else:
        return -1


def determine_tarballs_to_deploy(successful_jobs, upload_policy):
    """Determines tarballs to deploy depending on upload policy

    Args:
        successful_jobs (list): list of dictionaries
                          {job_dir, slurm_out, eessi_tarballs, pr_comment_id}
        upload_policy (string): one of 'all', 'latest' or 'once'
            'all': deploy all
            'latest': deploy only the last for each build target
            'once': deploy only latest if none for this build target has
                    been deployed before
    Returns:
        to_be_deployed (dictionary): dictionary of dictionaries
                                     {job_dir, pr_comment_id, timestamp}
    """
    funcname = sys._getframe().f_code.co_name

    log(f"{funcname}(): num successful jobs {len(successful_jobs)}")

    to_be_deployed = {}
    for job in successful_jobs:
        # all tarballs for successful job
        tarballs = job["eessi_tarballs"]
        log(f"{funcname}(): num tarballs {len(tarballs)}")

        # full path to first tarball for successful job
        # Note, only one tarball per job is expected.
        tb0 = tarballs[0]
        log(f"{funcname}(): path to 1st tarball: '{tb0}'")

        # name of tarball file only
        tb0_base = os.path.basename(tb0)
        log(f"{funcname}(): tarball filename: '{tb0_base}'")

        # tarball name format: eessi-VERSION-COMPONENT-OS-ARCH-TIMESTAMP.tar.gz
        # remove "-TIMESTAMP.tar.gz"
        # build_target format: eessi-VERSION-COMPONENT-OS-ARCH
        build_target = "-".join(tb0_base.split("-")[:-1])
        log(f"{funcname}(): tarball build target '{build_target}'")

        # timestamp in the filename
        timestamp = int(tb0_base.split("-")[-1][:-7])
        log(f"{funcname}(): tarball timestamp {timestamp}")

        deploy = False
        if upload_policy == "all":
            deploy = True
        elif upload_policy == "latest":
            if build_target in to_be_deployed:
                if to_be_deployed[build_target]["timestamp"] < timestamp:
                    # current one will be replaced
                    deploy = True
            else:
                deploy = True
        elif upload_policy == "once":
            uploaded = uploaded_before(build_target, job["job_dir"])
            if uploaded is None:
                deploy = True
            else:
                indent_fname = f"{' '*len(funcname + '(): ')}"
                log(f"{funcname}(): tarball for build target '{build_target}'\n"
                    f"{indent_fname}has been uploaded through '{uploaded}'")

        if deploy:
            to_be_deployed[build_target] = {"job_dir": job["job_dir"],
                                            "pr_comment_id": job["pr_comment_id"],
                                            "timestamp": timestamp}

    return to_be_deployed


def deploy_built_artefacts(pr, event_info):
    """Deploy built artefacts.

    Args:
        pr (PullRequest): object representing a pull request
        event_info (dict): dictionary containing event information
    """
    funcname = sys._getframe().f_code.co_name

    log(f"{funcname}(): deploy for PR {pr.number}")

    cfg = config.read_config()
    deploy_cfg = cfg[DEPLOYCFG]
    deploy_permission = deploy_cfg.get(DEPLOY_PERMISSION, '')
    log(f"{funcname}(): deploy permission '{deploy_permission}'")

    labeler = event_info['raw_request_body']['sender']['login']

    # verify that the GH account that set label bot:deploy has the
    # permission to trigger the deployment
    if labeler not in deploy_permission.split():
        log(f"{funcname}(): GH account '{labeler}' is not authorized to deploy")
        no_deploy_permission_comment = deploy_cfg.get(NO_DEPLOY_PERMISSION_COMMENT)
        repo_name = event_info["raw_request_body"]["repository"]["full_name"]
        pr_comments.create_comment(repo_name,
                                   pr.number,
                                   no_deploy_permission_comment.format(deploy_labeler=labeler))
        return
    else:
        log(f"{funcname}(): GH account '{labeler}' is authorized to deploy")

    # get upload policy from config
    upload_policy = deploy_cfg.get(UPLOAD_POLICY)
    log(f"{funcname}(): upload policy '{upload_policy}'")

    if upload_policy == "none":
        return

    # 1) determine what has been built for the PR
    # 2) for each build check the status of jobs (SUCCESS or FAILURE)
    # 3) for the successful ones, determine which to deploy depending on policy
    # 4) call function to deploy a single artefact per software subdir

    # 1) determine what has been built for the PR
    #    - each job is stored in a directory given by its job ID
    #    - these job directories are stored under jobs_base_dir/YYYY.MM/pr_<id>
    #    - multiple YYYY.MM directories may need to be scanned
    job_dirs = determine_job_dirs(pr.number)
    log(f"{funcname}(): job_dirs = {','.join(job_dirs)}")

    # 2) for each build check the status of jobs (SUCCESS or FAILURE)
    #    - scan slurm*out file for: 'No modules missing!' & 'created'
    successful_jobs = determine_successful_jobs(job_dirs)

    # 3) for the successful ones, determine which to deploy depending on
    #    the upload policy
    to_be_deployed = determine_tarballs_to_deploy(successful_jobs, upload_policy)

    # 4) call function to deploy a single artefact per software subdir
    #    - update PR comments (look for comments with build-ts.tar.gz)
    repo_name = pr.base.repo.full_name

    for target, job in to_be_deployed.items():
        job_dir = job['job_dir']
        timestamp = job['timestamp']
        pr_comment_id = job['pr_comment_id']
        upload_tarball(job_dir, target, timestamp, repo_name, pr.number, pr_comment_id)
