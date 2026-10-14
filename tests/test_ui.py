import unittest
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from workout_logger.ui.prompts import Choice, ask_rir, select


class MenuTests(unittest.TestCase):
    def choose(self, keys, choices=None, default=None):
        with create_pipe_input() as pipe:
            def application(**kwargs):
                return Application(**kwargs, input=pipe, output=DummyOutput())
            pipe.send_text(keys)
            with patch('workout_logger.ui.prompts.Application', side_effect=application):
                return select('Choose:', choices or [('First', 'a'), ('Middle', 'b'), ('Last', 'c')], default=default)

    def test_up_from_first_wraps_to_last_and_accepts(self):
        self.assertEqual(self.choose('\x1b[A\r'), 'c')

    def test_down_from_last_wraps_to_first_and_accepts(self):
        self.assertEqual(self.choose('\x1b[B\r', default='c'), 'a')

    def test_multiple_arrow_moves_and_default(self):
        self.assertEqual(self.choose('\x1b[A\x1b[A\r'), 'b')
        self.assertEqual(self.choose('\r', default='b'), 'b')

    def test_one_item_wraps_safely(self):
        self.assertEqual(self.choose('\x1b[A\x1b[B\r', [Choice('Only', 7)]), 7)

    def test_escape_returns_none(self):
        self.assertIsNone(self.choose('\x1b'))

    def test_other_rir_accepts_arbitrary_nonnegative_value(self):
        with patch('workout_logger.ui.prompts.select', return_value='other'), patch('workout_logger.ui.prompts.ask_int', return_value=5):
            self.assertEqual(ask_rir(), '5')


if __name__ == '__main__':
    unittest.main()
