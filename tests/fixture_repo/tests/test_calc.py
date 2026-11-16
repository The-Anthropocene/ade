# id: tests.test_calc
# what: covers app.calc
# cmd: python3 -m unittest tests/test_calc.py
# reads: -
# writes: -
import unittest

from app.calc import add


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
