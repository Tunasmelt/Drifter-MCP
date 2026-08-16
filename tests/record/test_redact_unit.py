"""Unit tests for record/redact.py's pattern coverage and false-positive rate.

test_redaction.py (the F-04 integration test) proves the three named secret
shapes never reach disk end-to-end. These tests are narrower and faster:
they pin down what the high-entropy catch-all does and doesn't flag, since
an over-eager catch-all would make Drifter's recordings useless (every tool
name and identifier redacted) in the name of a guarantee nothing needed.
"""

from record.redact import REDACTED, redact_secrets, redact_string

PLANTED_OPENAI_KEY = "sk-" + "abcd1234EFGH5678ijkl9012MNOP3456qrst7890UVWX"
PLANTED_BEARER_TOKEN = "Bearer xT9fL2mQ8vC4nR7pW1sD6hK3jY5bE0gA"
PLANTED_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


def test_openai_key_redacted():
    assert PLANTED_OPENAI_KEY not in redact_string(f"key={PLANTED_OPENAI_KEY}")


def test_bearer_token_redacted():
    assert PLANTED_BEARER_TOKEN not in redact_string(f"Authorization: {PLANTED_BEARER_TOKEN}")


def test_jwt_redacted():
    assert PLANTED_JWT not in redact_string(f"token={PLANTED_JWT}")


def test_high_entropy_catch_all_redacts_random_token_of_unknown_shape():
    # 32 random-looking chars, no recognizable prefix/structure.
    random_token = "aQ7xM2pL9zR4vN8kT1wC6hB3jY5fD0gS"
    assert redact_string(random_token) == REDACTED


def test_ordinary_identifiers_survive_untouched():
    for benign in [
        "invoice_creation",
        "get_customer",
        "customer-42-onboarding-flow",
        "a normal English sentence with several words in it",
        "https://api.example.com/v1/customers/42",
    ]:
        assert redact_string(benign) == benign


def test_redact_secrets_recurses_through_nested_structures():
    nested = {"outer": {"inner": [PLANTED_OPENAI_KEY, {"deep": PLANTED_JWT}]}}
    result = redact_secrets(nested)
    assert result["outer"]["inner"][0] == REDACTED
    assert result["outer"]["inner"][1]["deep"] == REDACTED


def test_redact_secrets_does_not_mutate_input():
    original = {"api_key": PLANTED_OPENAI_KEY}
    redact_secrets(original)
    assert original["api_key"] == PLANTED_OPENAI_KEY
