from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.db import mesh_repos as mesh, schema


class MeshDomainTests(unittest.TestCase):
    def setUp(self):
        journal=patch('server.runtime.migration_journal.append_journal_event')
        journal.start(); self.addCleanup(journal.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'mesh.db'
        def connect():
            conn=sqlite3.connect(path)
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            self.addCleanup(conn.close)
            return conn
        self.connect=connect
        for module in (mesh,schema):
            patcher=patch.object(module,'get_connection',side_effect=connect)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher=patch.object(mesh,'data_root',return_value=Path(self.tmp.name))
        patcher.start(); self.addCleanup(patcher.stop)
        schema.init_db()
        with connect() as conn:
            for name in 'ABCD':
                conn.execute('INSERT INTO agent_templates(template_id,name,role,created_at,updated_at) VALUES (?,?,?,0,0)',(name,name,'role '+name))
        mesh.configure('A',['B','C'])
        mesh.configure('B',['C','D'])
        mesh.configure('C',['D'])

    def task(self,caller='A',target='B',**kwargs):
        return mesh.delegate(caller,target,'Produce evidence',['Accurate'],**kwargs)

    def assert_code(self,code,func,*args,**kwargs):
        with self.assertRaises(mesh.MeshError) as caught: func(*args,**kwargs)
        self.assertEqual(caught.exception.code,code)

    def test_parallel_coordination_resumes_after_all_children_are_accepted(self):
        first=self.task(root_request_id='parallel')
        second=self.task(target='C',root_request_id='parallel')
        root=first['parent_task_id']
        mesh.yield_task(root,'A','children',child_task_ids=[first['task_id'],second['task_id']])
        mesh.submit(first['task_id'],'B',{'summary':'first result'})
        mesh.review(first['task_id'],'A',1,'accept')
        with mesh._tx() as conn:
            mesh._queue_coordinations(conn)
        self.assertEqual(mesh.get_task(root)['status'],'waiting')
        mesh.submit(second['task_id'],'C',{'summary':'second result'})
        mesh.review(second['task_id'],'A',1,'accept')
        work=mesh.claim_work('after-restart')
        self.assertIsNotNone(work)
        self.assertEqual(work['task_id'],root)
        self.assertEqual(work['role'],'coordination')
        self.assertEqual(work['task']['wait_reason'],'')
        self.assertEqual(len(work['task']['outcomes']),2)
        self.assertIsNone(mesh.claim_work('duplicate'))
        mesh.finish_coordination(root,'A',{'summary':'Both results'})
        mesh.finish_work(work['work_id'],work['lease_token'],work['fencing_token'])
        self.assertIsNone(mesh.claim_work('finished'))

    def test_coordination_late_children_wait_includes_failed_branch(self):
        first=self.task(root_request_id='partial')
        second=self.task(target='C',root_request_id='partial')
        mesh.submit(first['task_id'],'B',{'summary':'first result'})
        mesh.review(first['task_id'],'A',1,'accept')
        mesh.cancel_task(second['task_id'],'A')
        root=first['parent_task_id']
        mesh.yield_task(root,'A','children',child_task_ids=[first['task_id'],second['task_id']])
        work=mesh.claim_work('worker')
        self.assertIsNotNone(work)
        self.assertEqual(work['task_id'],root)
        self.assertEqual({t['status'] for t in work['task']['outcomes']},{'completed','cancelled'})

    def test_coordination_children_wait_preserves_human_decision(self):
        task=self.task()
        root=task['parent_task_id']
        mesh.yield_task(root,'A','children',child_task_ids=[task['task_id']])
        mesh.request_decision(root,'A','permission',['approve','deny'],{'tool':'read_file'})
        mesh.submit(task['task_id'],'B',{'summary':'result'})
        mesh.review(task['task_id'],'A',1,'accept')
        self.assertIsNone(mesh.claim_work('worker'))
        self.assertEqual(mesh.get_task(root)['wait_reason'],'permission')

    def test_acl_scoped_reply_and_contract(self):
        self.assertEqual({p['agent_id'] for p in mesh.discover('A')},{'B','C'})
        self.assertEqual({p['agent_id'] for p in mesh.discover('A', 'B C')},{'B','C'})
        self.assert_code('permission_denied',self.task,target='D')
        t=self.task()
        mesh.configure('A',[])
        msg=mesh.send_message(t['task_id'],'B','clarification_request','Which period?')
        self.assertEqual(msg['recipient_id'],'A')
        self.assert_code('permission_denied',self.task,caller='B',target='A')
        self.assert_code('permission_denied',mesh.messages,t['task_id'],'C')
        self.assertNotIn('system_prompt',mesh.profile('B'))

    def test_nested_review_and_stale_submission(self):
        parent=self.task(); child=self.task('B','C',parent_task_id=parent['task_id'])
        self.assert_code('invalid_input',mesh.submit,parent['task_id'],'B',{'summary':'premature'})
        mesh.submit(child['task_id'],'C',{'summary':'evidence'})
        self.assert_code('permission_denied',mesh.review,child['task_id'],'C',1,'accept')
        mesh.review(child['task_id'],'B',1,'accept')
        mesh.submit(parent['task_id'],'B',{'summary':'delivery'})
        mesh.review(parent['task_id'],'A',1,'request_changes','Need sources')
        mesh.submit(parent['task_id'],'B',{'summary':'with sources'})
        self.assert_code('stale_revision',mesh.review,parent['task_id'],'A',1,'accept')
        self.assertEqual(mesh.review(parent['task_id'],'A',2,'accept')['status'],'completed')

    def test_idempotency_and_transaction_rollback(self):
        task=self.task(idempotency_key='same')
        self.assertEqual(task['task_id'],self.task(idempotency_key='same')['task_id'])
        self.assert_code('stale_revision',mesh.delegate,'A','B','different',['Accurate'],idempotency_key='same')
        msg=mesh.send_message(task['task_id'],'B','message','hello',idempotency_key='m')
        self.assertEqual(msg,mesh.send_message(task['task_id'],'B','message','hello',idempotency_key='m'))
        self.assertEqual(len(mesh.messages(task['task_id'])),2)

    def test_combined_parent_dependency_cycle_rejected(self):
        parent=self.task()
        self.assert_code('dependency_cycle',self.task,'B','C',parent_task_id=parent['task_id'],depends_on=[parent['task_id']])
        self.assertEqual(len(mesh.list_tasks()['items']),1)

    def test_cancel_cascades_and_fences_late_worker(self):
        parent=self.task(); child=self.task('B','C',parent_task_id=parent['task_id'])
        work=mesh.claim_work('worker')
        self.assertIsNotNone(work)
        result=mesh.cancel_task(parent['task_id'],'A')
        self.assertEqual(result['affected_count'],2)
        self.assertEqual(mesh.get_task(child['task_id'])['status'],'cancelled')
        with mesh.work_context(work['work_id'],work['lease_token'],work['fencing_token']):
            self.assert_code('stale_revision',mesh.submit,parent['task_id'],'B',{'summary':'late'})

    def test_failure_closes_descendants_partial_requires_initiator(self):
        parent=self.task(); child=self.task('B','C',parent_task_id=parent['task_id']); grand=self.task('C','D',parent_task_id=child['task_id'])
        mesh.fail_task(child['task_id'],'C','broken')
        self.assertEqual(mesh.get_task(grand['task_id'])['status'],'cancelled')
        self.assertEqual(mesh.get_task(parent['task_id'])['wait_reason'],'child_failed')
        self.assert_code('permission_denied',mesh.allow_partial,parent['task_id'],'B',[child['task_id']],'Proceed')
        mesh.allow_partial(parent['task_id'],'A',[child['task_id']],'Proceed with gaps')
        self.assertEqual(mesh.submit(parent['task_id'],'B',{'summary':'partial'})['status'],'submitted')

    def test_dependency_waits_for_acceptance_and_queue_serial(self):
        first=self.task(root_request_id='root')
        second=self.task(target='C',root_request_id='root',depends_on=[first['task_id']])
        work=mesh.claim_work('worker')
        self.assertEqual(work['task_id'],first['task_id'])
        mesh.submit(first['task_id'],'B',{'summary':'done'})
        self.assertTrue(mesh.finish_work(work['work_id'],work['lease_token'],work['fencing_token']))
        review=mesh.claim_work('reviewer')
        self.assertEqual(review['agent_id'],'A')
        self.assertIsNone(mesh.claim_work('blocked'))
        mesh.review(first['task_id'],'A',1,'accept')
        mesh.finish_work(review['work_id'],review['lease_token'],review['fencing_token'])
        self.assertEqual(mesh.claim_work('next')['task_id'],second['task_id'])

    def test_expired_lease_requires_reconciliation(self):
        task=self.task(); work=mesh.claim_work('worker')
        with self.connect() as conn: conn.execute('UPDATE mesh_inbox SET expires_at=0 WHERE work_id=?',(work['work_id'],))
        self.assertIsNone(mesh.claim_work('restart'))
        snapshot=mesh.get_task(task['task_id'])
        self.assertEqual(snapshot['wait_reason'],'reconciliation')
        self.assertFalse(mesh.finish_work(work['work_id'],work['lease_token'],work['fencing_token']))
        decision=snapshot['decisions'][0]
        mesh.decide(decision['decision_id'],'retry',decision['expected_task_revision'],{'evidence':'No external action executed'})
        self.assertIsNotNone(mesh.claim_work('restart'))

    def test_task_limit_decision_and_replay(self):
        task=self.task()
        with self.connect() as conn: conn.execute('UPDATE mesh_limits SET tasks=1 WHERE workflow_id=?',(task['workflow_id'],))
        blocked=self.task('B','C',parent_task_id=task['task_id'],idempotency_key='child')
        self.assertTrue(blocked['limit_reached'])
        decision=blocked['decision']
        child=mesh.decide(decision['decision_id'],'raise_limit',decision['expected_task_revision'],{'value':2})
        self.assertEqual(child['target_id'],'C')

    def test_artifact_scope_hash_and_path_escape(self):
        task=self.task(); root=Path(mesh.task_workspace(task['task_id'])); root.mkdir(parents=True)
        path=root/'report.txt'; path.write_text('result')
        artifact=mesh.register_artifact(task['task_id'],'B',str(path))
        self.assertNotIn('uri',artifact)
        self.assertEqual(mesh.artifact(artifact['artifact_id'],'A')['path'],str(path.resolve()))
        self.assert_code('permission_denied',mesh.artifact,artifact['artifact_id'],'C')
        self.assert_code('permission_denied',mesh.register_artifact,task['task_id'],'B',__file__)
        path.write_text('changed')
        self.assert_code('stale_revision',mesh.artifact,artifact['artifact_id'],'A')

    def test_permission_answer_does_not_accept_task(self):
        task=self.task()
        decision=mesh.request_decision(task['task_id'],'B','permission',['approve','deny','cancel'],{'fingerprint':'f','tool':'write'})
        mesh.decide(decision['decision_id'],'approve',decision['expected_task_revision'])
        self.assertEqual(mesh.consume_permission(task['task_id'],'B','f')['action'],'approve')
        self.assertEqual(mesh.get_task(task['task_id'])['status'],'queued')

    def test_migration_restart_and_coordination(self):
        schema.init_db()
        task=self.task(); work=mesh.claim_work('worker')
        mesh.submit(task['task_id'],'B',{'summary':'done'})
        mesh.finish_work(work['work_id'],work['lease_token'],work['fencing_token'])
        reviewer=mesh.claim_work('reviewer')
        mesh.review(task['task_id'],'A',1,'accept')
        mesh.finish_work(reviewer['work_id'],reviewer['lease_token'],reviewer['fencing_token'])
        final=mesh.claim_work('summary')
        self.assertEqual(final['role'],'coordination')
        mesh.finish_coordination(final['task_id'],'A',{'reply':'Delivered'})
        self.assertEqual(mesh.graph(task['workflow_id'])['workflow']['status'],'completed')

    def test_timeout_and_single_use_permission(self):
        task=self.task(); child=self.task('B','C',parent_task_id=task['task_id'])
        work=mesh.claim_work('worker')
        with self.connect() as conn:
            conn.execute('UPDATE workflow_attempts SET created_at=0 WHERE attempt_id=?',(work['attempt_id'],))
        self.assertFalse(mesh.heartbeat(work['work_id'],work['lease_token'],work['fencing_token']))
        self.assertEqual(mesh.get_task(task['task_id'])['status'],'timeout')
        self.assertEqual(mesh.get_task(child['task_id'])['status'],'cancelled')
        another=self.task()
        decision=mesh.request_decision(another['task_id'],'B','permission',['approve','deny','cancel'],{'fingerprint':'once'})
        mesh.decide(decision['decision_id'],'approve',decision['expected_task_revision'])
        self.assertIsNotNone(mesh.consume_permission(another['task_id'],'B','once'))
        self.assertIsNone(mesh.consume_permission(another['task_id'],'B','once'))

    def test_summary_returns_to_source_and_respects_clear_fence(self):
        for clear in (False,True):
            sid='source-'+str(clear)
            with self.connect() as conn: conn.execute('INSERT INTO session_fences(session_id) VALUES (?)',(sid,))
            task=self.task(origin_session_id=sid)
            mesh.submit(task['task_id'],'B',{'summary':'done'})
            mesh.review(task['task_id'],'A',1,'accept')
            if clear:
                with self.connect() as conn: conn.execute('UPDATE session_fences SET epoch=1 WHERE session_id=?',(sid,))
            mesh.finish_coordination(task['parent_task_id'],'A',{'reply':'Final summary'})
            with self.connect() as conn:
                count=conn.execute('SELECT count(*) FROM assistant_messages WHERE session_id=?',(sid,)).fetchone()[0]
                self.assertEqual(count,0 if clear else 1)

    def test_user_input_wait_does_not_consume_pending_work(self):
        task=self.task()
        decision=mesh.request_decision(task['task_id'],'B','user_input',['answer','cancel'],{'questions':['Which period?']})
        self.assertIsNone(mesh.claim_work('worker'))
        mesh.decide(decision['decision_id'],'answer',decision['expected_task_revision'],{'answer':'This year'},'answer-once')
        same=mesh.decide(decision['decision_id'],'answer',decision['expected_task_revision'],{'answer':'This year'},'answer-once')
        self.assertEqual(same['status'],'queued')
        self.assert_code('stale_revision',mesh.decide,decision['decision_id'],'answer',decision['expected_task_revision'],{'answer':'Another'},'answer-once')

    def test_yield_after_children_completed_does_not_lose_wake(self):
        task=self.task(); child=self.task('B','C',parent_task_id=task['task_id'])
        mesh.submit(child['task_id'],'C',{'summary':'done'})
        mesh.review(child['task_id'],'B',1,'accept')
        result=mesh.yield_task(task['task_id'],'B','children',child_task_ids=[child['task_id']])
        self.assertEqual(result['status'],'queued')

    def test_limits_are_atomic_and_root_budget_shared(self):
        parent=self.task(root_request_id='shared')
        peer=self.task(target='C',root_request_id='shared')
        self.assertEqual(parent['workflow_id'],peer['workflow_id'])
        with self.connect() as conn: conn.execute('UPDATE mesh_limits SET tasks=2 WHERE workflow_id=?',(parent['workflow_id'],))
        blocked=self.task('B','D',parent_task_id=parent['task_id'])
        self.assertTrue(blocked['limit_reached'])
        graph=mesh.graph(parent['workflow_id'])
        self.assertEqual(len([node for node in graph['nodes'] if node['kind']=='agent']),3)
        self.assertEqual(len([node for node in graph['nodes'] if node['kind']=='summary']),1)

    def test_graph_projects_serial_delegation_as_assistant_flow(self):
        first=self.task()
        second=self.task('B','C',parent_task_id=first['task_id'])
        graph=mesh.graph(first['workflow_id'])
        edges={(edge['source'],edge['target']) for edge in graph['edges']}
        summary=next(node['node_id'] for node in graph['nodes'] if node['kind']=='summary')
        self.assertIn(('agent:origin:A','agent:task:'+first['task_id']),edges)
        self.assertIn(('agent:task:'+first['task_id'],'agent:task:'+second['task_id']),edges)
        self.assertIn(('agent:task:'+second['task_id'],summary),edges)
        self.assertNotIn(('agent:task:'+first['task_id'],summary),edges)

    def test_graph_projects_branching_delegation_and_leaf_summary(self):
        first=self.task(root_request_id='branch')
        second=self.task(target='C',root_request_id='branch')
        child=self.task('B','D',parent_task_id=first['task_id'])
        graph=mesh.graph(first['workflow_id'])
        edges={(edge['source'],edge['target']) for edge in graph['edges']}
        summary=next(node['node_id'] for node in graph['nodes'] if node['kind']=='summary')
        self.assertIn(('agent:origin:A','agent:task:'+first['task_id']),edges)
        self.assertIn(('agent:origin:A','agent:task:'+second['task_id']),edges)
        self.assertIn(('agent:task:'+first['task_id'],'agent:task:'+child['task_id']),edges)
        self.assertIn(('agent:task:'+second['task_id'],summary),edges)
        self.assertIn(('agent:task:'+child['task_id'],summary),edges)
        self.assertNotIn(('agent:task:'+first['task_id'],summary),edges)

    def test_artifact_creator_can_explicitly_forward_without_message_access(self):
        parent=self.task(); root=Path(mesh.task_workspace(parent['task_id'])); root.mkdir(parents=True)
        path=root/'evidence.txt'; path.write_text('Selected evidence')
        item=mesh.register_artifact(parent['task_id'],'B',str(path))
        self.assertTrue(item['can_forward'])
        self.assertNotIn('allow_forward',item['metadata'])
        self.assertTrue(mesh.get_task(parent['task_id'],'B')['artifacts'][0]['can_forward'])
        self.assertTrue(mesh.artifact(item['artifact_id'],'B')['can_forward'])
        child=self.task('B','C',parent_task_id=parent['task_id'],artifact_refs=[item['artifact_id']])
        readable=mesh.artifact(item['artifact_id'],'C')
        self.assertEqual(readable['artifact_id'],item['artifact_id'])
        self.assertEqual(Path(readable['path']).read_text(),'Selected evidence')
        self.assertFalse(readable['can_forward'])
        self.assertNotIn('allow_forward',readable['metadata'])
        self.assert_code('permission_denied',mesh.messages,parent['task_id'],'C')
        self.assertEqual(mesh.submit(child['task_id'],'C',{'summary':'Checked','artifact_refs':[item['artifact_id']]})['status'],'submitted')

    def test_noncreator_forward_capability_remains_denied_without_override(self):
        parent=self.task(); root=Path(mesh.task_workspace(parent['task_id'])); root.mkdir(parents=True)
        path=root/'evidence.txt'; path.write_text('Selected evidence')
        item=mesh.register_artifact(parent['task_id'],'B',str(path))
        self.assertFalse(mesh.get_task(parent['task_id'],'A')['artifacts'][0]['can_forward'])
        self.assertFalse(mesh.artifact(item['artifact_id'],'A')['can_forward'])
        self.assert_code('permission_denied',self.task,'A','C',artifact_refs=[item['artifact_id']])
        child=self.task('B','C',parent_task_id=parent['task_id'],artifact_refs=[item['artifact_id']])
        self.assert_code('permission_denied',self.task,'C','D',parent_task_id=child['task_id'],artifact_refs=[item['artifact_id']])
        with self.connect() as conn:
            import json
            metadata=json.loads(conn.execute('SELECT metadata_json FROM workflow_artifacts WHERE artifact_id=?',(item['artifact_id'],)).fetchone()[0])
            self.assertFalse(metadata['allow_forward'])
            metadata['allow_forward']=True
            conn.execute('UPDATE workflow_artifacts SET metadata_json=? WHERE artifact_id=?',(json.dumps(metadata),item['artifact_id']))
        self.assertTrue(mesh.artifact(item['artifact_id'],'A')['can_forward'])
        self.assertTrue(mesh.get_task(parent['task_id'],'A')['artifacts'][0]['can_forward'])

    def test_coordination_decision_is_visible_only_to_source_and_user(self):
        task=self.task()
        decision=mesh.request_decision(task['parent_task_id'],'A','user_input',['answer','cancel'],{'questions':['Publish summary?']})
        self.assertIn(decision['decision_id'],[d['decision_id'] for d in mesh.get_task(task['task_id'])['decisions']])
        self.assertIn(decision['decision_id'],[d['decision_id'] for d in mesh.get_task(task['task_id'],'A')['decisions']])
        self.assertNotIn(decision['decision_id'],[d['decision_id'] for d in mesh.get_task(task['task_id'],'B')['decisions']])
        self.assertNotIn('coordination_wait_reason',mesh.list_tasks(actor_id='B')['items'][0])

    def test_restart_recovers_cancelled_worker_stopping_receipt(self):
        task=self.task(); work=mesh.claim_work('old-process')
        mesh.cancel_task(task['task_id'])
        self.assertEqual(mesh.get_task(task['task_id'])['executions_stopping'],1)
        self.assertEqual(mesh.recover_stopped_after_restart(),1)
        self.assertEqual(mesh.get_task(task['task_id'])['executions_stopping'],0)

    def _usage_event(self,task_id,model,event_id='event',session_id='usage-session'):
        with self.connect() as conn:
            conn.execute('INSERT INTO mesh_task_sessions(task_id,agent_id,session_id) VALUES (?,?,?) ON CONFLICT(task_id,agent_id) DO UPDATE SET session_id=excluded.session_id',(task_id,'B',session_id))
            conn.execute('INSERT INTO llm_usage_events(event_id,call_id,run_id,session_id,agent_id,call_kind,provider,model,status,usage_source,cache_usage_source,input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,total_tokens,started_at,completed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(event_id,event_id,'run-'+event_id,session_id,'B','chat','provider',model,'completed','provider','provider',1000,400,600,200,1200,10,12.5,13))

    def test_usage_pricing_cached_tokens_and_duration(self):
        task=self.task()
        self._usage_event(task['task_id'],'known')
        prices={'known':{'currency':'CNY','input_per_million':3,'cached_input_per_million':0.1,'output_per_million':9}}
        with patch('server.runtime.pricing._pricing_models',return_value=prices):
            usage=mesh.get_task(task['task_id'])['usage']
        self.assertAlmostEqual(usage['cost'],0.00364)
        self.assertEqual(usage['cached_input_tokens'],400)
        self.assertEqual(usage['uncached_input_tokens'],600)
        self.assertEqual(usage['duration_seconds'],2.5)
        self.assertEqual(usage['call_count'],1)
        self.assertEqual(usage['currency'],'CNY')

    def test_usage_partial_pricing_is_not_presented_as_total(self):
        task=self.task()
        self._usage_event(task['task_id'],'known')
        self._usage_event(task['task_id'],'missing','another')
        with patch('server.runtime.pricing._pricing_models',return_value={'known':{'currency':'USD','input_per_million':1,'cached_input_per_million':0.1,'output_per_million':2}}), patch('server.runtime.pricing.usd_to_cny_rate',return_value=7):
            usage=mesh.get_task(task['task_id'])['usage']
        self.assertIsNone(usage['cost'])
        self.assertIsNone(usage['spend_cny'])
        self.assertAlmostEqual(usage['known_spend_cny'],0.00728)
        self.assertEqual(usage['unpriced_model_count'],1)
        self.assertEqual(usage['total_tokens'],2400)
        self.assertEqual(usage['duration_seconds'],5)

    def test_usage_missing_exchange_rate_and_empty_usage_are_unknown(self):
        task=self.task()
        self.assertIsNone(mesh.get_task(task['task_id'])['usage']['cost'])
        self._usage_event(task['task_id'],'usd')
        with patch('server.runtime.pricing._pricing_models',return_value={'usd':{'currency':'USD','input_per_million':1,'cached_input_per_million':0.1,'output_per_million':2}}), patch('server.runtime.pricing.usd_to_cny_rate',return_value=None):
            usage=mesh.get_task(task['task_id'])['usage']
        self.assertIsNone(usage['cost'])
        self.assertEqual(usage['models'][0]['price_status'],'exchange_rate_unavailable')


if __name__=='__main__': unittest.main()
