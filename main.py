import urllib.request
import json
import uuid
import datetime
import time
import itertools
from functools import lru_cache
import re
import os
import string

import boto3
import botocore

IDENTITY_DOC_URL = 'http://169.254.169.254/latest/dynamic/instance-identity/document'
# cloudwatch ignores messages older than 14 days
OLDEST_LOG_RETENTION = datetime.timedelta(days=14)

@lru_cache(1)
def get_instance_identity_document():
    with urllib.request.urlopen(IDENTITY_DOC_URL) as src:
        doc = json.load(src)
    # remove null values and snake case keys
    return {k: v for k, v in doc.items() if v is not None}

def get_region():
    if 'AWS_DEFAULT_REGION' in os.environ:
        return os.environ['AWS_DEFAULT_REGION']
    return get_instance_identity_document()['region']

def normalise_unit(unit):
    if '@' in unit:
        # remove templating in unit names e.g. sshd@127.0.0.1:12345.service -> sshd.service
        unit = '.'.join(( unit.partition('@')[0], unit.rpartition('.')[2] ))
    return unit

class Formatter(string.Formatter):
    '''
    custom string formatter
    if the key is of the format a|b|c , it will try to use a, b or c in that order
    strings can also be used with '", but they should not contain |

    >>> Format = Formatter().format
    >>> Format('{a|b|c}', b=5)
    '5'
    >>> Format('{a|b|c}', d=5)
    Traceback (most recent call last):
    KeyError: 'a|b|c'
    >>> Format('{a|"ASD"}')
    'ASD'
    '''

    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            for i in key.split('|'):
                # test for string literal
                if len(i) > 1 and (i[0] == '"' or i[0] == "'") and i[0] == i[-1]:
                    return i[1:-1]

                # test for special key
                if i.startswith('$'):
                    k = i[1:]
                    # instance identity doc variables
                    doc = get_instance_identity_document()
                    if k in doc:
                        return doc[k]

                    # custom journald variables
                    if k == 'unit':
                        if 'USER_UNIT' in kwargs:
                            return normalise_unit(kwargs['USER_UNIT'])
                        if '_SYSTEMD_UNIT' in kwargs:
                            return normalise_unit(kwargs['_SYSTEMD_UNIT'])
                    if k == 'docker_container':
                        if 'CONTAINER_NAME' in kwargs and kwargs.get('_SYSTEMD_UNIT') == 'docker.service':
                            return kwargs['CONTAINER_NAME'] + '.container'

                    # environment variables
                    if k in os.environ:
                        return os.environ[k]

                # default
                if i in kwargs:
                    return kwargs[i]

        return super().get_value(key, args, kwargs)

Format = Formatter().format

class JournalMsgEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.timestamp()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

class CloudWatchClient:
    def __init__(self, cursor_path, log_group_format, log_stream_format):
        self.client = boto3.client('logs', region_name=get_region())
        self.cursor_path = cursor_path
        self.log_group_format = log_group_format
        self.log_stream_format = log_stream_format

    def group_messages(self, msg):
        ''' returns the group and stream names for this msg '''
        group = Format(self.log_group_format, **msg)
        group = self.log_group_client(group)
        stream = Format(self.log_stream_format, **msg)
        return (group, stream)

    @lru_cache(None)
    def log_group_client(self, name):
        ''' get or create a log group client '''
        return LogGroupClient(name, self)

    def put_log_messages(self, log_group, log_stream, seq_token, messages):
        ''' log the message to cloudwatch, then save the cursor '''
        kwargs = (dict(sequenceToken=seq_token) if seq_token else {})
        log_events = list(map(self.make_message, messages))

        result = self.client.put_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            logEvents=log_events,
            **kwargs
        )
        # save last cursor
        self.save_cursor(messages[-1]['__CURSOR'])
        return result

    @staticmethod
    def make_message(message):
        ''' prepare a message to send to cloudwatch '''
        timestamp = int(message['__REALTIME_TIMESTAMP'].timestamp() * 1000)
        # remove unserialisable values
        message = {k: v for k, v in message.items() if isinstance(v, (str, int, uuid.UUID, datetime.datetime))}
        # encode entire message in json
        message = json.dumps(message, cls=JournalMsgEncoder)
        return dict(timestamp=timestamp, message=message)

    @staticmethod
    def retain_message(message, retention=OLDEST_LOG_RETENTION):
        ''' cloudwatch ignores messages older than 14 days '''
        return (datetime.datetime.now() - message['__REALTIME_TIMESTAMP']) < retention

    def save_cursor(self, cursor):
        ''' saves the journal cursor to file '''
        with open(self.cursor_path, 'w') as f:
            f.write(cursor)

    def load_cursor(self):
        ''' loads the journal cursor from file, returns None if file not found '''
        try:
            with open(self.cursor_path, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            return

    def upload_journal_logs(self, log_path):
        import systemd.journal
        cursor = self.load_cursor()
        with systemd.journal.Reader(path=log_path) as reader:
            if cursor:
                reader.seek_cursor(cursor)
            else:
                # no cursor, start from 14 days ago
                reader.seek_realtime(datetime.datetime.now() - OLDEST_LOG_RETENTION)

            reader = filter(self.retain_message, reader)
            for (group, stream), messages in itertools.groupby(reader, key=self.group_messages):
                group.log_messages(stream, list(messages))

class LogGroupClient:
    ALREADY_EXISTS = 'ResourceAlreadyExistsException'
    THROTTLED = 'ThrottlingException'
    OPERATION_ABORTED = 'OperationAbortedException'
    INVALID_TOKEN = 'InvalidSequenceTokenException'
    NEXT_TOKEN_REGEX = re.compile('sequenceToken(\sis)?: (\S+)')

    def __init__(self, log_group, parent):
        self.log_group = log_group
        self.parent = parent
        self.tokens = {}
        self.create_log_group()

    def create_log_group(self):
        ''' create a log group, ignoring if it exists '''
        try:
            self.parent.client.create_log_group(logGroupName=self.log_group)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] != self.ALREADY_EXISTS:
                raise

    def create_log_stream(self, log_stream):
        ''' create a log stream, ignoring if it exists '''
        try:
            self.parent.client.create_log_stream(logGroupName=self.log_group, logStreamName=log_stream)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] != self.ALREADY_EXISTS:
                raise

    @staticmethod
    def group_messages(messages, maxlen=10, timespan=datetime.timedelta(hours=23)):
        '''
        group messages:
            - in 23 hour segments (cloudwatch rejects logs spanning > 24 hours)
            - in groups of 10 to avoid upload limits
        '''
        while messages:
            group = messages
            start_date = group[0]['__REALTIME_TIMESTAMP']
            group = itertools.takewhile(lambda m: m['__REALTIME_TIMESTAMP'] - start_date < timespan, group)
            group = itertools.islice(group, maxlen)
            group = list(group)
            yield group
            messages = messages[len(group):]

    def log_messages(self, log_stream, messages):
        for chunk in self.group_messages(messages):
            self._log_messages(log_stream, chunk)

    def _log_messages(self, log_stream, messages):
        ''' log the messages '''
        if not messages:
            return

        while True:
            try:
                seq_token = self.get_seq_token(log_stream)
                result = self.parent.put_log_messages(self.log_group, log_stream, seq_token, messages)
            except botocore.exceptions.ClientError as e:
                code = e.response['Error']['Code']
                if code == self.THROTTLED:
                    # throttled, wait a bit and retry
                    time.sleep(1)
                elif code == self.OPERATION_ABORTED:
                    # aborted, retry
                    pass
                elif code == self.INVALID_TOKEN:
                    # invalid token, use the given token (if any)
                    match = self.NEXT_TOKEN_REGEX.search(e.response['Error']['Message'])
                    if match:
                        self.tokens[log_stream] = (None if match.group(2) == 'null' else match.group(2))
                    else:
                        self.tokens.pop(log_stream)
                else:
                    # other error
                    raise
            else:
                # no error, finish
                break
        self.tokens[log_stream] = result['nextSequenceToken']

    def get_seq_token(self, log_stream):
        ''' get the sequence token for the stream '''

        try:
            return self.tokens[log_stream]
        except KeyError:
            pass
        token = self.tokens[log_stream] = self.get_new_seq_token(log_stream)
        return token

    def get_new_seq_token(self, log_stream):
        streams = self.parent.client.describe_log_streams(logGroupName=self.log_group, logStreamNamePrefix=log_stream, limit=1)
        if streams['logStreams']:
            stream = streams['logStreams'][0]
            if stream['logStreamName'] == log_stream:
                # found the stream
                return stream.get('uploadSequenceToken')
        # no stream, create it
        self.create_log_stream(log_stream)

if __name__ == '__main__': # pragma: no cover
    import argparse
    import systemd.journal

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--cursor', required=True,
                        help='Store/read the journald cursor in this file')
    parser.add_argument('--logs', default='/var/log/journal',
                        help='Directory to journald logs (default: %(default)s)')
    parser.add_argument('-g', '--log-group-format', required=True,
                       help='Python format string for log group names')
    parser.add_argument('-s', '--log-stream-format', required=True,
                       help='Python format string for log stream names')
    args = parser.parse_args()

    while True:
        upload_logs(args.cursor, args.log_group_format, args.log_stream_format)
