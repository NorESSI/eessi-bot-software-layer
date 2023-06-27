#!/usr/bin/env python3
#
# (Slurm) job manager for the GitHub App for the EESSI project
#
# This tool monitors EESSI build jobs and acts on state changes of
# these jobs. It releases jobs initially held, it processes finished
# jobs and for both reports status changes/results back to the
# corresponding GitHub pull request to a software-layer repo (origin
# or fork).
#
# EESSI build jobs are recognised by
#  - being submitted in JobUserHeld status (sbatch parameter --hold)
#  - job ids listed in a specific directory (ids being symlinks to job
#    directories created by EESSI bot)
#
# This file is part of the EESSI build-and-deploy bot,
# see https://github.com/EESSI/eessi-bot-software-layer
#
# The bot helps with requests to add software installations to the
# EESSI software layer, see https://github.com/EESSI/software-layer
#
# author: Kenneth Hoste (@boegel)
# author: Bob Droege (@bedroge)
# author: Hafsa Naeem (@hafsa-naeem)
# author: Jacob Ziemke (@jacobz137)
# author: Thomas Roeblitz (@trz42)
#
# license: GPLv2
#

import os
import re
import sys
import time


from connections import github
from tools.args import job_manager_parse
from datetime import datetime, timezone
from tools import config, run_cmd
from tools.job_metadata import read_job_metadata_from_file, read_metadata_file
from tools.pr_comments import get_submitted_job_comment, update_comment

from pyghee.utils import log

AWAITS_LAUNCH = "awaits_launch"
FAILURE = "failure"
FINISHED_JOB_COMMENTS = "finished_job_comments"
NEW_JOB_COMMENTS = "new_job_comments"
MISSING_MODULES = "missing_modules"
MULTIPLE_TARBALLS = "multiple_tarballs"
NO_MATCHING_TARBALL = "no_matching_tarball"
NO_SLURM_OUT = "no_slurm_out"
NO_TARBALL_MESSAGE = "no_tarball_message"
RUNNING_JOB = "running_job"
RUNNING_JOB_COMMENTS = "running_job_comments"
SLURM_OUT = "slurm_out"
SUCCESS = "success"

JOB_RESULT_UNKNOWN_FMT = "job_result_unknown_fmt"
JOB_RESULT_COMMENT_DESCRIPTION = "comment_description"

REQUIRED_CONFIG = {
    NEW_JOB_COMMENTS: [AWAITS_LAUNCH],
    RUNNING_JOB_COMMENTS: [RUNNING_JOB],
    FINISHED_JOB_COMMENTS: [SUCCESS, FAILURE, NO_SLURM_OUT, SLURM_OUT, MISSING_MODULES,
                            NO_TARBALL_MESSAGE, NO_MATCHING_TARBALL, MULTIPLE_TARBALLS,
                            JOB_RESULT_UNKNOWN_FMT]
}


class EESSIBotSoftwareLayerJobManager:
    "main class for (Slurm) job manager of EESSI bot (separate process)"

    def __init__(self):
        cfg = config.read_config()
        job_manager_cfg = cfg['job_manager']
        self.logfile = job_manager_cfg.get('log_path')

    def get_current_jobs(self):
        # who am i
        username = os.getenv('USER', None)
        if username is None:
            raise Exception("Unable to find username")

        squeue_cmd = "%s --long --user=%s" % (self.poll_command, username)
        squeue_output, squeue_err, squeue_exitcode = run_cmd(
            squeue_cmd,
            "get_current_jobs(): squeue command",
            log_file=self.logfile,
        )

        # create dictionary of jobs
        # if any with the following information per job:
        #  jobid, state, nodelist_reason
        # skip first two lines of output ("range(2,...)")
        current_jobs = {}
        lines = str(squeue_output).rstrip().split("\n")
        bad_state_messages = {
            "F": "Failure",
            "OOM": "Out of Memory",
            "TO": "Time Out",
        }

        # get job info, logging any Slurm issues
        for i in range(2, len(lines)):
            # assume lines 2 to len(lines) contain jobs
            job = lines[i].rstrip().split()
            if len(job) >= 9:
                job_id = job[0]
                state = job[4]
                current_jobs[job_id] = {
                    "jobid": job_id,
                    "state": state,
                    "reason": job[8],
                }
                if state in bad_state_messages:
                    log("Job {} in state {}: {}".format(job_id, state, bad_state_messages[state]))

        return current_jobs

    def determine_running_jobs(self, current_jobs):
        """ determine which jobs are in running state

        Args:
            current_jobs (dict): dictionary containing data of current jobs

        Returns:
            running_jobs (list): list containing ids of running jobs
        """
        running_jobs = []
        for job in current_jobs.values():
            if job["state"] == "RUNNING":
                running_jobs.append(job["jobid"])
        return running_jobs

    # known_jobs = job_manager.get_known_jobs()
    def get_known_jobs(self):
        # find all symlinks resembling job ids (digits only) in jobdir
        known_jobs = {}
        if os.path.isdir(self.submitted_jobs_dir):
            regex = re.compile(r"(\d)+")
            for fname in os.listdir(self.submitted_jobs_dir):
                if regex.match(fname):
                    full_path = os.path.join(self.submitted_jobs_dir, fname)
                    if os.path.islink(full_path):
                        known_jobs[fname] = {"jobid": fname}
                    else:
                        log(
                            "get_known_jobs(): entry %s in %s"
                            " is not recognised as a symlink"
                            % (full_path, self.submitted_jobs_dir),
                            self.logfile,
                        )
                else:
                    log(
                        "get_known_jobs(): entry %s in %s "
                        "doesn't match regex" %
                        (fname, self.submitted_jobs_dir),
                        self.logfile,
                    )
        else:
            log(
                "get_known_jobs(): directory '%s' "
                "does not exist -> assuming no jobs known previously"
                % self.submitted_jobs_dir,
                self.logfile,
            )

        return known_jobs

    # new_jobs = job.manager.determine_new_jobs(known_jobs, current_jobs)
    def determine_new_jobs(self, known_jobs, current_jobs):
        # known_jobs is a dictionary: jobid -> {'jobid':jobid}
        # current_jobs is a dictionary: jobid -> {'jobid':jobid,
        #                                         'state':val,'reason':val}
        new_jobs = []
        for ckey in current_jobs:
            if ckey not in known_jobs:
                new_jobs.append(ckey)

        return new_jobs

    # finished_jobs = job.manager.determine_finished_jobs(known_jobs,
    #                                                     current_jobs)
    def determine_finished_jobs(self, known_jobs, current_jobs):
        # known_jobs is a dictionary: jobid -> {'jobid':jobid}
        # current_jobs is a dictionary: jobid -> {'jobid':jobid,
        #                                         'state':val,'reason':val}
        finished_jobs = []
        for kkey in known_jobs:
            if kkey not in current_jobs:
                finished_jobs.append(kkey)

        return finished_jobs

    def read_job_pr_metadata(self, job_metadata_path):
        """
        Determine metadata of a job or None.
        """
        return read_job_metadata_from_file(job_metadata_path, self.logfile)

    def read_job_result(self, job_result_file_path):
        """
        Check if result file exists, read it and return 'RESULT section if so, return None if not.
        """
        # just use a function provided by module tools.job_metadata
        result = read_metadata_file(job_result_file_path, self.logfile)
        if result and "RESULT" in result:
            return result["RESULT"]
        else:
            return None

    # job_manager.process_new_job(current_jobs[nj])
    def process_new_job(self, new_job):
        # create symlink in submitted_jobs_dir (destination is the working
        #   dir of the job derived via scontrol)
        # release job
        # update PR comment with new status (released)

        job_id = new_job["jobid"]

        scontrol_cmd = "%s --oneliner show jobid %s" % (
            self.scontrol_command,
            job_id,
        )
        scontrol_output, scontrol_err, scontrol_exitcode = run_cmd(
            scontrol_cmd,
            "process_new_job(): scontrol command",
            log_file=self.logfile,
        )

        # parse output,
        # look for WorkDir=dir
        match = re.search(r".* WorkDir=(\S+) .*",
                          str(scontrol_output))
        if match:
            log(
                "process_new_job(): work dir of job %s: '%s'"
                % (job_id, match.group(1)),
                self.logfile,
            )

            job_metadata_path = "%s/_bot_job%s.metadata" % (
                match.group(1),
                job_id,
            )

            # check if metadata file exist
            metadata_pr = self.read_job_pr_metadata(job_metadata_path)

            if metadata_pr is None:
                log(f"No metadata file found at {job_metadata_path} for job {job_id}, so skipping it",
                    self.logfile)
                return False

            symlink_source = os.path.join(self.submitted_jobs_dir, job_id)
            log(
                "process_new_job(): create a symlink: %s -> %s"
                % (symlink_source, match.group(1)),
                self.logfile,
            )
            os.symlink(match.group(1), symlink_source)

            release_cmd = "%s release %s" % (
                self.scontrol_command,
                job_id,
            )

            release_output, release_err, release_exitcode = run_cmd(
                release_cmd,
                "process_new_job(): scontrol command",
                log_file=self.logfile,
            )

            # update PR
            # (a) get repo name and PR number
            #     from file _bot_job<JOBID>.metadata
            # (b) find & get comment for this job
            # (c) add a row to the table

            # (a) get repo name and PR number from
            #     file _bot_job<JOBID>.metadata
            #   the file should be written by the event handler
            #           to the working dir of the job

            # get repo name
            repo_name = metadata_pr.get("repo", "")
            # get pr number
            pr_number = metadata_pr.get("pr_number", None)

            gh = github.get_instance()

            repo = gh.get_repo(repo_name)
            pr = repo.get_pull(int(pr_number))

            # (b) find & get comment for this job
            # only get comment if we don't know its id yet
            if "comment_id" not in new_job:
                new_job_cmnt = get_submitted_job_comment(pr, new_job['jobid'])

                if new_job_cmnt:
                    log(
                        "process_new_job(): found comment with id %s"
                        % new_job_cmnt.id,
                        self.logfile,
                    )
                    new_job["comment_id"] = new_job_cmnt.id

            # (c) add a row to the table
            # add row to status table if we found a comment
            if "comment_id" in new_job:
                new_job_comments_cfg = config.read_config()[NEW_JOB_COMMENTS]
                dt = datetime.now(timezone.utc)
                update = "\n|%s|released|" % dt.strftime("%b %d %X %Z %Y")
                update += f"{new_job_comments_cfg[AWAITS_LAUNCH]}|"
                update_comment(new_job["comment_id"], pr, update)
            else:
                log(
                    "process_new_job(): did not obtain/find a comment"
                    " for job '%s'" % job_id,
                    self.logfile,
                )
                # TODO just create one?
        else:
            # TODO can we run this tool on a job directory? the path to
            #      the directory might be obtained from
            #               a comment to the PR
            log(
                "process_new_job(): did not find work dir for job '%s'"
                % job_id,
                self.logfile,
            )

        return True

    def process_running_jobs(self, running_job):
        """process the jobs in running state and print comment

        Args:
            running_job (dict): dictionary containing data of the running jobs

        Raises:
            Exception: raise exception if there is no metadata file
        """

        gh = github.get_instance()

        # set some variables for accessing work dir of job
        job_dir = os.path.join(self.submitted_jobs_dir, running_job["jobid"])

        # TODO create function for obtaining values from metadata file
        #        might be based on allowing multiple configuration files
        #        in tools/config.py
        metadata_file = "_bot_job%s.metadata" % running_job["jobid"]
        job_metadata_path = os.path.join(job_dir, metadata_file)

        # check if metadata file exist
        metadata_pr = self.read_job_pr_metadata(job_metadata_path)
        if metadata_pr is None:
            raise Exception("Unable to find metadata file")

        # get repo name
        repo_name = metadata_pr.get("repo", "")
        # get pr number
        pr_number = metadata_pr.get("pr_number", None)

        repo = gh.get_repo(repo_name)
        pullrequest = repo.get_pull(int(pr_number))

        # determine comment to be updated
        if "comment_id" not in running_job:
            running_job_cmnt = get_submitted_job_comment(pullrequest, running_job['jobid'])

            if running_job_cmnt:
                log(
                    "process_running_job(): found comment with id %s"
                    % running_job_cmnt.id,
                    self.logfile,
                )
                running_job["comment_id"] = running_job_cmnt.id
                running_job["comment_body"] = running_job_cmnt.body

        if "comment_id" in running_job:
            dt = datetime.now(timezone.utc)
            running_job_comments_cfg = config.read_config()[RUNNING_JOB_COMMENTS]
            running_msg = running_job_comments_cfg[RUNNING_JOB].format(job_id=running_job['jobid'])
            if "comment_body" in running_job and running_msg in running_job["comment_body"]:
                log("Not updating comment, '%s' already found" % running_msg)
            else:
                update = f"\n|{dt.strftime('%b %d %X %Z %Y')}|running|"
                update += f"{running_msg}|"
                update_comment(running_job["comment_id"], pullrequest, update)
        else:
            log(
                "process_running_job(): did not obtain/find a comment"
                " for job '%s'" % running_job['jobid'],
                self.logfile,
            )

    def process_finished_job(self, finished_job):
        """Process a finished job (move symlink, log and update PR comment).

        Args:
            finished_job (dict): dictionary with information about job
        """
        fn = sys._getframe().f_code.co_name

        # PROCEDURE
        #   - MOVE symlink to finished dir
        #   - REPORT status always to log, if accessible also to PR comment

        job_id = finished_job['jobid']

        # MOVE symlink from job_ids_dir/submitted to jobs_ids_dir/finished
        old_symlink = os.path.join(self.submitted_jobs_dir, job_id)

        finished_jobs_dir = os.path.join(self.job_ids_dir, "finished")
        os.makedirs(finished_jobs_dir, exist_ok=True)

        new_symlink = os.path.join(finished_jobs_dir, job_id)

        log(f"{fn}(): os.rename({old_symlink},{new_symlink})", self.logfile)
        os.rename(old_symlink, new_symlink)

        # REPORT status (to logfile in any case, to PR comment if accessible)
        #   rely fully on what bot/check-result.sh has returned
        #   check if file _bot_jobJOBID.result exists --> if so read it and
        #   update PR comment
        # contents of *.result file (here we only use section [RESULT])
        #   [RESULT]
        #   comment_description = _FULLY_DEFINED_UPDATE_TO_PR_COMMENT_
        #   status = {SUCCESS,FAILURE,UNKNOWN}
        #   artefacts = _LIST_OF_ARTEFACTS_TO_BE_DEPLOYED_

        # obtain format templates from app.cfg
        finished_job_comments_cfg = config.read_config()[FINISHED_JOB_COMMENTS]

        # check if _bot_jobJOBID.result exits
        job_result_file = f"_bot_job{job_id}.result"
        job_result_file_path = os.path.join(new_symlink, job_result_file)
        job_results = self.read_job_result(job_result_file_path)

        # set comment_description in case no results were found (self.read_job_result
        # returned None), it's also used (reused actually) in case the job
        # results do not have a preformatted comment
        job_result_unknown_fmt = finished_job_comments_cfg[JOB_RESULT_UNKNOWN_FMT]
        comment_description = job_result_unknown_fmt.format(filename=job_result_file)
        if job_results:
            # get preformatted comment_description or use previously set default for unknown
            comment_description = job_results.get(JOB_RESULT_COMMENT_DESCRIPTION, comment_description)

        # report to log
        log(f"{fn}(): finished job {job_id}\n"
            f"########\n"
            f"comment_description: {comment_description}\n"
            f"########\n", self.logfile)

        dt = datetime.now(timezone.utc)

        comment_update = f"\n|{dt.strftime('%b %d %X %Z %Y')}|finished|"
        comment_update += f"{comment_description}|"

        # obtain id of PR comment to be updated (from _bot_jobID.metadata)
        metadata_file = f"_bot_job{job_id}.metadata"
        job_metadata_path = os.path.join(new_symlink, metadata_file)
        metadata_pr = self.read_job_pr_metadata(job_metadata_path)
        if metadata_pr is None:
            # TODO should we raise the Exception here? maybe first process
            #      the finished job and raise an exception at the end?
            raise Exception("Unable to find metadata file ... skip updating PR comment")

        # get repo name
        repo_name = metadata_pr.get("repo", None)
        # get pr number
        pr_number = metadata_pr.get("pr_number", -1)
        # get pr comment id
        pr_comment_id = metadata_pr.get("pr_comment_id", -1)
        log(f"{fn}(): pr comment id {pr_comment_id}", self.logfile)

        # establish contact to pull request on github
        gh = github.get_instance()

        repo = gh.get_repo(repo_name)
        pull_request = repo.get_pull(int(pr_number))

        update_comment(int(pr_comment_id), pull_request, comment_update)

        return


def main():
    """Main function."""

    opts = job_manager_parse()

    # config is read and checked for settings to raise an exception early when the job_manager runs.
    config.check_required_cfg_settings(REQUIRED_CONFIG)
    github.connect()

    job_manager = EESSIBotSoftwareLayerJobManager()
    job_manager.job_filter = {}
    if opts.jobs is not None:
        job_manager.job_filter = {jobid: None
                                  for jobid in opts.jobs.split(",")}

    log(
        "job manager just started, logging to '%s', processing job ids '%s'"
        % (job_manager.logfile, ",".join(job_manager.job_filter.keys())),
        job_manager.logfile,
    )
    print(
        "job manager just started, logging to '%s', processing job ids '%s'"
        % (job_manager.logfile, ",".join(job_manager.job_filter.keys()))
    )

    # before main loop, get list of known jobs (stored on disk)
    # main loop
    #  get current jobs of the bot user (job id, state, reason)
    #    (assume all are jobs building software)
    #  determine new jobs (comparing known and current jobs)
    #    process new jobs (filtered by optional command line option)
    #  determine finished jobs (comparing known and current jobs)
    #    process finished jobs (filtered by optional command line option)
    #  set known jobs to list of current jobs

    max_iter = int(opts.max_manager_iterations)
    # retrieve some settings from app.cfg
    job_manager.job_ids_dir = ""
    job_manager.submitted_jobs_dir = ""
    job_manager.poll_command = "false"
    poll_interval = 0
    job_manager.scontrol_command = ""
    if max_iter != 0:
        cfg = config.read_config()
        job_mgr = cfg["job_manager"]
        job_manager.job_ids_dir = job_mgr.get("job_ids_dir")
        job_manager.submitted_jobs_dir = os.path.join(
            job_manager.job_ids_dir, "submitted"
        )
        job_manager.poll_command = job_mgr.get("poll_command") or False
        poll_interval = int(job_mgr.get("poll_interval") or 0)
        if poll_interval <= 0:
            poll_interval = 60
        job_manager.scontrol_command = job_mgr.get("scontrol_command") or False
        os.makedirs(job_manager.submitted_jobs_dir, exist_ok=True)

    # max_iter
    #   < 0: run loop indefinitely
    #  == 0: don't run loop
    #   > 0: run loop max_iter times
    # processing may be limited to a list of job ids (see parameter -j --jobs)
    i = 0
    if max_iter != 0:
        known_jobs = job_manager.get_known_jobs()
    while max_iter < 0 or i < max_iter:
        log("job manager main loop: iteration %d" % i, job_manager.logfile)
        log(
            "job manager main loop: known_jobs='%s'" % ",".join(
                known_jobs.keys()),
            job_manager.logfile,
        )

        current_jobs = job_manager.get_current_jobs()
        log(
            "job manager main loop: current_jobs='%s'" % ",".join(
                current_jobs.keys()),
            job_manager.logfile,
        )

        new_jobs = job_manager.determine_new_jobs(known_jobs, current_jobs)
        log(
            "job manager main loop: new_jobs='%s'" % ",".join(new_jobs),
            job_manager.logfile,
        )
        # process new jobs
        non_bot_jobs = []
        for nj in new_jobs:
            # assume it is not a bot job
            is_bot_job = False
            if not job_manager.job_filter or nj in job_manager.job_filter:
                is_bot_job = job_manager.process_new_job(current_jobs[nj])
            if not is_bot_job:
                # add job id to non_bot_jobs list
                non_bot_jobs.append(nj)
            # else:
            #    log("job manager main loop: skipping new job"
            #        " %s due to parameter '--jobs %s'" % (
            #          nj,opts.jobs), job_manager.logfile)

        # remove non bot jobs from current_jobs
        for job in non_bot_jobs:
            current_jobs.pop(job)

        running_jobs = job_manager.determine_running_jobs(current_jobs)
        log(
            "job manager main loop: running_jobs='%s'" %
            ",".join(running_jobs),
            job_manager.logfile,
        )

        for rj in running_jobs:
            if not job_manager.job_filter or rj in job_manager.job_filter:
                job_manager.process_running_jobs(current_jobs[rj])

        finished_jobs = job_manager.determine_finished_jobs(
                        known_jobs, current_jobs)
        log(
            "job manager main loop: finished_jobs='%s'" %
            ",".join(finished_jobs),
            job_manager.logfile,
        )
        # process finished jobs
        for fj in finished_jobs:
            if not job_manager.job_filter or fj in job_manager.job_filter:
                job_manager.process_finished_job(known_jobs[fj])
            # else:
            #    log("job manager main loop: skipping finished "
            #        "job %s due"" to parameter '--jobs %s'" % (fj,opts.jobs),
            #        " job_manager.logfile)"

        known_jobs = current_jobs

        # sleep poll_interval seconds (only if at least one more iteration)
        if max_iter < 0 or i + 1 < max_iter:
            log(
                "job manager main loop: sleep %d seconds" % poll_interval,
                job_manager.logfile,
            )
            time.sleep(poll_interval)
        i = i + 1


if __name__ == "__main__":
    main()
