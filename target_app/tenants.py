"""Tenant configuration.

Many institutions run the same vendor product with different branding and
different words for the same thing. Two tenants is the smallest number that
proves a capability recorded against one can run against the other, so it is
the number we ship.

Only vocabulary and branding vary here. Step ORDER varies too (see
`confirm_before_review`), because a tenant that merely restyles the same pages
would not test anything a CSS change could not fake.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    key: str
    display_name: str
    # The same concept, named differently per institution. These strings end up
    # as accessible names, which is exactly what a recorded locator keys on --
    # so this is the variance a cross-tenant overlay has to absorb.
    member_word: str          # "Member" vs "Shareholder"
    member_id_label: str
    search_button: str
    subaccount_word: str
    open_button: str
    amount_label: str
    submit_button: str
    # Dual approval threshold in dollars. Different institutions set different
    # limits, which changes WHICH runs hit the approval path at all.
    approval_threshold: float


TENANTS: dict[str, Tenant] = {
    "mendota": Tenant(
        key="mendota",
        display_name="Lake Mendota Credit Union",
        member_word="Member",
        member_id_label="Member ID",
        search_button="Find Member",
        subaccount_word="Sub-Account",
        open_button="Open Sub-Account",
        amount_label="Opening Deposit",
        submit_button="Submit Request",
        approval_threshold=10_000.00,
    ),
    "presidio": Tenant(
        key="presidio",
        display_name="Presidio Federal Credit Union",
        member_word="Shareholder",
        member_id_label="Shareholder Number",
        search_button="Locate",
        subaccount_word="Linked Account",
        open_button="Create Linked Account",
        amount_label="Initial Funding",
        submit_button="Send for Processing",
        approval_threshold=2_500.00,
    ),
}


def get_tenant(key: str) -> Tenant:
    if key not in TENANTS:
        raise KeyError(f"unknown tenant {key!r}; have {sorted(TENANTS)}")
    return TENANTS[key]
