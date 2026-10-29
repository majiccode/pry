import contextlib
import io
import os
import unittest
from unittest.mock import patch

from git_panel import GitPanel
from network_panel import NetworkPanel
from test_investigation import pry


class HeaderTests(unittest.TestCase):
    def test_header_on_every_screen(self):
        picker = dict(top=0, index=0, models=['example'], message='Ready')
        browser = dict(directory='/', selected=[], top=0, index=0, entries=[], message='', path_input=None)
        for width in (20, 80, 120):
            for panel, files, models in [(None, None, None), (GitPanel('.', False), None, None),
                                          (NetworkPanel('.'), None, None), (None, browser, None),
                                          (None, None, picker)]:
                with self.subTest(width=width, panel=type(panel).__name__, files=bool(files), models=bool(models)):
                    output = io.StringIO()
                    with patch.object(pry.os, 'get_terminal_size', return_value=os.terminal_size((width, 30))), \
                            patch.object(pry, 'git_panel', panel), patch.object(pry, 'browser', files), \
                            patch.object(pry, 'model_picker', models), contextlib.redirect_stdout(output):
                        pry.render_screen()
                    frame = output.getvalue()
                    self.assertIn('Pry', frame)
                    self.assertIn('\033[38;5;208m', frame)
                    if panel:
                        self.assertTrue(all(row > 1 for row, *_ in panel.zones))
                        self.assertTrue(all(row <= 30 for row, *_ in panel.zones))
                    else:
                        self.assertTrue(all(row > 1 for row, _, _ in pry.click_zones)
                                        if files or models else True)

    def test_panel_form_hitbox_is_shifted(self):
        panel = GitPanel('.', False)
        panel.form = dict(summary='Commit', fields=[('Message', 'test')], index=0)
        frame = panel.draw(80, 30, pry.wrap_terminal_line, ('', '', '', '', ''), top=1)
        row, left, _, action, _ = panel.zones[0]
        self.assertEqual(action, 'submit')
        self.assertIn(f'\033[{row};{left}H[Apply]', frame)


if __name__ == '__main__':
    unittest.main()
