"""Imports from `ledger.py`: the file that makes this fixture cross-file."""

from __future__ import annotations

from ledger import Account, OverdraftError, open_account


def transfer(source: Account, target: Account, amount: int) -> None:
    if source.balance < amount:
        raise OverdraftError(source.name)
    source.debit(amount)
    target.credit(amount)


def bootstrap(name: str) -> Account:
    account = open_account(name)
    account.credit(100)
    return account
