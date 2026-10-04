import pytest
from src.parser import parse_pair

def test_parse_pair_requires_both_sides():
    with pytest.raises(ValueError): parse_pair('left:')

def test_parse_pair():
    assert parse_pair('left:right') == ('left', 'right')
