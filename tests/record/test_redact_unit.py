"""Unit tests for record/redact.py's pattern coverage and false-positive rate.

test_redaction.py (the F-04 integration test) proves the three named secret
shapes never reach disk end-to-end. These tests are narrower and faster:
they pin down what the high-entropy catch-all does and doesn't flag, since
an over-eager catch-all would make Drifter's recordings useless (every tool
name and identifier redacted) in the name of a guarantee nothing needed.
"""

from mcp_drifter.record.redact import is_redaction_marker, redact_rpc_payload, redact_secrets, redact_string

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
    assert is_redaction_marker(redact_string(random_token))


# --- F-10 fix: the marker distinguishes different secrets ------------------
# Previously a real, documented limitation (FEATURES.md F-10, fixed this
# round): every secret redacted to the SAME fixed "[REDACTED]" placeholder,
# so `drifter stats`' retry-rate heuristic (identical-consecutive-arguments)
# misdetected two calls using two DIFFERENT real secrets as a retry. Fixed by
# making the marker a deterministic hash of the matched value instead of a
# constant -- these tests pin down the two properties that actually matter:
# different secrets must produce different markers (the fix), and the SAME
# secret must always produce the SAME marker (required for `drifter stats`'
# retry detection AND `replay/replay_store.py`'s tier-1 exact-key replay to
# keep working at all -- see redact.py's own module docstring for why this
# can't be salted).


def test_two_different_secrets_redact_to_different_markers():
    key_a = "sk-" + "a" * 30
    key_b = "sk-" + "b" * 30
    assert redact_string(key_a) != redact_string(key_b)


def test_the_same_secret_always_redacts_to_the_same_marker():
    assert redact_string(PLANTED_OPENAI_KEY) == redact_string(PLANTED_OPENAI_KEY)
    assert redact_secrets({"k": PLANTED_JWT}) == redact_secrets({"k": PLANTED_JWT})


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
    assert is_redaction_marker(result["outer"]["inner"][0])
    assert is_redaction_marker(result["outer"]["inner"][1]["deep"])


def test_redact_secrets_does_not_mutate_input():
    original = {"api_key": PLANTED_OPENAI_KEY}
    redact_secrets(original)
    assert original["api_key"] == PLANTED_OPENAI_KEY


def test_non_string_non_container_values_pass_through_unchanged():
    """redact_secrets recurses into dict/list and redacts str -- every
    other type (int, bool, None, float) has no string to match against
    and must survive completely untouched, not be stringified or
    dropped."""
    for value in (42, True, False, None, 3.14):
        assert redact_secrets(value) == value
        assert redact_secrets({"k": value})["k"] == value


def test_a_secret_embedded_in_surrounding_text_preserves_the_rest():
    """redact_string does a substring substitution (pattern.sub), not a
    whole-string replace-if-matched -- confirmed here with exact expected
    output, not just "the secret is gone" (already covered) but "the
    surrounding benign text survives," which matters for keeping redacted
    recordings actually useful to read."""
    text = f"Please use this key: {PLANTED_OPENAI_KEY} when calling the API."
    result = redact_string(text)
    assert result.startswith("Please use this key: [REDACTED:")
    assert result.endswith("] when calling the API.")
    assert PLANTED_OPENAI_KEY not in result


def test_empty_string_and_empty_containers_do_not_crash():
    assert redact_string("") == ""
    assert redact_secrets({}) == {}
    assert redact_secrets([]) == []
    assert redact_secrets({"nested": {"empty_list": []}}) == {"nested": {"empty_list": []}}


# --- redact_rpc_payload: structural scoping (F-03's raw-mirror path) --------
# Previously untested directly -- only exercised indirectly through
# test_redaction.py's end-to-end planted-secret check, which never
# confirmed the ENVELOPE fields (jsonrpc/id/method) survive untouched
# specifically because they're structurally exempt, not because no
# secret-shaped string happened to land there.


def test_redact_rpc_payload_only_touches_params_result_and_error_data():
    raw = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"api_key": PLANTED_OPENAI_KEY},
        "result": {"token": PLANTED_JWT},
    }
    redacted = redact_rpc_payload(raw)
    assert redacted["jsonrpc"] == "2.0"  # envelope fields untouched
    assert redacted["id"] == 7
    assert redacted["method"] == "tools/call"
    assert is_redaction_marker(redacted["params"]["api_key"])
    assert is_redaction_marker(redacted["result"]["token"])


def test_redact_rpc_payload_redacts_error_data_specifically():
    raw = {
        "jsonrpc": "2.0",
        "id": 8,
        "error": {"code": -32000, "message": "failed", "data": {"leaked": PLANTED_BEARER_TOKEN}},
    }
    redacted = redact_rpc_payload(raw)
    assert redacted["error"]["code"] == -32000  # structural error fields untouched
    assert redacted["error"]["message"] == "failed"
    assert is_redaction_marker(redacted["error"]["data"]["leaked"])


def test_redact_rpc_payload_does_not_mutate_the_input_dict():
    raw = {"jsonrpc": "2.0", "id": 1, "params": {"api_key": PLANTED_OPENAI_KEY}}
    redact_rpc_payload(raw)
    assert raw["params"]["api_key"] == PLANTED_OPENAI_KEY


def test_redact_rpc_payload_handles_a_message_with_none_of_the_optional_fields():
    """A bare request/response with no params, result, or error at all
    (e.g. a notification) must pass through without KeyError."""
    raw = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert redact_rpc_payload(raw) == raw
