from unittest import TestCase
from unittest.mock import patch, mock_open, Mock
import json
import urllib.request

from main import Format, IDENTITY_DOC_URL, get_instance_identity_document

IDENTITY_DOC_STR = b'''{
  "devpayProductCodes" : null,
  "availabilityZone" : "us-east-1d",
  "privateIp" : "10.158.112.84",
  "version" : "2010-08-31",
  "region" : "us-east-1",
  "instanceId" : "i-1234567890abcdef0",
  "billingProducts" : null,
  "instanceType" : "t1.micro",
  "accountId" : "123456789012",
  "pendingTime" : "2015-11-19T16:32:11Z",
  "imageId" : "ami-5fb8c835",
  "kernelId" : "aki-919dcaf8",
  "ramdiskId" : null,
  "architecture" : "x86_64"
}
'''

IDENTITY_DOC = json.loads(IDENTITY_DOC_STR.decode('utf-8'))

@patch('main.get_instance_identity_document', return_value=IDENTITY_DOC, autospec=True)
class FormatterTest(TestCase):
    def test_default_formatting(self, _):
        ''' test formatting is same as default '''
        for fmt, kwargs in [
            ['string', {}],
            ['abc {d}', {'d': 123}],
            ['{b} {a}', {'a': 123, 'b': 456}],
            ['formatting {x:03}', {'x': 2}],
        ]:
            self.assertEqual(Format(fmt, **kwargs), fmt.format(**kwargs))

        self.assertEqual(Format('{}', 123), '{}'.format(123))
        self.assertRaises(KeyError, Format, '{a}')

    def test_formatting_defaults(self, _):
        ''' test the a|b|c fallthrough defaulting '''
        self.assertEqual(Format('xyz {a|b|c} 123', a=1, b=2, c=3), 'xyz 1 123')
        self.assertEqual(Format('xyz {a|b|c} 123', b=2, c=3), 'xyz 2 123')
        self.assertEqual(Format('xyz {a|b|c} 123', c=3), 'xyz 3 123')
        self.assertRaises(KeyError, Format, 'xyz {a|b|c} 123')

    def test_string_formatting(self, _):
        ''' test when key is a string '''
        self.assertEqual(Format('xyz {a|b|"hello"} 123', b=5), 'xyz 5 123')
        self.assertEqual(Format('xyz {a|b|"hello"} 123'), 'xyz hello 123')
        self.assertEqual(Format("xyz {a|b|'hello'} 123"), 'xyz hello 123')

    def test_identity_doc_formatting(self, _):
        ''' test variables in the identity doc '''
        self.assertEqual(Format('xyz {$instanceId}'), 'xyz ' + IDENTITY_DOC['instanceId'])
        self.assertEqual(Format('xyz {$region}'), 'xyz ' + IDENTITY_DOC['region'])
        self.assertEqual(Format('xyz {invalid|$region}'), 'xyz ' + IDENTITY_DOC['region'])

    def test_journald_vars(self, _):
        ''' test some convenience vars made from journald fields '''
        # test $unit
        self.assertEqual(Format('xyz {$unit}', _SYSTEMD_UNIT='systemd_unit', **{'$unit': 'not used'}), 'xyz systemd_unit')
        self.assertEqual(Format('xyz {$unit}', USER_UNIT='user_unit', _SYSTEMD_UNIT='not used', **{'$unit': 'not used'}), 'xyz user_unit')
        # test templated unit
        self.assertEqual(Format('xyz {$unit}', _SYSTEMD_UNIT='systemd_unit@arg.service', **{'$unit': 'not used'}), 'xyz systemd_unit.service')
        # test no unit name found
        self.assertEqual(Format('xyz {$unit}', **{'$unit': 'hello'}), 'xyz hello')
        # docker container
        self.assertEqual(Format('xyz {$docker_container}', _SYSTEMD_UNIT='docker.service', CONTAINER_NAME='container', **{'$docker_container': 'not used'}), 'xyz container')
        self.assertEqual(Format('xyz {$docker_container}', CONTAINER_NAME='container', **{'$docker_container': 'hello'}), 'xyz hello')
        self.assertEqual(Format('xyz {$docker_container}', _SYSTEMD_UNIT='docker.service', **{'$docker_container': 'hello'}), 'xyz hello')

    def test_default_special_vars(self, _):
        ''' test when $var not found '''
        self.assertEqual(Format('xyz {$other}', **{'$other': 'hello'}), 'xyz hello')
        self.assertRaises(KeyError, Format, 'xyz {$not_found}')

class InstanceIdentityDocTest(TestCase):
    DATA = dict(a=123, b='xyz')
    NULL_DATA = dict(a=123, b='xyz', c=None)

    def setUp(self):
        # clear the lru_cache every time
        get_instance_identity_document.cache_clear()

    @patch('urllib.request.urlopen', mock_open(read_data=json.dumps(DATA)))
    def test_get_instance_identity_document(self):
        self.assertEqual(get_instance_identity_document(), self.DATA)
        urllib.request.urlopen.assert_called_with(IDENTITY_DOC_URL)

    @patch('urllib.request.urlopen', mock_open(read_data=json.dumps(NULL_DATA)))
    def test_none_values_removed(self):
        ''' it drops where values are null '''
        self.assertEqual(get_instance_identity_document(), self.DATA)
        urllib.request.urlopen.assert_called_with(IDENTITY_DOC_URL)
