import pytest
import re
from orchs.supervisor._verdict import _parse_verdict

def test_parse_verdict_basic():
    assert _parse_verdict("DONE") == ("DONE", "")
    assert _parse_verdict("DONE: All good") == ("DONE", "All good")
    assert _parse_verdict("CONTINUE: Missing tests") == ("CONTINUE", "Missing tests")
    assert _parse_verdict("FIX: Typo in main.py") == ("FIX", "Typo in main.py")
    assert _parse_verdict("AWAIT_USER: What is the port?") == ("AWAIT_USER", "What is the port?")

def test_parse_verdict_case_insensitive():
    assert _parse_verdict("done") == ("DONE", "")
    assert _parse_verdict("Continue: more work") == ("CONTINUE", "more work")
    assert _parse_verdict("fix: error") == ("FIX", "error")

def test_parse_verdict_whitespace_and_newlines():
    assert _parse_verdict("  DONE  ") == ("DONE", "")
    assert _parse_verdict("\nCONTINUE:\nFirst line\nSecond line") == ("CONTINUE", "First line\nSecond line")
    assert _parse_verdict("FIX : something") == ("FIX", "something")

def test_parse_verdict_mixed_content():
    # It should find the keyword even if there's preamble
    assert _parse_verdict("Review complete. DONE") == ("DONE", "")
    assert _parse_verdict("Based on my analysis, CONTINUE: add tests") == ("CONTINUE", "add tests")
    assert _parse_verdict("Verdict: FIX - missing docs") == ("FIX", "missing docs")
    assert _parse_verdict("Decision == DONE") == ("DONE", "")
    assert _parse_verdict("Verdict: FIX --- missing docs") == ("FIX", "missing docs")
    
    # Empty instructions with separators
    assert _parse_verdict("CONTINUE:") == ("CONTINUE", "")
    assert _parse_verdict("FIX - ") == ("FIX", "")
    
    # Complex instructions
    text = "Based on the artifacts, CONTINUE: Please add the following:\n1. Unit tests\n2. Documentation"
    assert _parse_verdict(text) == ("CONTINUE", "Please add the following:\n1. Unit tests\n2. Documentation")

def test_parse_verdict_fallback():
    assert _parse_verdict("Totally unparseable output") == ("DONE", "")
    assert _parse_verdict("") == ("DONE", "")
    assert _parse_verdict(None) == ("DONE", "")

if __name__ == "__main__":
    pytest.main([__file__])
