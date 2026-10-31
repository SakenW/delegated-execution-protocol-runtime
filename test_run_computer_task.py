#!/usr/bin/env python3
"""Drive the real JSONL approval client against a deterministic local server."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent))
from agent_profiles import load_binding
from test_profile_fixtures import write_profiles
from run_computer_task import run, load_task

CHILD = '22222222-2222-2222-2222-222222222222'
PARENT = '11111111-1111-1111-1111-111111111111'
FAKE = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
child='22222222-2222-2222-2222-222222222222';parent='11111111-1111-1111-1111-111111111111'
case=os.environ.get('COMPUTER_TEST_CASE','ok');root=parent
history=Path('wire.jsonl').open('w')
def out(*messages):
 # One write deliberately bursts several lines: a buffered select reader used to stall here.
 sys.stdout.write(''.join(json.dumps(x)+'\n' for x in messages));sys.stdout.flush()
for line in sys.stdin:
 m=json.loads(line);history.write(json.dumps(m)+'\n');history.flush();method=m.get('method')
 if method=='initialize':out({'id':m['id'],'result':{}})
 elif method in ['thread/start','thread/resume']:
  assert m['params']['approvalPolicy']=='on-request'
  root=child if method=='thread/resume' else parent
  out({'id':m['id'],'result':{'thread':{'id':root}}})
 elif method=='turn/start':
  p=Path(os.environ['CODEX_HOME'])/'sessions/2026/10/30'/('rollout-'+child+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
  ctx={'turn_id':'turn-child','model':'gpt-6.1-terra','effort':'medium','sandbox_policy':{'type':'workspace-write'}}
  source={'subagent':{'thread_spawn':{'agent_role':'delegated_computer_worker','parent_thread_id':parent}}}
  if case=='wrong-model':ctx['model']='different-model'
  if case=='wrong-permissions':ctx['sandbox_policy']['type']='danger-full-access'
  if case=='stale-turn':ctx['turn_id']='old-turn'
  if case=='wrong-role':source['subagent']['thread_spawn']['agent_role']='delegated_worker'
  p.write_text(json.dumps({'type':'session_meta','payload':{'source':source,'model_provider':'openai'}})+'\n'+json.dumps({'type':'turn_context','payload':ctx})+'\n'+json.dumps({'type':'response_item','payload':{'type':'message','role':'developer','content':[{'type':'input_text','text':'stale instructions' if case=='wrong-instructions' else 'Use assigned ownership'}]}})+'\n')
  q={'threadId':child,'turnId':'turn-child','serverName':'cua_repl','mode':'form','message':'App access',
     'requestedSchema':{'type':'object','properties':{}},'_meta':{'codex_approval_kind':'mcp_tool_call','connector_id':'computer-use','riskLevel':'low','tool_name':'get_app_state','tool_params':{'app':'com.apple.calculator'}}}
  if case=='other-app':q['_meta']['tool_params']['app']='com.apple.mail'
  if case=='other-tool':q['_meta']['tool_name']='press_key'
  if case=='high-risk':q['_meta']['riskLevel']='high'
  if case=='action-form':q['requestedSchema']['properties']={'confirm':{'type':'boolean'}}
  if case=='device-proof':q['mode']='openai/userVerification'
  if case=='root-request':q['threadId']=parent
  if case=='action-confirmation':q['_meta']['codex_request_type']='approval_request'
  started={'method':'turn/started','params':{'threadId':root,'turn':{'id':'turn-child' if root==child else 'turn-root'}}}
  spawned={'method':'item/completed','params':{'threadId':parent,'item':{'type':'collabAgentToolCall','tool':'spawnAgent','status':'completed','senderThreadId':parent,'receiverThreadIds':[child]}}}
  approval={'id':90,'method':'mcpServer/elicitation/request','params':q}
  if case=='unknown-request':approval['method']='item/commandExecution/requestApproval'
  if root==parent:out(started,spawned,approval)
  else:out(started,approval)
 elif m.get('id')==90:
  if case=='timeout':continue
  out({'method':'item/completed','params':{'threadId':child,'item':{'type':'agentMessage','text':'fixture UI evidence'}}},
      {'method':'turn/completed','params':{'threadId':root,'turn':{'id':'turn-child' if root==child else 'turn-root','status':'completed'}}})
'''

class ComputerTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root/'home'; write_profiles(self.home)
        self.cli = self.root/'fake-codex'; self.cli.write_text(FAKE); self.cli.chmod(0o755)
        self.binding = load_binding(self.home,'delegated_computer_worker')
        self.binding['developer_instructions'] = 'Use assigned ownership'
        self.task = {'task':'Read Calculator only','authorization':'User explicitly authorized this Calculator test',
                     'apps':{'com.apple.calculator':['get_app_state']}}
    def tearDown(self): self.tmp.cleanup()
    def execute(self, case='ok', resume=None):
        out=self.root/('out-'+case+('-resume' if resume else ''))
        args=argparse.Namespace(output_dir=out,codex_home=self.home,cli=str(self.cli),resume_thread=resume,timeout=1 if case=='timeout' else 5)
        with patch.dict(os.environ,{'COMPUTER_TEST_CASE':case}):result=run(args,self.task,self.binding)
        wire=[json.loads(x) for x in (out/'wire.jsonl').read_text().splitlines()]
        return result,wire
    def test_fresh_named_task_and_burst_reader(self):
        result,wire=self.execute()
        self.assertEqual(result['status'],'turn_completed')
        self.assertTrue(result['binding_verified']); self.assertTrue(result['main_verification_required'])
        self.assertEqual(result['resume_thread'],CHILD)
        self.assertEqual(next(m['result'] for m in wire if m.get('id')==90),{'action':'accept','content':{}})
        self.assertEqual(sum(m.get('method')=='thread/start' for m in wire),1)
    def test_resume_reuses_child_without_coordinator(self):
        self.execute()
        result,wire=self.execute(resume=CHILD)
        self.assertEqual(result['status'],'turn_completed')
        self.assertFalse(any(m.get('method')=='thread/start' for m in wire))
    def test_out_of_scope_and_sensitive_requests_are_not_approved(self):
        for case in ['other-app','other-tool','high-risk','action-form','device-proof','root-request','action-confirmation','unknown-request']:
            with self.subTest(case=case):
                result,wire=self.execute(case)
                self.assertEqual(result['status'],'approval_required')
                self.assertFalse(any(m.get('result',{}).get('action')=='accept' for m in wire))
                self.assertTrue(any(m.get('method')=='turn/interrupt' for m in wire) or result['accepted_app_requests']==0)
    def test_runtime_mismatch_never_grants_access(self):
        for case in ['wrong-model','wrong-role','wrong-permissions','stale-turn','wrong-instructions']:
            with self.subTest(case=case):
                result,wire=self.execute(case)
                self.assertIn(result['status'],['failed','approval_required'])
                self.assertFalse(any(m.get('result',{}).get('action')=='accept' for m in wire))
    def test_timeout_does_not_retry(self):
        result,wire=self.execute('timeout')
        self.assertEqual(result['status'],'timeout')
        self.assertEqual(sum(m.get('method')=='turn/start' for m in wire),1)
    def test_evidence_is_not_overwritten(self):
        self.execute()
        with self.assertRaises(FileExistsError): self.execute()
    def test_missing_user_scope_rejected(self):
        p=self.root/'task.json';p.write_text(json.dumps({**self.task,'authorization':''}))
        with self.assertRaises(ValueError):load_task(p)

if __name__=='__main__':unittest.main()
