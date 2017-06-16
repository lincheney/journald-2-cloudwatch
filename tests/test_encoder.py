from unittest import TestCase
import datetime
import uuid
import json

import main

class JsonEncodeTest(TestCase):
    def dumps(self, value):
        return json.dumps(value, cls=main.JournalMsgEncoder)

    def test_encode_default(self):
        ''' test encoding is same as defaults '''
        for obj in [
            "string",
            123,
            123.45,
            [1, 2, 'x'],
            dict(key='value'),
            [1, dict(key=dict(nested=[])), ['a', 'b', 'c']],
        ]:
            self.assertEqual(self.dumps(obj), json.dumps(obj))

        self.assertRaises(TypeError, self.dumps, object())
        self.assertRaises(TypeError, self.dumps, {'dict': object()})

    def test_encode_datetime(self):
        ''' test encoding datetime '''
        obj = datetime.datetime.now()
        self.assertEqual(self.dumps(obj), json.dumps(obj.timestamp()))

    def test_encode_uuid(self):
        ''' test encoding uuids '''
        obj = uuid.uuid4()
        self.assertEqual(self.dumps(obj), json.dumps(str(obj)))
