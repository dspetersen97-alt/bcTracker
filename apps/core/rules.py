"""
Permissions for the deployment's own settings.

Separate from ``accounts.manage_users`` on purpose, even though today the same
role holds both: creating an account and changing where the ministry's mail goes
out from are different powers, and the mail settings are the one page in this
application that can quietly redirect every invitation link the ministry sends.
Naming it now means the day a ministry wants an administrator who may add
counselees but not touch the mailbox, the answer is one predicate.
"""

import rules

from apps.accounts.rules import is_admin

rules.add_perm("core.manage_site_settings", is_admin)
