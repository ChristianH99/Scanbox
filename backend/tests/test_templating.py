from scanbox.destinations.templating import render_template


class Secret:
    password = "hunter2"  # noqa: S105

    def __str__(self) -> str:
        return "obj"


def test_substitutes_known_tokens():
    assert (
        render_template("Scan: {title} ({pages} p.)", {"title": "Invoice", "pages": 3})
        == "Scan: Invoice (3 p.)"
    )


def test_unknown_tokens_left_verbatim():
    assert render_template("{title} {nope} {}", {"title": "X"}) == "X {nope} {}"


def test_escapes():
    assert render_template("{{title}} {{{title}}} }}", {"title": "X"}) == "{title} {X} }"


def test_unbalanced_braces_are_harmless():
    assert render_template("{title", {"title": "X"}) == "{title"
    assert render_template("a } b {", {}) == "a } b {"


def test_no_attribute_or_index_access_or_format_spec():
    vars_ = {"obj": Secret(), "lst": [1, 2], "title": "T"}
    assert render_template("{obj.password}", vars_) == "{obj.password}"
    assert (
        render_template("{obj.__class__.__init__.__globals__}", vars_)
        == "{obj.__class__.__init__.__globals__}"
    )
    assert render_template("{lst[0]}", vars_) == "{lst[0]}"
    assert render_template("{title!r}", vars_) == "{title!r}"
    assert render_template("{title:>99999999}", vars_) == "{title:>99999999}"
    assert render_template("{obj}", vars_) == "obj"


def test_values_are_not_reinterpreted():
    # A substituted value containing braces is not rendered again.
    assert render_template("{a}", {"a": "{b}", "b": "no"}) == "{b}"


def test_none_renders_empty():
    assert render_template("[{qr}]", {"qr": None}) == "[]"
