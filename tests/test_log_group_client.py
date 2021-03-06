from unittest import TestCase
from unittest.mock import patch, sentinel, call, create_autospec
import os
from datetime import datetime
from moto import mock_cloudwatch
from botocore.exceptions import ClientError

from main import CloudWatchClient, LogGroupClient

def client_error(code, msg='', op=''):
    return ClientError({'Error': {'Code': code, 'Message': msg}}, op)

@mock_cloudwatch
class LogGroupClientTest(TestCase):
    GROUP = 'log group'
    STREAM = 'log stream'
    REGION = 'us-east-1'
    PUT_LOG_EVENTS_RESULT = {'nextSequenceToken': sentinel.next_token}

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

        with patch.object(self.cwl, 'create_log_group', side_effect=client_error('ResourceAlreadyExistsException')):
            self.client.create_log_group()

    def test_create_log_group_other_error(self):
        ''' creating log groups propagates other errors '''

        with patch.object(self.cwl, 'create_log_group', side_effect=client_error('other error')):
            with self.assertRaises(ClientError):
                self.client.create_log_group()

    def test_create_log_stream(self):
        ''' it creates log streams '''

        self.client.create_log_stream(self.STREAM)
        self.cwl.create_log_stream.assert_called_once_with(logGroupName=self.GROUP, logStreamName=self.STREAM)

    def test_create_log_stream_exists(self):
        ''' if log stream exists it ignores the error '''

        with patch.object(self.cwl, 'create_log_stream', side_effect=client_error('ResourceAlreadyExistsException')):
            self.client.create_log_stream(self.STREAM)

    def test_create_log_stream_other_error(self):
        ''' creating log streams propagates other errors '''

        with patch.object(self.cwl, 'create_log_stream', side_effect=client_error('other error')):
            with self.assertRaises(ClientError):
                self.client.create_log_stream(self.STREAM)

    def test_get_new_seq_token_empty(self):
        ''' no log streams found, it should create the log stream '''

        value = dict(logStreams=[])
        self.cwl.describe_log_streams.return_value = value
        self.client.get_new_seq_token(self.STREAM)
        self.assertEqual(self.cwl.mock_calls, [
            call.describe_log_streams(limit=1, logGroupName=self.GROUP, logStreamNamePrefix=self.STREAM),
            call.create_log_stream(logGroupName=self.GROUP, logStreamName=self.STREAM),
        ])

    def test_get_new_seq_token_no_match(self):
        ''' no matching log streams found, it should create the log stream '''

        value = dict(logStreams=[dict(logStreamName='blargh')])
        self.cwl.describe_log_streams.return_value = value
        self.client.get_new_seq_token(self.STREAM)
        self.assertEqual(self.cwl.mock_calls, [
            call.describe_log_streams(limit=1, logGroupName=self.GROUP, logStreamNamePrefix=self.STREAM),
            call.create_log_stream(logGroupName=self.GROUP, logStreamName=self.STREAM),
        ])

    def test_get_new_seq_token(self):
        ''' return the seq token '''

        value = dict(logStreams=[dict(logStreamName=self.STREAM, uploadSequenceToken=sentinel.token)])
        self.cwl.describe_log_streams.return_value = value
        self.assertIs( self.client.get_new_seq_token(self.STREAM), sentinel.token )

    def test_get_seq_token(self):
        ''' it should get a new seq token '''

        with patch.object(self.client, 'get_new_seq_token', return_value=sentinel.token):
            self.assertIs( self.client.get_seq_token(self.STREAM), sentinel.token )

    def test_get_seq_token_cached(self):
        ''' it should return a cached seq token '''

        self.client.tokens[self.STREAM] = sentinel.token
        self.assertIs( self.client.get_seq_token(self.STREAM), sentinel.token )

    def test_log_messages_no_messages(self):
        ''' no messages, it does nothing '''
        self.client.log_messages(self.STREAM, [])
        # no aws api calls
        self.assertEqual(len(self.cwl.mock_calls), 0)

    def mock_log_messages(self, seq_token=[sentinel.token], side_effect=[PUT_LOG_EVENTS_RESULT]):
        with patch.object(self.client, 'get_new_seq_token', side_effect=seq_token, autospec=True) as self.get_new_seq_token:
            with patch.object(self.parent, 'put_log_messages', autospec=True, side_effect=side_effect) as self.put_log_messages:
                self.client.log_messages(self.STREAM, sentinel.messages)

    def test_log_messages(self):
        ''' log_messages() uploads logs to cloudwatch '''

        self.mock_log_messages()
        self.get_new_seq_token.assert_called_once_with(self.STREAM)
        self.put_log_messages.assert_called_once_with(self.GROUP, self.STREAM, sentinel.token, sentinel.messages)
        self.assertIs(self.client.tokens[self.STREAM], sentinel.next_token)

    @patch('time.sleep')
    def test_log_messages_throttled(self, sleep):
        ''' log_messages() retries if throttled '''

        error = client_error('ThrottlingException')
        self.mock_log_messages(side_effect=[error, error, self.PUT_LOG_EVENTS_RESULT])
        self.put_log_messages.assert_has_calls([call(self.GROUP, self.STREAM, sentinel.token, sentinel.messages) for _ in range(3)])
        self.assertIs(self.client.tokens[self.STREAM], sentinel.next_token)

    def test_log_messages_operation_aborted(self):
        ''' log_messages() retries if aborted '''

        error = client_error('OperationAbortedException')
        self.mock_log_messages(side_effect=[error, error, self.PUT_LOG_EVENTS_RESULT])
        self.put_log_messages.assert_has_calls([call(self.GROUP, self.STREAM, sentinel.token, sentinel.messages) for _ in range(3)])
        self.assertIs(self.client.tokens[self.STREAM], sentinel.next_token)

    def test_log_messages_invalid_token(self):
        ''' log_messages() retries with new token '''

        token = 'hereisacloudwatchtoken'
        error = client_error('InvalidSequenceTokenException', msg='The given sequenceToken is invalid. The next expected sequenceToken is: ' + token)
        self.mock_log_messages(side_effect=[error, self.PUT_LOG_EVENTS_RESULT])
        self.put_log_messages.assert_has_calls([call(self.GROUP, self.STREAM, token, sentinel.messages) for token in (sentinel.token, token)])
        self.assertIs(self.client.tokens[self.STREAM], sentinel.next_token)

    def test_log_messages_invalid_token_null(self):
        ''' log_messages() retries on invalid token with null '''

        error = client_error('InvalidSequenceTokenException', msg='The given sequenceToken is invalid. The next expected sequenceToken is: null')
        self.mock_log_messages(side_effect=[error, self.PUT_LOG_EVENTS_RESULT])
        self.put_log_messages.assert_has_calls([call(self.GROUP, self.STREAM, token, sentinel.messages) for token in (sentinel.token, None)])
        self.assertIs(self.client.tokens[self.STREAM], sentinel.next_token)

    def test_log_messages_invalid_token_no_token_given(self):
        ''' log_messages() fetches new token and retries '''

        error = client_error('InvalidSequenceTokenException', msg='blargh')
        tokens = [sentinel.token1, sentinel.token2]
        self.mock_log_messages(seq_token=tokens, side_effect=[error, self.PUT_LOG_EVENTS_RESULT])
        self.put_log_messages.assert_has_calls([call(self.GROUP, self.STREAM, token, sentinel.messages) for token in tokens])
        self.assertIs(self.client.tokens[self.STREAM], sentinel.next_token)

    def test_log_messages_error(self):
        ''' log_messages() propagates other errors '''

        error = client_error('other error')
        with self.assertRaises(ClientError) as cm:
            self.mock_log_messages(side_effect=[error])
            self.assertEqual(cm, error)
