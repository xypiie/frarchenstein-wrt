# SPDX-License-Identifier: GPL-2.0-only
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

source=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(source))
spec=importlib.util.spec_from_file_location('hybrid_apps',source/'apps.py')
apps=importlib.util.module_from_spec(spec);spec.loader.exec_module(apps)

class ApplicationContracts(unittest.TestCase):
    def setUp(self):
        self.app={'name':'fixture','uid':4500,'gid':4500,'cwd':'/srv/router-apps/fixture',
                  'command':['/usr/bin/python3','server.py'],'check':['/usr/bin/python3','check.py'],
                  'listeners':[{'address':'192.168.77.1','port':9000}],
                  'python':{'requirements':'requirements.lock'}}
    def validate(self):return apps.validate({'format':1,'apps':[self.app]})
    def test_explicit_contract(self):self.assertEqual(list(self.validate()),['fixture'])
    def test_root_identity_refused(self):
        self.app['uid']=0
        with self.assertRaises(ValueError):self.validate()
    def test_data_escape_refused(self):
        self.app['cwd']='/srv/router-apps/fixture/../../root'
        with self.assertRaises(ValueError):self.validate()
    def test_shell_string_refused(self):
        self.app['command']='python3 server.py'
        with self.assertRaises(ValueError):self.validate()
    def test_environment_injection_refused(self):
        self.app['environment']={'LD_PRELOAD':'evil.so'}
        with self.assertRaises(ValueError):self.validate()
    def test_implicit_wildcard_listener_refused(self):
        self.app['listeners'][0]['address']='0.0.0.0'
        with self.assertRaises(ValueError):self.validate()
    def test_conflicting_listener_refused(self):
        second=copy.deepcopy(self.app);second.update(name='second',uid=4501,gid=4501,cwd='/srv/router-apps/second')
        with self.assertRaises(ValueError):apps.validate({'format':1,'apps':[self.app,second]})
    def test_shared_user_refused(self):
        second=copy.deepcopy(self.app);second.update(name='second',cwd='/srv/router-apps/second',listeners=[])
        with self.assertRaises(ValueError):apps.validate({'format':1,'apps':[self.app,second]})
    def test_unlocked_requirements_refused(self):
        self.app['python']={'requirements':'requirements.txt'}
        with self.assertRaises(ValueError):self.validate()

    def test_package_option_injection_refused(self):
        self.app['packages']=['--nodeps']
        with self.assertRaises(ValueError):self.validate()

    def test_provision_refuses_before_writes_without_shared_mount(self):
        with patch.object(apps,'check_mounts',side_effect=ValueError('unmounted')), patch.object(apps,'registry') as registry:
            with self.assertRaises(ValueError):apps.provision()
            registry.assert_not_called()
