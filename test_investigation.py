import importlib.util
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import investigation


spec = importlib.util.spec_from_file_location('pry', Path(__file__).with_name('ai-tool.py'))
pry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pry)


class InvestigationTests(unittest.TestCase):
    def session(self):
        session = pry.Conversation(lambda messages, token: None)
        session.command_sender = lambda command, token: None
        session.investigation = investigation.create('Why is localhost:3000 unreachable?')
        return session

    def test_targets(self):
        for target, host in [('localhost:3000', 'localhost'), ('[::1]:3000', '::1'),
                             ('[::ffff:127.0.0.1]:3000', '::ffff:127.0.0.1'),
                             ('https://example.com:443/path', 'example.com')]:
            self.assertEqual(investigation.create(target)['host'], host)
        for target in ['localhost', 'host:0', 'host:65536', 'a:80 b:90',
                       '::1:3000', 'https://my+service.example:443', 'host:80;touch /tmp/no']:
            with self.assertRaises(ValueError):
                investigation.create(target)
        self.assertEqual(investigation.create('example.com:443')['checks'], ['dns', 'tcp'])

    def test_review_does_not_execute(self):
        session = self.session()
        with patch.object(pry, 'conversation', session):
            pry.investigate_command('next')
            self.assertTrue(pry.current_input_buffer.startswith('/cmd '))
            self.assertEqual(session.history, [])
            self.assertIsNone(session.active)

    def test_evidence_and_failed_probe_progression(self):
        session = self.session()
        state = session.investigation
        for check in state['checks']:
            self.assertEqual(investigation.next_check(state, session.history), check)
            session.submit_command(investigation.command(state, check))
            self.assertEqual(session.history[-1]['investigation_check'], check)
            session.complete('SUCCESS', session.active, dict(cwd=session.cwd, text='Probe unavailable', exit_code=1))
        self.assertIsNone(investigation.next_check(state, session.history))
        session.submit_command(investigation.command(state, 'tcp'))
        session.complete('SUCCESS', session.active, dict(cwd=session.cwd, text='connected', exit_code=0))
        snapshot = investigation.snapshot(state, session.history)
        self.assertIn('[E4]', snapshot)
        self.assertNotIn('[E2]', snapshot)
        self.assertIn('connected', snapshot)
        session.submit('What does this mean?')
        frozen = session.history[-1]['investigation_context']
        session.investigation = None
        self.assertEqual(session.history[-1]['investigation_context'], frozen)

    def test_restore_never_restarts_pending_check(self):
        session = self.session()
        session.submit_command(investigation.command(session.investigation, 'dns'))
        with tempfile.TemporaryDirectory() as directory:
            store = pry.BookmarkStore(Path(directory) / 'sessions.json')
            saved = store.save_session(session)
            loaded = pry.BookmarkStore(store.path).items[0]
            with patch.dict(pry.sessions), patch.object(pry, 'autosave'):
                restored = pry.make_session(loaded)
            self.assertEqual(restored.investigation, saved['investigation'])
            self.assertIsNone(restored.active)
            self.assertFalse(restored.history[0]['pending'])
            self.assertTrue(restored.history[0]['error'])
        self.assertIsNone(investigation.restore({'host': ';bad'}))

    def test_probe_local_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = investigation.probe('tcp', '127.0.0.1', port)
            self.assertEqual(result['attempts'][0]['outcome'], 'connected')
        result = investigation.probe('tcp', '127.0.0.1', port)
        self.assertNotEqual(result['attempts'][0]['outcome'], 'connected')

    def test_conclusion_and_topic_reset(self):
        session = self.session()
        with patch.object(pry, 'conversation', session):
            pry.investigate_command('conclude')
            self.assertIn('insufficient evidence', session.history[-1]['prompt'])
            session.complete('SUCCESS', session.active, 'Insufficient evidence.')
            pry.handle_submission('/topic')
            self.assertIsNone(session.investigation)

    def test_evidence_citations_survive_context_budget(self):
        session = self.session()
        for check in session.investigation['checks']:
            session.submit_command(investigation.command(session.investigation, check))
            session.complete('SUCCESS', session.active, dict(cwd=session.cwd,
                             text='observed ' + check + '\n' + 'x' * 8000, exit_code=0))
        snapshot = investigation.snapshot(session.investigation, session.history)
        messages, _ = pry.build_messages('System rules. ' * 600,
                                        [dict(role='user', content='Conclude.')], inspector=snapshot)
        for index in range(1, 4):
            self.assertIn(f'[E{index}]', messages[0]['content'])
        for check in session.investigation['checks']:
            self.assertIn('observed ' + check, messages[0]['content'])


if __name__ == '__main__':
    unittest.main()
