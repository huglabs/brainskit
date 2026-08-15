"""The definitions `postings.py` imports, so the corpus has a cross-file edge."""

from __future__ import annotations


class Account:
    """A balance that can be credited and debited."""

    def __init__(self, name: str, balance: int = 0) -> None:
        self.name = name
        self.balance = balance

    def credit(self, amount: int) -> int:
        self.balance += amount
        return self.balance

    def debit(self, amount: int) -> int:
        self.balance -= amount
        return self.balance


class OverdraftError(Exception):
    """Raised when a debit would take an account below zero."""


def open_account(name: str) -> Account:
    return Account(name)
