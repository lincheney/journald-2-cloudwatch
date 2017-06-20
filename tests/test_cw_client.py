from unittest import TestCase
from unittest.mock import patch, sentinel, call, create_autospec, Mock, MagicMock, DEFAULT
import tempfile
import os
import uuid
import json
import sys
from datetime import datetime, timedelta
from moto import mock_cloudwatch
import systemd.journal

from main import get_region, CloudWatchClient, JournalMsgEncoder, LogGroupClient, Format, OLDEST_LOG_RETENTION

class RegionTest(TestCase):
    ''' tests for get_region() '''

    def test_region_from_env_var(self):
        ''' uses $AWS_DEFAULT_REGION by default '''

        region = 'aws region'
        with patch.dict('os.environ', AWS_DEFAULT_REGION=region):
            self.assertEqual(get_region(), region)

    def test_region_from_ec2_metadata(self):
        ''' uses ec2 metadata when env var is not set '''

        region = 'aws region'
        with patch.dict('os.environ', clear=True):
            with patch('main.get_instance_identity_document', return_value=dict(region=region), autospec=True):
                self.assertEqual(get_region(), region)

@mock_cloudwatch
class CloudWatchClientTest(TestCase):
    CURSOR = os.path.dirname(__file__) + '/cursor.txt'
    CURSOR_CONTENT = open(CURSOR).read().rstrip('\n')
    GROUP = 'log group'
    STREAM = 'log stream '
    REGION = 'us-east-1'

    def make_client(self, cursor='/dev/null', group_format='group', stream_format='stream'):
        return CloudWatchClient(cursor, group_format, stream_format)

    def setUp(self):
        super().setUp()
        os.environ['AWS_DEFAULT_REGION'] = self.REGION
        self.client = self.make_client(self.CURSOR, self.GROUP, self.STREAM)
        self.cwl = self.client.client = create_autospec(self.client.client)

    def test_init(self):
        ''' test client init '''

        with patch('boto3.client', autospec=True) as boto3:
            with patch('main.get_region', autospec=True) as get_region:
                client = self.make_client(sentinel.cursor, sentinel.group, sentinel.stream)
                self.assertIs(client.cursor_path, sentinel.cursor)
                self.assertIs(client.log_group_format, sentinel.group)
                self.assertIs(client.log_stream_format, sentinel.stream)
                # sets up the cwlogs client
                self.assertEqual(client.client, boto3.return_value)
                boto3.assert_called_once_with('logs', region_name=get_region.return_value)

    def test_save_cursor(self):
        ''' test the cursor is saved to the file '''

        cursor = 'blargh'
        with tempfile.NamedTemporaryFile('r') as file:
            client = self.make_client(file.name)
            client.save_cursor(cursor)
            self.assertEqual(file.read(), cursor)

    def test_load_cursor(self):
        ''' test the cursor is loaded from the file '''

        cursor = 'blarg'
        with tempfile.NamedTemporaryFile('w') as file:
            client = self.make_client(file.name)
            file.write(cursor)
            file.flush()
            self.assertEqual(client.load_cursor(), cursor)

    def test_load_no_cursor(self):
        ''' test load cursor for non existent file '''

        client = self.make_client('/non/existent/file')
        self.assertIsNone(client.load_cursor())

    def test_retain_message(self):
        ''' test retain_message() '''

        # keep messages newer than 14 days
        self.assertTrue(CloudWatchClient.retain_message(dict(__REALTIME_TIMESTAMP=datetime.now() - timedelta(days=1))))
        # drop messages older than 14 days
        self.assertFalse(CloudWatchClient.retain_message(dict(__REALTIME_TIMESTAMP=datetime.now() - timedelta(days=14))))

    def test_make_message(self):
        ''' test make_message() serialises the data '''

        ts = 123456789
        msg = [('__REALTIME_TIMESTAMP', datetime.fromtimestamp(ts)), ('a', 'abc'), ('b', 123), ('c', datetime.now()), ('d', uuid.uuid4()), ('e', object()), ]
        result = CloudWatchClient.make_message(dict(msg))

        # dict with 2 keys
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 2)

        self.assertEqual(result['timestamp'], ts*1000)
        # only first 5 fields are serialisable
        self.assertEqual(json.loads(result['message']), json.loads(json.dumps(dict(msg[:5]), cls=JournalMsgEncoder)))

    def test_log_group_client(self):
        ''' test log group client creation '''

        group = self.client.log_group_client('group-name')
        self.assertIsInstance(group, LogGroupClient)
        self.assertIs(group.parent, self.client)
        self.assertEqual(group.log_group, 'group-name')

    def test_log_group_client_cached(self):
        ''' test log group client creation is cached '''

        group = self.client.log_group_client('group-name')
        group2 = self.client.log_group_client('group-name')
        self.assertIs(group, group2)

    def test_group_messages(self):
        ''' test making group and stream names from msg '''

        msg = {}
        names = [self.GROUP, self.STREAM]
        with patch('main.Format', side_effect=names) as formatter:
            group, stream = self.client.group_messages(msg)

        self.assertIsInstance(group, LogGroupClient)
        self.assertEqual(group.log_group, self.GROUP)
        self.assertEqual(stream, self.STREAM)

    def test_put_log_messages(self):
        ''' test put_log_messages() '''

        messages = [dict(a='abc'), dict(b='xyz', __CURSOR=sentinel.cursor)]
        events = [sentinel.msg1, sentinel.msg2]
        with patch.object(self.client, 'make_message', side_effect=events, autospec=True) as make_message:
            with patch.object(self.client, 'save_cursor', autospec=True) as save_cursor:
                self.client.put_log_messages(sentinel.group, sentinel.stream, sentinel.token, messages)

        self.cwl.put_log_events.assert_called_once_with(
            logGroupName=sentinel.group,
            logStreamName=sentinel.stream,
            logEvents=events,
            sequenceToken=sentinel.token,
        )
        # saves the cursor once finished
        save_cursor.assert_called_once_with(sentinel.cursor)

    def test_put_log_messages_no_token(self):
        ''' test put_log_messages() when no sequence token given '''

        messages = [dict(a='abc'), dict(b='xyz', __CURSOR=sentinel.cursor)]
        events = [sentinel.msg1, sentinel.msg2]
        with patch.object(self.client, 'make_message', side_effect=events, autospec=True) as make_message:
            with patch.object(self.client, 'save_cursor', autospec=True) as save_cursor:
                self.client.put_log_messages(sentinel.group, sentinel.stream, None, messages)

        self.cwl.put_log_events.assert_called_once_with(
            logGroupName=sentinel.group,
            logStreamName=sentinel.stream,
            logEvents=events,
        )
        # saves the cursor once finished
        save_cursor.assert_called_once_with(sentinel.cursor)

    def test_upload_journal_logs(self):
        ''' test upload_journal_logs() '''

        journal = systemd.journal.Reader(path=os.getcwd())
        with patch('systemd.journal.Reader', return_value=journal) as journal:
            with patch('main.JournaldClient', MagicMock(autospec=True)) as reader:
                reader.return_value.__iter__.return_value = [sentinel.msg1, sentinel.msg2, sentinel.msg3, sentinel.msg4]

                with patch.multiple(self.client, retain_message=DEFAULT, group_messages=DEFAULT):
                    log_group1 = Mock()
                    log_group2 = Mock()
                    self.client.retain_message.side_effect = [True, False, True, True]
                    self.client.group_messages.side_effect = [(log_group1, 'stream1'), (log_group2, 'stream2'), (log_group2, 'stream2')]

                    self.client.upload_journal_logs(os.getcwd())

        # creates reader
        reader.assert_called_once_with(journal.return_value, self.CURSOR_CONTENT)
        # uploads log messages
        log_group1.log_messages.assert_called_once_with('stream1', [sentinel.msg1])
        log_group2.log_messages.assert_called_once_with('stream2', [sentinel.msg3, sentinel.msg4])
