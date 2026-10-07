import sys
import tempfile
import unittest
from pathlib import Path

from personal_vault.pi_rpc import PiRpc, RpcError


FAKE = r'''
import json,sys
def emit(value): print(json.dumps(value,ensure_ascii=False),flush=True)
for line in sys.stdin.buffer:
    cmd=json.loads(line);kind=cmd['type'];data={}
    if kind=='disconnect':sys.exit(0)
    if kind=='bad':
        emit(dict(type='response',id=cmd['id'],success=False,error='synthetic failure'));continue
    if kind=='get_state': data={'sessionFile':'/tmp/fake-session.jsonl','messageCount':2}
    if kind=='prompt':data={'disposition':'started'}
    emit(dict(type='response',id=cmd['id'],success=True,data=data))
    if kind=='prompt':
        emit(dict(type='tool_execution_end',isError=True,result={'text':'synthetic tool error'}))
        emit(dict(type='message_update',text='one\u2028two'))
        emit(dict(type='agent_settled'))
    if kind=='abort':emit(dict(type='agent_settled'))
'''


class RpcTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'fake.py';path.write_text(FAKE)
        self.client=PiRpc([sys.executable,str(path)])
        self.addCleanup(self.client.close)
        self.client.start()

    def test_events_command_failure_and_cancellation(self):
        events=self.client.prompt('hello')
        self.assertTrue(events[0]['isError'])
        self.assertEqual(events[1]['text'],'one\u2028two')
        with self.assertRaisesRegex(RpcError,'synthetic failure'):
            self.client.request('bad')
        self.client.cancel()
        self.assertEqual(self.client.wait_settled()[-1]['type'],'agent_settled')

    def test_disconnect_and_resume_do_not_replay_prompt(self):
        with self.assertRaisesRegex(RpcError,'closed'):
            self.client.request('disconnect')
        state=self.client.reconnect()
        self.assertEqual(state['sessionFile'],'/tmp/fake-session.jsonl')
        self.assertTrue(self.client.events.empty())
