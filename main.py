import urllib.request
import json
import uuid
import datetime
import time
import itertools
from functools import lru_cache

import systemd.journal
import boto3
import botocore

@lru_cache(1)
def get_instance_id():
    URL = 'http://169.254.169.254/latest/meta-data/instance-id'
    with urllib.request.urlopen(URL) as src:
        return src.read().decode()

class JournalMsgEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.timestamp()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

class CloudWatchClient:
    def __init__(self, cursor_path, log_group_format, log_stream_format):
        self.client = boto3.client('logs')
        self.cursor_path = cursor_path
        self.log_group_format = log_group_format
        self.log_stream_format = log_stream_format
        self.log_groups = {}

    def group_messages(self, msg):
        msg = self.normalise_message(msg)
        group = self.log_group_format.format(msg)
        group = self.log_group_client(group)
        stream = self.log_stream_format(msg)
        return (group, stream)

    def log_group_client(self, name):
        try:
            return self.log_groups[name]
        except KeyError:
            pass
        client = self.log_groups[name] = LogGroupClient(name, self)
        return client

    def normalise_unit(self, unit):
        if '@' in unit:
            # remove templating in unit names e.g. sshd@127.0.0.1:12345.service -> sshd.service
            unit = '.'.join(( unit.partition('@')[0], unit.rpartition('.')[2] ))
        return unit

    def normalise_message(self, msg):
        msg = msg.copy()

        if 'USER_UNIT' in msg:
            msg['$UNIT'] = self.normalise_unit(msg['USER_UNIT'])
        elif '_SYSTEMD_UNIT' in msg:
            msg['$UNIT'] = self.normalise_unit(msg['_SYSTEMD_UNIT'])

        if 'CONTAINER_NAME' in msg and msg.get('_SYSTEMD_UNIT') == 'docker.service':
            msg['$DOCKER_CONTAINER'] = msg['CONTAINER_NAME']

        msg['$INSTANCE_ID'] = get_instance_id()

        msg['$DEFAULT'] = '[other]'

        return msg

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
            self.client.create_log_group(logGroupName=self.log_group)
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
                seq_token = self.get_seq_token()
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

        streams = self.client.describe_log_streams(logGroupName=self.log_group, logStreamNamePrefix=log_stream, limit=1)
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
    parser.add_argument('--cursor', required=True,
                        help='Store/read the journald cursor in this file')
    parser.add_argument('--logs', default='/var/log/journal',
                        help='Directory to journald logs (default: %(default)s)')
    group.add_argument('--log-group-format')
    group.add_argument('--log-stream-format')
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
