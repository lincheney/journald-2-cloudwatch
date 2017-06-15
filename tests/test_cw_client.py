from unittest import TestCase
from unittest.mock import patch, sentinel, call
import tempfile
import os
import uuid
import json
from datetime import datetime, timedelta

from main import get_region, CloudWatchClient, JournalMsgEncoder, LogGroupClient, Format

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

@patch('boto3.client', autospec=True)
class CloudWatchClientTest(TestCase):
    def client(self, cursor='/dev/null', group_format='group', stream_format='stream'):
        return CloudWatchClient(cursor, group_format, stream_format)

    REGION = 'us-east-1'
    def setUp(self):
        os.environ['AWS_DEFAULT_REGION'] = self.REGION

    def test_init(self, boto3):
        ''' test client init '''

        client = self.client(sentinel.cursor, sentinel.group, sentinel.stream)
        self.assertIs(client.cursor_path, sentinel.cursor)
        self.assertIs(client.log_group_format, sentinel.group)
        self.assertIs(client.log_stream_format, sentinel.stream)
        # sets up the cwlogs client
        self.assertEqual(client.client, boto3.return_value)
        boto3.assert_called_once_with('logs', region_name=self.REGION)

    def test_save_cursor(self, boto3):
        ''' test the cursor is saved to the file '''

        cursor = 'blargh'
        with tempfile.NamedTemporaryFile('r') as file:
            client = self.client(file.name)
            client.save_cursor(cursor)
            self.assertEqual(file.read(), cursor)

    def test_load_cursor(self, boto3):
        ''' test the cursor is loaded from the file '''

        cursor = 'blarg'
        with tempfile.NamedTemporaryFile('w') as file:
            client = self.client(file.name)
            file.write(cursor)
            file.flush()
            self.assertEqual(client.load_cursor(), cursor)

    def test_load_no_cursor(self, boto3):
        ''' test load cursor for non existent file '''

        client = self.client('/non/existent/file')
        self.assertIsNone(client.load_cursor())

    def test_retain_message(self, boto3):
        ''' test retain_message() '''

        # keep messages newer than 14 days
        self.assertTrue(CloudWatchClient.retain_message(dict(__REALTIME_TIMESTAMP=datetime.now() - timedelta(days=1))))
        # drop messages older than 14 days
        self.assertFalse(CloudWatchClient.retain_message(dict(__REALTIME_TIMESTAMP=datetime.now() - timedelta(days=14))))

    def test_make_message(self, boto3):
        ''' test make_message() serialises the data '''

        ts = 123456789
        msg = [('__REALTIME_TIMESTAMP', datetime.fromtimestamp(ts)), ('a', 'abc'), ('b', 123), ('c', datetime.now()), ('d', uuid.uuid4()), ('e', object()), ]
        client = self.client()
        result = client.make_message(dict(msg))

        # dict with 2 keys
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 2)

        self.assertEqual(result['timestamp'], ts*1000)
        # only first 5 fields are serialisable
        self.assertEqual(json.loads(result['message']), json.loads(json.dumps(dict(msg[:5]), cls=JournalMsgEncoder)))

    def test_log_group_client(self, boto3):
        ''' test log group client creation '''

        client = self.client()
        group = client.log_group_client('group-name')
        self.assertIsInstance(group, LogGroupClient)
        self.assertIs(group.parent, client)
        self.assertEqual(group.log_group, 'group-name')

    def test_group_messages(self, boto3):
        ''' test making group and stream names from msg '''

        client = self.client(group_format='{group}', stream_format='{stream}')
        msg = dict(group='abc', stream='xyz')

        with patch('main.Format', wraps=Format) as formatter:
            group, stream = client.group_messages(msg)
            formatter.assert_has_calls([
                call(client.log_group_format, **msg),
                call(client.log_stream_format, **msg),
            ])

        self.assertIsInstance(group, LogGroupClient)
        self.assertEqual(group.log_group, msg['group'])
        self.assertEqual(stream, msg['stream'])

    def test_put_log_messages(self, boto3):
        ''' test put_log_messages() '''
        client = self.client()
        messages = [dict(a='abc'), dict(b='xyz')]

        events = [sentinel.msg1, sentinel.msg2]
        with patch.object(client, 'make_message', side_effect=events) as make_message:
            client.put_log_messages(sentinel.group, sentinel.stream, sentinel.token, messages)

        client.client.put_log_events.assert_called_once_with(
            logGroupName=sentinel.group,
            logStreamName=sentinel.stream,
            logEvents=events,
            sequenceToken=sentinel.token,
        )

    def test_put_log_messages_no_token(self, boto3):
        ''' test put_log_messages() when no sequence token given '''
        client = self.client()
        messages = [dict(a='abc'), dict(b='xyz')]

        events = [sentinel.msg1, sentinel.msg2]
        with patch.object(client, 'make_message', side_effect=events) as make_message:
            client.put_log_messages(sentinel.group, sentinel.stream, None, messages)

        client.client.put_log_events.assert_called_once_with(
            logGroupName=sentinel.group,
            logStreamName=sentinel.stream,
            logEvents=events,
        )
