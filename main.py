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

import systemd.journal
import boto3
import botocore

IDENTITY_DOC_URL = 'http://169.254.169.254/latest/dynamic/instance-identity/document'

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
                            return kwargs['CONTAINER_NAME']

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
        self.log_groups = {}

    def group_messages(self, msg):
        ''' returns the group and stream names for this msg '''
        group = Format(self.log_group_format, **msg)
        group = self.log_group_client(group)
        stream = Format(self.log_stream_format, **msg)
        return (group, stream)

    def log_group_client(self, name):
        ''' get or create a log group client '''
        try:
            return self.log_groups[name]
        except KeyError:
            pass
        client = self.log_groups[name] = LogGroupClient(name, self)
        return client

    def put_log_messages(self, log_group, log_stream, seq_token, messages):
        ''' log the message to cloudwatch '''
        kwargs = (dict(sequenceToken=seq_token) if seq_token else {})
        log_events = list(map(self.make_message, messages))

        return self.client.put_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            logEvents=log_events,
            **kwargs
        )

    def make_message(self, message):
        ''' prepare a message to send to cloudwatch '''
        timestamp = int(message['__REALTIME_TIMESTAMP'].timestamp() * 1000)
        # remove unserialisable values
        message = {k: v for k, v in message.items() if isinstance(v, (str, int, uuid.UUID, datetime.datetime))}
        # encode entire message in json
        message = json.dumps(message, cls=JournalMsgEncoder)
        return dict(timestamp=timestamp, message=message)

    @staticmethod
    def retain_message(message, retention=datetime.timedelta(days=14)):
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

class LogGroupClient:
    ALREADY_EXISTS = 'ResourceAlreadyExistsException'
    THROTTLED = 'ThrottlingException'

    def __init__(self, log_group, parent):
        self.log_group = log_group
        self.parent = parent
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

    def group_messages(self, messages, maxlen=10, timespan=datetime.timedelta(hours=23)):
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
        ''' log the messages, then save the cursor '''
        if not messages:
            return

        while True:
            try:
                seq_token = self.get_seq_token(log_stream)
                self.parent.put_log_messages(self.log_group, log_stream, seq_token, messages)
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] != self.THROTTLED:
                    raise
            else:
                break
            time.sleep(1)
        # save last cursor
        self.parent.save_cursor(messages[-1]['__CURSOR'])

    def get_seq_token(self, log_stream):
        ''' get the sequence token for the stream '''

        streams = self.parent.client.describe_log_streams(logGroupName=self.log_group, logStreamNamePrefix=log_stream, limit=1)
        if streams['logStreams']:
            stream = streams['logStreams'][0]
            if stream['logStreamName'] == log_stream:
                # found the stream
                return stream.get('uploadSequenceToken')
        # no stream, create it
        self.create_log_stream(log_stream)

if __name__ == '__main__':
    import argparse
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

    client = CloudWatchClient(args.cursor, args.log_group_format, args.log_stream_format)

    while True:
        cursor = client.load_cursor()
        with systemd.journal.Reader(path=args.logs) as reader:
            if cursor:
                reader.seek_cursor(cursor)
            else:
                # no cursor, start from start of this boot
                reader.this_boot()
                reader.seek_head()

            reader = filter(CloudWatchClient.retain_message, reader)
            for (group, stream), messages in itertools.groupby(reader, key=client.group_messages):
                group.log_messages(stream, list(messages))
