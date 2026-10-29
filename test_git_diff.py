import re
import unittest

from git_panel import diff_rows
from test_investigation import pry


class DiffRenderingTests(unittest.TestCase):
    def render(self, patch, width=80):
        rows = diff_rows(patch, width, pry.wrap_terminal_line, ('', '', '', '', ''))
        plain = [re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', row) for row in rows]
        return rows, plain

    def test_numbers_colors_and_headers(self):
        rows, plain = self.render('diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n'
                                  '@@ -4,2 +4,3 @@ function\n context\n-old\n+new\n+extra')
        output = '\n'.join(plain)
        self.assertIn('+2 added | -1 removed', output)
        self.assertIn('Lines 4 -> 4 function', output)
        self.assertIn('  4   4 |   context', output)
        self.assertIn('  5     | - old', output)
        self.assertIn('      5 | + new', output)
        self.assertIn('      6 | + extra', output)
        self.assertTrue(any('\033[38;5;114m' in row for row in rows))
        self.assertTrue(any('\033[38;5;203m' in row for row in rows))

    def test_wrapping_and_control_safety(self):
        _, plain = self.render('diff --git a/a b/a\n@@ -0,0 +1 @@\n+' + 'x' * 100 + '\033[2J', 30)
        self.assertTrue(all(len(row) <= 30 for row in plain))
        self.assertIn('\\x1b', ''.join(plain))
        self.assertTrue(any(row.startswith(' ' * 10) and 'x' in row for row in plain))

    def test_binary_and_plain_output(self):
        for value in ['Binary files differ', 'No text diff', 'Untracked file\n+literal text']:
            _, plain = self.render(value)
            self.assertEqual('\n'.join(plain), value)

    def test_multiple_files_reset_line_numbers(self):
        _, plain = self.render('diff --git a/a b/a\n@@ -1 +1 @@\n-a\n+b\n'
                               'diff --git a/b b/b\nnew file mode 100644\n@@ -0,0 +1 @@\n+c')
        self.assertIn('2 file(s) | +2 added | -1 removed', '\n'.join(plain))
        self.assertIn('      1 | + c', '\n'.join(plain))


if __name__ == '__main__':
    unittest.main()
