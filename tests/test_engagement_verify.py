#!/usr/bin/env python3
"""
Unit tests for scripts/engagement_verify.py cornerstone workflow.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from engagement_verify import _title_rank


def test_title_rank_low_level() -> None:
    assert _title_rank("Coordinator") == 2
    assert _title_rank("Operations Coordinator") == 2
    assert _title_rank("Marketing Specialist") == 3
    assert _title_rank("Engineer") == 7


def test_title_rank_unranked_not_blocked() -> None:
    # Titles that are not explicitly ranked but also not senior should be neutral.
    assert _title_rank("Machinist") == 100
    assert _title_rank("Operator") == 100


def test_title_rank_senior_blocked() -> None:
    assert _title_rank("CEO") == 9999
    assert _title_rank("Chief Executive Officer") == 9999
    assert _title_rank("VP") == 9999
    assert _title_rank("Vice President") == 9999
    assert _title_rank("Director of Human Resources") == 9999
    assert _title_rank("Senior Vice President, Operations") == 9999
    assert _title_rank("Chairman and Chief Executive Officer") == 9999
    assert _title_rank("President") == 9999


def test_title_rank_coo_not_coordinator() -> None:
    # "COO" contains the substring "coo" but must be treated as a senior title,
    # not a coordinator match.
    assert _title_rank("COO") == 9999
    assert _title_rank("Chief Operating Officer") == 9999


if __name__ == "__main__":
    test_title_rank_low_level()
    test_title_rank_unranked_not_blocked()
    test_title_rank_senior_blocked()
    test_title_rank_coo_not_coordinator()
    print("all tests passed")
