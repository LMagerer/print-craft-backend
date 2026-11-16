from app.schemas.config import Binding
from app.services.tokens import (
    FilenameDeduper,
    apply_transform,
    extract_tokens,
    render_template,
    resolve_binding,
    sanitize_filename,
    suggest_bindings,
    validate_tokens,
)


def test_token_resolution():
    rec = {"first": "Ada", "last": "Lovelace", "n": 7.0, "addr.city": "London"}
    assert render_template("{{first}} {{ last }} #{{n}} ({{addr.city}}) {{nope}}", rec) == "Ada Lovelace #7 (London) "
    assert extract_tokens("{{a}}{{b}}{{a}}") == ["a", "b"]
    assert validate_tokens("{{a}} {{x}}", ["a", "b"]) == ["x"]


def test_resolve_binding_modes():
    rec = {"name": "Bob", "id": None}
    assert resolve_binding(Binding(mode="field", field="name"), rec) == "Bob"
    assert resolve_binding(Binding(mode="field", field="id"), rec) == ""
    assert resolve_binding(Binding(mode="template", template="Hi {{name}}"), rec) == "Hi Bob"
    # Editor mode (no record): sample text, else a token placeholder.
    assert resolve_binding(Binding(field="name"), None, "Sample") == "Sample"
    assert resolve_binding(Binding(field="name"), None) == "{{name}}"


def test_transforms():
    assert apply_transform("hello wORLD", "capitalize") == "Hello WORLD"
    assert apply_transform("straße", "uppercase") == "STRASSE"
    assert apply_transform("ABC", "lowercase") == "abc"


def test_sanitize_and_dedupe():
    assert sanitize_filename('a/b:c*?"<>|.png') == "a_b_c______.png"
    assert sanitize_filename("  ..hidden.. ") == "hidden"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("\x00\x01") == "__"
    d = FilenameDeduper()
    assert [d.claim("card", "png"), d.claim("Card", "png"), d.claim("card", "png")] == ["card.png", "Card_2.png", "card_3.png"]


def test_suggest_bindings_case_insensitive():
    assert suggest_bindings(["Name", "card no", "x"], ["NAME", "card_no", "other"]) == {"Name": "NAME", "card no": "card_no"}
