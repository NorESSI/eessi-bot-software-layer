# This file is part of the EESSI build-and-deploy bot,
# see https://github.com/EESSI/eessi-bot-software-layer
#
# The bot helps with requests to add software installations to the
# EESSI software layer, see https://github.com/EESSI/software-layer
#
# author: Kenneth Hoste (@boegel)
# author: Thomas Roeblitz (@trz42)
#
# license: GPLv2
#


def control_center_process_comment(request_body):
    """Processes update to a PR comment to implement control center."""
    # determine the contents of the update
    # determine if the control center needs to be updated
    # perform the update


def control_center_process_pr_opened(event_info, pr):
    """Process event "pull request opened"."""
    # add initial info for control center to PR description (if not yet done)
    #   make two functions: one for checking, one for adding
