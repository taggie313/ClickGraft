"""
tests/test_salient_error_line.py — Unit tests for _salient_error_line in clickgraft/build.py.
Target: Python 3.9+
"""

from clickgraft.build import _salient_error_line

EXAMPLE_1 = """\
Command failed: clang -arch arm64 ...
Stderr: ld: tapi error: malformed file
/Library/Developer/CommandLineTools/SDKs/MacOSX27.0.sdk/usr/lib/libSystem.B.tbd:4:20: error: unknown architecture
                   arm64e.x1-macos, arm64e.x1-maccatalyst ]
                   ^~~~~~~~~~~~~~~
clang: error: linker command failed with exit code 1 (use -v to see invocation)"""

EXAMPLE_2 = """\
Command failed: clang ...
Stderr: clang: error: no such file or directory: 'shim.c'
clang: error: linker command failed with exit code 1 (use -v to see invocation)"""

EXAMPLE_3 = """\
Command failed: clang ...
Stderr: clang: error: linker command failed with exit code 1 (use -v to see invocation)"""


def test_picks_the_tapi_diagnostic_not_the_boilerplate():
    result = _salient_error_line(EXAMPLE_1)
    assert result.startswith("ld: tapi error: malformed file")
    assert "tapi" in result
    assert "linker command failed" not in result


def test_picks_a_clang_diagnostic_when_that_is_the_real_error():
    result = _salient_error_line(EXAMPLE_2)
    assert result == "clang: error: no such file or directory: 'shim.c'"


def test_falls_back_when_only_boilerplate_present():
    result = _salient_error_line(EXAMPLE_3)
    assert result
    assert "linker command failed" in result


def test_empty_and_whitespace_only_messages():
    assert _salient_error_line("") == "(no output)"
    assert _salient_error_line("\n \n") == "(no output)"


def test_truncation_marks_the_cut():
    line = "error: " + "a" * 400
    result = _salient_error_line(line, limit=110)
    assert len(result) == 110
    assert result.endswith("…")


def test_whitespace_is_collapsed():
    line = "   ld:   cannot   find   library   \t\t -lfoo   "
    result = _salient_error_line(line)
    assert result == "ld: cannot find library -lfoo"


def test_distinct_sdk_failures_produce_distinct_lines():
    blob1 = (
        "Command failed: clang -arch arm64 ...\n"
        "Stderr: ld: tapi error: malformed file /SDKs/MacOSX27.0.sdk/usr/lib/libSystem.B.tbd:4:20: error: unknown architecture\n"
        "clang: error: linker command failed with exit code 1 (use -v to see invocation)"
    )
    blob2 = (
        "Command failed: clang -arch arm64 ...\n"
        "Stderr: ld: tapi error: malformed file /SDKs/MacOSX26.0.sdk/usr/lib/libSystem.B.tbd:4:20: error: unknown architecture\n"
        "clang: error: linker command failed with exit code 1 (use -v to see invocation)"
    )
    res1 = _salient_error_line(blob1)
    res2 = _salient_error_line(blob2)
    assert res1 != res2


def test_degenerate_inputs():
    # Message with no Stderr: marker at all
    msg_no_marker = (
        "Command failed: clang -arch arm64\n"
        "clang: error: cannot find symbol"
    )
    assert _salient_error_line(msg_no_marker) == "clang: error: cannot find symbol"

    # Message that is one line
    assert _salient_error_line("fatal: something broke") == "fatal: something broke"

    # Message of only newlines
    assert _salient_error_line("\n\n\n") == "(no output)"

    # Caret markers line (^~~~~~) not preferred over real error line
    msg_carets = (
        "Command failed: clang ...\n"
        "Stderr: ^~~~~~~~~~~~~~\n"
        "some_file.c:10: error: undefined symbol\n"
        "clang: error: linker command failed with exit code 1 (use -v to see invocation)"
    )
    assert _salient_error_line(msg_carets) == "some_file.c:10: error: undefined symbol"

    # Non-ASCII bytes in path
    msg_non_ascii = (
        "Command failed: clang ...\n"
        "Stderr: ld: cannot find /Users/élise/lib.dylib\n"
        "clang: error: linker command failed with exit code 1 (use -v to see invocation)"
    )
    assert "élise" in _salient_error_line(msg_non_ascii)
