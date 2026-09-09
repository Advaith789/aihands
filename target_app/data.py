"""In-memory record store.

Deliberately not a database. This app is a fixture: the interesting behaviour
is the misbehaviour, and a dict we can reset in one call is the cheapest way to
make runs repeatable.

Every seeded member exists to force ONE runtime condition the replay engine has
to classify correctly. That is the reason we build our own target instead of
driving a public demo site: on someone else's site you cannot make a permission
denial happen on demand.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field, replace
from typing import Literal

MemberStatus = Literal["active", "frozen", "closed"]

# Business rules the UI enforces. Replay has to discover that these produce
# ANSWERS ("below the minimum"), not crashes.
MIN_OPENING_DEPOSIT = 25.00
MAX_OPENING_DEPOSIT = 250_000.00


@dataclass
class Account:
    number: str
    kind: str
    balance: float


@dataclass
class Member:
    member_id: str
    name: str
    status: MemberStatus
    accounts: list[Account] = field(default_factory=list)
    # Why this member is here. Not shown in the UI -- it documents the fixture.
    fixture_note: str = ""
    # Fails the first N detail loads, then succeeds. Models a transiently
    # unhealthy backend: the condition a bounded retry is supposed to absorb.
    flaky_loads_remaining: int = 0

    def has_savings(self) -> bool:
        return any(a.kind == "savings" for a in self.accounts)


_SEED: list[Member] = [
    Member(
        member_id="M-1001",
        name="Ada Whitfield",
        status="active",
        accounts=[Account("CHK-4410", "checking", 3_240.18)],
        fixture_note="happy path: the flow discovery is recorded against",
    ),
    Member(
        member_id="M-1002",
        name="Marcus Deng",
        status="frozen",
        accounts=[Account("CHK-2210", "checking", 118.40)],
        fixture_note="permission denial: the account is frozen, so opening is refused",
    ),
    Member(
        member_id="M-1003",
        name="Priya Raman",
        status="active",
        accounts=[
            Account("CHK-8801", "checking", 9_115.02),
            Account("SAV-8802", "savings", 22_400.00),
        ],
        fixture_note="already-satisfied: a savings sub-account exists, so the goal is moot",
    ),
    Member(
        member_id="M-1004",
        name="Tomas Iversen",
        status="active",
        accounts=[Account("CHK-3390", "checking", 640.75)],
        fixture_note="transient failure: first detail load 500s, second succeeds",
        flaky_loads_remaining=1,
    ),
    Member(
        member_id="M-1005",
        name="Helen Okafor",
        status="active",
        accounts=[Account("CHK-7120", "checking", 84_902.55)],
        fixture_note="dual approval: funded large enough to trip the approval interstitial",
    ),
]


class Store:
    """Mutable world state for one running instance.

    Guarded by a lock because Flask serves concurrently and a half-applied
    mutation would make a run irreproducible, which defeats the point.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._members: dict[str, Member] = {}
        self._counter = itertools.count(88_200)
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._members = {m.member_id: replace(m, accounts=list(m.accounts)) for m in _SEED}
            self._counter = itertools.count(88_200)

    def get(self, member_id: str) -> Member | None:
        return self._members.get(member_id.strip().upper())

    def consume_flaky_load(self, member: Member) -> bool:
        """Return True if this load should fail. Decrements on the way through,
        so the *same* request retried a moment later gets a different answer --
        which is what makes it a genuine transient rather than a static error."""
        with self._lock:
            if member.flaky_loads_remaining > 0:
                member.flaky_loads_remaining -= 1
                return True
            return False

    def open_savings(self, member: Member, deposit: float) -> Account:
        with self._lock:
            number = f"SAV-{next(self._counter)}"
            account = Account(number, "savings", round(deposit, 2))
            member.accounts.append(account)
            return account


store = Store()
