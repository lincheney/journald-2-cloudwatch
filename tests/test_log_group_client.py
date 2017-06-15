from unittest import TestCase
from unittest.mock import patch, sentinel, call, create_autospec
import os
from datetime import datetime
from moto import mock_cloudwatch
from botocore.exceptions import ClientError

from main import CloudWatchClient, LogGroupClient

@mock_cloudwatch
class LogGroupClientTest(TestCase):
    GROUP = 'log group'
    STREAM = 'log stream'
    REGION = 'us-east-1'

    def setUp(self):
        super().setUp()
        os.environ['AWS_DEFAULT_REGION'] = self.REGION
        self.parent = CloudWatchClient('', '', '')
        self.cwl = self.parent.client = create_autospec(self.parent.client)
        self.client = LogGroupClient(self.GROUP, self.parent)
        self.cwl.reset_mock()

    def test_init(self):
        client = LogGroupClient(self.GROUP, self.parent)
        self.assertEqual(client.log_group, self.GROUP)
        self.cwl.create_log_group.assert_called_once_with(logGroupName=self.GROUP)

    def test_create_log_group(self):
        ''' it creates log groups '''

        self.client.create_log_group()
        self.cwl.create_log_group.assert_called_once_with(logGroupName=self.GROUP)

    def test_create_log_group_exists(self):
        ''' if log group exists it ignores the error '''

        with patch.object(self.cwl, 'create_log_group', side_effect=ClientError({'Error': {'Code': 'ResourceAlreadyExistsException'}}, '')):
            self.client.create_log_group()

    def test_create_log_group_other_error(self):
        ''' creating log groups propagates other errors '''

        with patch.object(self.cwl, 'create_log_group', side_effect=ClientError({'Error': {'Code': 'other error'}}, '')):
            with self.assertRaises(ClientError):
                self.client.create_log_group()

    def test_create_log_stream(self):
        ''' it creates log streams '''

        self.client.create_log_stream(self.STREAM)
        self.cwl.create_log_stream.assert_called_once_with(logGroupName=self.GROUP, logStreamName=self.STREAM)

    def test_create_log_stream_exists(self):
        ''' if log stream exists it ignores the error '''

        with patch.object(self.cwl, 'create_log_stream', side_effect=ClientError({'Error': {'Code': 'ResourceAlreadyExistsException'}}, '')):
            self.client.create_log_stream(self.STREAM)

    def test_create_log_stream_other_error(self):
        ''' creating log streams propagates other errors '''

        with patch.object(self.cwl, 'create_log_stream', side_effect=ClientError({'Error': {'Code': 'other error'}}, '')):
            with self.assertRaises(ClientError):
                self.client.create_log_stream(self.STREAM)

    def test_get_seq_token_empty(self):
        ''' no log streams found, it should create the log stream '''

        value = dict(logStreams=[])
        self.cwl.describe_log_streams.return_value = value
        self.client.get_seq_token(self.STREAM)
        self.assertEqual(self.cwl.mock_calls, [
            call.describe_log_streams(limit=1, logGroupName=self.GROUP, logStreamNamePrefix=self.STREAM),
            call.create_log_stream(logGroupName=self.GROUP, logStreamName=self.STREAM),
        ])

    def test_get_seq_token_no_match(self):
        ''' no matching log streams found, it should create the log stream '''

        value = dict(logStreams=[dict(logStreamName='blargh')])
        self.cwl.describe_log_streams.return_value = value
        self.client.get_seq_token(self.STREAM)
        self.assertEqual(self.cwl.mock_calls, [
            call.describe_log_streams(limit=1, logGroupName=self.GROUP, logStreamNamePrefix=self.STREAM),
            call.create_log_stream(logGroupName=self.GROUP, logStreamName=self.STREAM),
        ])

    def test_get_seq_token(self):
        ''' return the seq token '''

        value = dict(logStreams=[dict(logStreamName=self.STREAM, uploadSequenceToken=sentinel.token)])
        self.cwl.describe_log_streams.return_value = value
        self.assertIs( self.client.get_seq_token(self.STREAM), sentinel.token )

    def test_group_messages(self):
        ''' group_messages() should chunk up messages '''

        key = '__REALTIME_TIMESTAMP'
        new_msg = {key: datetime.now()}
        old_msg = {key: datetime.fromtimestamp(123)}

        msgs = [old_msg]*4 + [new_msg]*15
        chunks = list(LogGroupClient.group_messages(msgs))
        # old in one chunk, new split into chunk of 10 and 5
        self.assertEqual(chunks, [msgs[:4], msgs[4:14], msgs[14:]])

    def test_log_messages_no_messages(self):
        ''' no messages, it does nothing '''
        self.client._log_messages(self.STREAM, [])
        # no aws api calls
        self.assertEqual(len(self.cwl.mock_calls), 0)

    def test_log_messages(self):
        ''' log_messages() uploads logs to cloudwatch '''

        with patch.object(self.client, 'get_seq_token', return_value=sentinel.token, autospec=True) as get_seq_token:
            with patch.object(self.parent, 'put_log_messages', autospec=True) as put_log_messages:
                self.client._log_messages(self.STREAM, sentinel.messages)

        get_seq_token.assert_called_once_with(self.STREAM)
        put_log_messages.assert_called_once_with(self.GROUP, self.STREAM, sentinel.token, sentinel.messages)

    @patch('time.sleep')
    def test_log_messages_throttled(self, sleep):
        ''' log_messages() retries if throttled '''

        error = ClientError(dict(Error=dict(Code='ThrottlingException')), '')
        tokens = [sentinel.token1, sentinel.token2, sentinel.token3]
        puts = [error, error, None]

        with patch.object(self.client, 'get_seq_token', side_effect=tokens, autospec=True) as get_seq_token:
            with patch.object(self.parent, 'put_log_messages', side_effect=puts, autospec=True) as put_log_messages:
                self.client._log_messages(self.STREAM, sentinel.messages)

        get_seq_token.assert_has_calls([ call(self.STREAM) ] * 3)
        put_log_messages.assert_has_calls([call(self.GROUP, self.STREAM, token, sentinel.messages) for token in tokens])
