"""Nothing sensitive leaves the process in the clear."""

from __future__ import annotations

import pytest

from aihands.kernel.redaction import Redactor, redactor_for, token


@pytest.mark.parametrize("text", [
    "123-45-6789", "member ssn 123-45-6789 on file",
])
def test_social_security_numbers_are_masked(text):
    assert "123-45-6789" not in Redactor().text(text)


@pytest.mark.parametrize("card", [
    "4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
])
def test_card_numbers_are_masked(card):
    assert card not in Redactor().text(f"paid with {card} today")


@pytest.mark.parametrize("email", ["ada@example.com", "a.b+c@sub.example.co.uk"])
def test_addresses_are_masked(email):
    assert email not in Redactor().text(f"contact {email}")


@pytest.mark.parametrize("text", [
    "SAV-88200", "M-1001", "opened a savings sub-account", "500.00", "2026-01-02",
])
def test_ordinary_values_pass_through(text):
    # Over-masking makes evidence useless, which is its own kind of failure.
    assert Redactor().text(text) == text


def test_declared_secrets_are_masked_wherever_they_appear():
    r = Redactor({"operator_id": "OP-77"})
    out = r.text("operator OP-77 signed in; OP-77 approved it")
    assert "OP-77" not in out and out.count("<redacted:") == 2


def test_the_same_value_always_yields_the_same_token():
    # An incident review has to be able to correlate two runs by the same
    # operator without ever seeing who they were.
    assert token("OP-77") == token("OP-77") != token("OP-78")


def test_a_longer_secret_wins_over_one_contained_in_it():
    r = Redactor({"short": "AB", "long": "ABCD"})
    assert "ABCD" not in r.text("value ABCD here")


def test_nested_structures_are_walked():
    r = Redactor({"pin": "9137"})
    out = r.value({"a": ["9137", {"b": "pin is 9137"}], "n": 5, "ok": True})
    assert "9137" not in str(out)
    assert out["n"] == 5 and out["ok"] is True


def test_non_strings_survive_untouched():
    r = Redactor({"x": "secret"})
    assert r.value({"count": 3, "flag": False, "none": None}) == {
        "count": 3, "flag": False, "none": None}


def test_empty_and_missing_secrets_are_harmless():
    assert Redactor().text("") == ""
    assert Redactor({"a": ""}).text("nothing to hide") == "nothing to hide"


def test_a_capability_decides_what_is_sensitive(capability):
    r = redactor_for(capability, {"operator_id": "OP-77", "member_id": "M-1001"})
    out = r.text("OP-77 opened an account for M-1001")
    assert "OP-77" not in out
    assert "M-1001" in out, "only what was declared sensitive should be masked"


def test_no_capability_still_masks_by_shape():
    r = redactor_for(None, {"whatever": "123-45-6789"})
    assert "123-45-6789" not in r.text("ssn 123-45-6789")


def test_dumps_produces_valid_json():
    import json
    r = Redactor({"pin": "9137"})
    parsed = json.loads(r.dumps({"pin": "9137", "n": 1}))
    assert parsed["n"] == 1 and "9137" not in parsed["pin"]
