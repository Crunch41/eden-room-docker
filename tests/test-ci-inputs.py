import datetime as dt
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import ci_inputs as m

class Inputs(unittest.TestCase):
    def setUp(self):
        self.now=dt.datetime(2026,10,7,tzinfo=dt.timezone.utc)
        self.inputs={'upstreams':{'x':'a'*40},'bases':{'image':'sha256:'+'b'*64},'source':'c'*64}
        self.previous={**self.inputs,'published_at':self.now.isoformat()}
    def test_unchanged_skips(self):self.assertEqual(m.decision(self.inputs,self.previous,'schedule',self.now),(False,False,[]))
    def test_each_input_change_builds(self):
        for key in ['upstreams','bases','source']:
            self.assertTrue(m.decision({**self.inputs,key:{}},self.previous,'schedule',self.now)[0])
    def test_failed_publication_has_no_new_record_and_retries(self):
        current={**self.inputs,'source':'new'}
        for _ in range(2):self.assertTrue(m.decision(current,self.previous,'schedule',self.now)[0])
    def test_manual_and_push_keep_validation(self):
        for event in ['push','workflow_dispatch']:self.assertTrue(m.decision(self.inputs,self.previous,event,self.now)[0])
    def test_weekly_packages_refresh_with_unchanged_source(self):
        build,refresh,_=m.decision(self.inputs,self.previous,'schedule',self.now+dt.timedelta(days=7))
        self.assertTrue(build and refresh)
    def test_missing_record_builds(self):self.assertTrue(m.decision(self.inputs,{},'schedule',self.now)[0])
    def test_multistage_only_external_images(self):
        self.assertEqual(m.base_images('FROM ubuntu:24.04 AS build\nFROM build AS test\nFROM scratch\nFROM ubuntu:24.04\n'),['ubuntu:24.04'])
    def test_dynamic_from_fails_closed(self):
        with self.assertRaises(ValueError):m.base_images('FROM $BASE')

if __name__=='__main__':unittest.main()
