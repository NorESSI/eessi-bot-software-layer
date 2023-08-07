# This file is part of the EESSI build-and-deploy bot,
# see https://github.com/EESSI/eessi-bot-software-layer
#
# The bot helps with requests to add software installations to the
# EESSI software layer, see https://github.com/EESSI/software-layer
#
# author: Thomas Roeblitz (@trz42)
#
# license: GPLv2
#

# Standard library imports
import sys

# Third party imports (anything installed into the local Python environment)
from pyghee.utils import log

# Local application imports (anything from EESSI/eessi-bot-software-layer)
from tools import config

BOT_CONTROL = "bot_control"
COMMAND_PERMISSION = "command_permission"


def check_command_permission(account):
    """
    Check if the GitHub account is authorized to send commands to the bot

    Args:
        account (string): account for which permissions shall be checked

    Returns:
        True if account has command permission, False otherwise
    """
    fn = sys._getframe().f_code.co_name

    log(f"{fn}(): checking permission for sending commands")

    cfg = config.read_config()

    bot_ctrl = cfg[BOT_CONTROL]

    # verify that the GH account that has sent a command (via a adding or editing
    # a comment) has the permission to control the bot
    command_permission = bot_ctrl.get(COMMAND_PERMISSION, '')

    log(f"{fn}(): command permission '{command_permission}'")

    if account in command_permission.split():
        log(f"{fn}(): GH account '{account}' is authorized to send commands")
        return True
    else:
        log(f"{fn}(): GH account '{account}' is not authorized to send commands to the bot instance")
        return False
