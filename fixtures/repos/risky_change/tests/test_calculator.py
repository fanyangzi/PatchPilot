import pytest
from src.calculator import divide

def test_divide_zero_is_explicit():
    with pytest.raises(ZeroDivisionError): divide(10, 0)

def test_divide_regular():
    assert divide(10, 2) == 5
