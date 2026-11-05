"""Signals, sent after the surrounding transaction commits.

- ``account_connected``: sender=ConnectedAccount, account
- ``account_disconnected``: sender=ConnectedAccount, account
- ``publication_status_changed``: sender=Publication, publication, previous_status
"""

from django.dispatch import Signal

account_connected = Signal()
account_disconnected = Signal()
publication_status_changed = Signal()
