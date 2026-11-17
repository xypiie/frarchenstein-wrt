# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('interface_runtime',Path(__file__).resolve().parents[1]/'src/runtime.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)

class InterfaceIdentity(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'frarchenstein').mkdir();self.net=self.root/'net';self.net.mkdir()
        for name,mac in [('eth0','52:54:00:72:00:02'),('eth1','52:54:00:72:00:01')]:
            (self.net/name).mkdir();(self.net/name/'address').write_text(mac)
    def config(self,value):(self.root/'frarchenstein/interfaces.json').write_text(json.dumps(value))
    def test_reversed_probe_order_maps_by_mac(self):
        self.config({'lan0':'52:54:00:72:00:01','wan0':'52:54:00:72:00:02'})
        with patch.object(runtime,'run') as run:
            runtime.name_interfaces(self.root,self.net)
            self.assertEqual(run.call_args_list[0].args,('/usr/bin/ip','link','set','dev','eth1','down'))
            self.assertEqual(run.call_args_list[-1].args,('/usr/bin/ip','link','set','dev','rpi_tmp1','name','wan0'))
    def test_missing_identity_refuses_before_network_changes(self):
        self.config({'lan0':'52:54:00:72:00:ff'})
        with patch.object(runtime,'run') as run:
            with self.assertRaises(ValueError):runtime.name_interfaces(self.root,self.net)
            run.assert_not_called()
    def test_duplicate_identity_refused(self):
        self.config({'lan0':'52:54:00:72:00:01','wan0':'52:54:00:72:00:01'})
        with patch.object(runtime,'run') as run:
            with self.assertRaises(ValueError):runtime.name_interfaces(self.root,self.net)
            run.assert_not_called()
    def test_name_collision_refused(self):
        (self.net/'lan0').mkdir();(self.net/'lan0/address').write_text('52:54:00:72:00:ff')
        self.config({'lan0':'52:54:00:72:00:01'})
        with patch.object(runtime,'run') as run:
            with self.assertRaises(ValueError):runtime.name_interfaces(self.root,self.net)
            run.assert_not_called()
