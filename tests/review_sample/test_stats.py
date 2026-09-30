import unittest

from stats import average, largest


class StatsTests(unittest.TestCase):
    def test_largest(self):
        self.assertEqual(largest([1, 5, 3]), 5)

    def test_average(self):
        self.assertEqual(average([2, 4]), 3)


if __name__ == "__main__":
    unittest.main()
