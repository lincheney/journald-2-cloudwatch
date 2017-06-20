from unittest import TestCase
from unittest.mock import patch, sentinel, call, Mock, DEFAULT
import os
from datetime import datetime
import systemd.journal

from main import JournaldClient, OLDEST_LOG_RETENTION

class JournaldClientTest(TestCase):
    READER = systemd.journal.Reader(path=os.getcwd())

    def client(self, reader=READER, cursor=None):
        return JournaldClient(reader, cursor)

    @patch.multiple(READER, seek_cursor=DEFAULT, get_next=DEFAULT, spec_set=True)
    def test_with_cursor(self, seek_cursor, get_next):
        ''' test when cursor exists '''

        parent = Mock()
        parent.attach_mock(seek_cursor, 'seek_cursor')
        parent.attach_mock(get_next, 'get_next')
        self.client(cursor=sentinel.cursor)
        # seeks to the cursor and skips first
        parent.assert_has_calls([
            call.seek_cursor(sentinel.cursor),
            call.get_next(),
        ])

    @patch.object(READER, 'seek_realtime', autospec=True)
    def test_with_no_cursor(self, seek_realtime):
        ''' test when no cursor '''

        now = datetime.now()
        with patch('datetime.datetime', autospec=True) as dt:
            dt.now.return_value = now
            self.client(cursor=None)

        # seeks to start of this boot
        seek_realtime.assert_called_once_with(now - OLDEST_LOG_RETENTION)

    def test_messages(self):
        ''' passes logs from reader '''

        logs1 = [1, 2, 3]
        logs2 = [4, 5, 6]
        logs = logs1 + [{}] + logs2 + [{}]
        with patch('systemd.journal.Reader.get_next', side_effect=logs, spec_set=True) as get_next:
            with patch.object(self.READER, 'wait', side_effect=[None, StopIteration], spec_set=True) as wait:
                parent = Mock()
                parent.attach_mock(get_next, 'get_next')
                parent.attach_mock(wait, 'wait')

                self.assertListEqual(list(self.client()), logs1 + logs2)
                parent.assert_has_calls(
                    [call.get_next()] * 4 +
                    [call.wait()] +
                    [call.get_next()] * 4 +
                    [call.wait()]
                )
