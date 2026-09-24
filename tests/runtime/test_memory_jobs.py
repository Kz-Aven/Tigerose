import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.db import memory_store
from server.runtime import memory_jobs as jobs
from session_store import SessionManager


class MemoryJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.conn = self.connect()
        self.conn.executescript('''
            CREATE TABLE assistant_memories (memory_id TEXT PRIMARY KEY, template_id TEXT,
                body TEXT, source_groups TEXT, source TEXT, ts REAL);
            CREATE TABLE session_fences (session_id TEXT PRIMARY KEY, epoch INTEGER, deleted_at REAL);
            CREATE TABLE project_groups (group_id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE group_memberships (group_id TEXT, template_id TEXT);
            INSERT INTO session_fences VALUES ('session',0,NULL);
            INSERT INTO project_groups VALUES ('group','active');
            INSERT INTO group_memberships VALUES ('group','owner');
        ''')
        memory_store.migrate(self.conn)
        jobs.migrate(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, source='run', messages=None, group_id=''):
        job = jobs.enqueue_completed('owner', 'session', 0, source, messages or [
            {'role':'user', 'content':'以后生图使用供应商 https://example.com/docs'},
            {'role':'assistant', 'content':'已完成并验证接口'}], group_id, conn=self.conn)
        self.conn.commit()
        return job

    def worker(self, extractor=None):
        return jobs.MemoryWorker(self.connect, extractor or (lambda job,chunk,index: [
            {'body':'生图供应商接口文档 https://example.com/docs', 'type':'reference'}]))

    def candidates(self):
        result = jobs.list_candidates(self.conn, 'owner')
        self.conn.commit()
        return result

    def decide(self, candidate, confirm=True):
        self.conn.execute('BEGIN IMMEDIATE')
        result = jobs.decide_candidate(self.conn, 'owner', candidate['candidate_id'], confirm=confirm)
        self.conn.commit()
        return result

    def test_completed_snapshot_is_immutable_includes_final_and_idempotent(self):
        messages = [{'role':'user','content':'first'}, {'role':'assistant','content':'final'}]
        first = self.enqueue(messages=messages)
        messages[1]['content'] = 'mutated'
        duplicate = self.enqueue(messages=messages)
        self.assertEqual(first['job_id'], duplicate['job_id'])
        self.assertEqual(duplicate['snapshot']['messages'][-1]['content'], 'final')
        seen = []
        self.worker(lambda job,chunk,index: seen.extend(chunk) or []).run_once()
        self.assertEqual(seen[-1]['content'], 'final')

    def test_background_proposes_then_confirmation_is_idempotent(self):
        self.enqueue()
        self.worker().run_once()
        self.assertEqual(memory_store.list_memories(self.conn,'owner'), [])
        candidate = self.candidates()[0]
        result = self.decide(candidate)
        again = self.decide(candidate)
        self.assertEqual(result['memory_id'], again['memory_id'])
        self.assertEqual(len(memory_store.list_memories(self.conn,'owner')), 1)

    def test_clear_during_model_call_cancels_submission(self):
        self.enqueue()
        def extractor(job,chunk,index):
            conn = self.connect()
            try:
                conn.execute("UPDATE session_fences SET epoch=1 WHERE session_id='session'")
                conn.commit()
            finally:
                conn.close()
            return [{'body':'Old fact', 'type':'reference'}]
        self.worker(extractor).run_once()
        self.assertEqual(self.candidates(), [])
        self.assertEqual(jobs.list_jobs(self.conn,'owner')[0]['status'], 'cancelled')

    def test_clear_after_proposal_prevents_confirmation(self):
        self.enqueue()
        self.worker().run_once()
        candidate = self.candidates()[0]
        self.conn.execute("UPDATE session_fences SET epoch=1 WHERE session_id='session'")
        self.conn.commit()
        result = self.decide(candidate)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(memory_store.list_memories(self.conn,'owner'), [])

    def test_missing_or_deleted_fence_never_accepts_job(self):
        self.conn.execute("DELETE FROM session_fences")
        with self.assertRaises(ValueError):
            self.enqueue()
        self.conn.rollback()
        self.conn.execute("UPDATE session_fences SET deleted_at=1")
        with self.assertRaises(ValueError):
            self.enqueue()

    def test_crash_lease_is_recovered_and_retry_does_not_duplicate(self):
        job = self.enqueue()
        self.conn.execute("UPDATE memory_jobs SET status='running',lease_until=1,lease_token='dead'")
        self.conn.commit()
        self.worker().run_once()
        self.assertEqual(len(self.candidates()),1)
        self.assertFalse(self.worker().run_once())
        self.assertEqual(jobs.list_jobs(self.conn,'owner')[0]['attempts'],1)
        self.assertEqual(job['source_key'],'run')

    def test_failure_can_retry_and_dismissal_survives_replay(self):
        job = self.enqueue()
        def fail(*args):
            raise RuntimeError('secret provider response')
        self.worker(fail).run_once()
        failed = jobs.list_jobs(self.conn,'owner')[0]
        self.assertEqual(failed['status'],'failed')
        self.assertNotIn('secret provider',failed['error'])
        jobs.retry_job(self.conn,'owner',job['job_id'])
        self.conn.commit()
        self.worker().run_once()
        self.decide(self.candidates()[0],False)
        self.conn.execute("UPDATE memory_jobs SET status='failed'")
        jobs.retry_job(self.conn,'owner',job['job_id'])
        self.conn.commit()
        self.worker().run_once()
        self.assertEqual([c['status'] for c in self.candidates()], ['dismissed'])

    def test_credentials_are_never_sent_to_model_or_saved_as_candidate(self):
        self.enqueue(messages=[{'role':'user','content':'api_key=TEST_PLACEHOLDER'},
                                {'role':'assistant','content':'final'}])
        seen=[]
        def extractor(job,chunk,index):
            seen.extend(chunk)
            return [{'body':'api_key=TEST_PLACEHOLDER','type':'reference'}]
        self.worker(extractor).run_once()
        self.assertNotIn('TEST_PLACEHOLDER',json.dumps(seen))
        self.assertEqual(self.candidates(),[])

    def test_whole_turns_chunk_without_losing_final_message(self):
        messages=[]
        for i in range(4):
            messages += [{'role':'user','content':str(i)+'x'*9000}, {'role':'assistant','content':'done'+str(i)}]
        self.enqueue(messages=messages)
        seen=[]
        self.worker(lambda job,chunk,index: seen.append(chunk) or []).run_once()
        self.assertGreater(len(seen),1)
        self.assertEqual([m for c in seen for m in c],messages)
        self.assertTrue(all(c[0]['role']=='user' and c[-1]['role']=='assistant' for c in seen))

    def test_oversize_turn_is_explicit_failure_without_partial_candidates(self):
        self.enqueue(messages=[{'role':'user','content':'x'*(jobs.INPUT_LIMIT+1)}])
        self.worker().run_once()
        self.assertEqual(jobs.list_jobs(self.conn,'owner')[0]['status'],'failed')
        self.assertEqual(self.candidates(),[])

    def test_group_source_never_expands_to_assistant_scope(self):
        self.enqueue(group_id='group')
        self.worker(lambda *args:[{'body':'Group rule','type':'feedback','scope_kind':'assistant','scope_id':'owner'}]).run_once()
        memory_id=self.decide(self.candidates()[0])['memory_id']
        memory=memory_store.get(self.conn,'owner',memory_id)
        self.assertEqual((memory['scope_kind'],memory['scope_id']),('group','group'))
        self.assertEqual(memory_store.authorized(self.conn,'owner'),[])

    def test_organize_only_input_and_version_conflict_is_preserved(self):
        first=memory_store.create(self.conn,'owner','First original')
        other=memory_store.create(self.conn,'owner','Other original')
        jobs.organize(self.conn,'owner','assistant','owner',[first['memory_id']])
        self.conn.commit()
        self.worker(lambda *args:[
            {'intent':'update','target_memory_id':first['memory_id'],'body':'Proposed edit'},
            {'intent':'archive','target_memory_id':other['memory_id'],'body':'Other original'}]).run_once()
        candidates=self.candidates()
        self.assertEqual(len(candidates),1)
        memory_store.update(self.conn,'owner',first['memory_id'],body='User edit',expected_version=1)
        self.conn.commit()
        self.assertEqual(self.decide(candidates[0])['status'],'conflict')
        self.assertEqual(memory_store.get(self.conn,'owner',first['memory_id'])['body'],'User edit')
        self.assertEqual(memory_store.get(self.conn,'owner',other['memory_id'])['status'],'active')

    def test_organize_confirm_creates_revision_not_destructive_replace(self):
        first=memory_store.create(self.conn,'owner','Original')
        jobs.organize(self.conn,'owner','assistant','owner',[first['memory_id']])
        self.conn.commit()
        self.worker(lambda *args:[{'intent':'archive','target_memory_id':first['memory_id'],'summary':'Obsolete'}]).run_once()
        result=self.decide(self.candidates()[0])
        self.assertEqual(result['status'],'confirmed')
        after=memory_store.get(self.conn,'owner',first['memory_id'],include_deleted=True)
        self.assertEqual((after['status'],after['version']),('archived',2))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM memory_revisions').fetchone()[0],2)

    def test_import_preview_is_inert_confirm_scoped_idempotent_and_deleted_stays_deleted(self):
        root=Path(self.temp.name)/'user'
        root.mkdir()
        (root/'preference.md').write_text('---\ntype: feedback\n---\nPrefer concise responses',encoding='utf-8')
        preview=jobs.import_preview(self.conn,'owner',str(root),'group','group')
        self.assertEqual(memory_store.list_memories(self.conn,'owner'),[])
        result=jobs.import_confirm(self.conn,'owner',preview['preview_id'])
        self.assertEqual(jobs.import_confirm(self.conn,'owner',preview['preview_id']),result)
        memory=memory_store.get(self.conn,'owner',result['memory_ids'][0])
        self.assertEqual(memory['scope_kind'],'group')
        memory_store.update(self.conn,'owner',memory['memory_id'],status='deleted',expected_version=1)
        next_preview=jobs.import_preview(self.conn,'owner',str(root),'group','group')
        jobs.import_confirm(self.conn,'owner',next_preview['preview_id'])
        self.assertEqual(memory_store.list_memories(self.conn,'owner'),[])

    def test_stale_worker_cannot_commit_after_lease_reassigned(self):
        self.enqueue()
        def extractor(job,chunk,index):
            conn=self.connect()
            try:
                conn.execute("UPDATE memory_jobs SET lease_token='another-worker'")
                conn.commit()
            finally:
                conn.close()
            return [{'body':'Stale proposal','type':'reference'}]
        self.worker(extractor).run_once()
        self.assertEqual(self.candidates(),[])

    def test_old_job_cannot_restore_deleted_memory(self):
        self.enqueue()
        memory=memory_store.create(self.conn,'owner','Deleted preference')
        memory_store.update(self.conn,'owner',memory['memory_id'],status='deleted',expected_version=1)
        self.conn.commit()
        self.worker(lambda *args:[{'body':'Deleted preference','type':'user'}]).run_once()
        self.assertEqual(self.candidates(),[])

    def test_clear_scrubs_snapshot_and_candidate_content(self):
        self.enqueue()
        self.worker().run_once()
        self.conn.execute("UPDATE session_fences SET epoch=1")
        self.conn.commit()
        candidate=self.candidates()[0]
        self.assertEqual((candidate['status'],candidate['body'],candidate['before_body']),('cancelled','',''))
        self.assertEqual(self.conn.execute('SELECT snapshot FROM memory_jobs').fetchone()[0],'{}')

    def test_confirmation_api_rolls_back_partial_write(self):
        from server.api import memory_jobs as api
        self.enqueue()
        self.worker().run_once()
        candidate=self.candidates()[0]
        create=memory_store.create
        def failed_create(*args, **kwargs):
            create(*args, **kwargs)
            raise ValueError('Failed before confirmation')
        with patch.object(api,'get_connection',self.connect), patch.object(memory_store,'create',failed_create):
            with self.assertRaises(api.HTTPException) as error:
                api.confirm_candidate('owner',candidate['candidate_id'])
            self.assertEqual(error.exception.status_code,409)
        self.assertEqual(memory_store.list_memories(self.conn,'owner'),[])
        self.assertEqual(self.candidates()[0]['status'],'pending')

    def test_import_does_not_scan_cli_backup_or_staging(self):
        root=Path(self.temp.name)/'.memory'
        (root/'project').mkdir(parents=True)
        (root/'.backup_123').mkdir()
        (root/'project'/'active.md').write_text('Active memory')
        (root/'.backup_123'/'deleted.md').write_text('Previously deleted memory')
        preview=jobs.import_preview(self.conn,'owner',str(root),'assistant','owner')
        self.assertEqual([m['body'] for m in preview['entries']],['Active memory'])

    def make_session(self):
        manager=SessionManager(Path(self.temp.name), {'session':{'persist_dir':'.sessions'}})
        state=manager.create_session('assistant_dm','assistant:owner')
        self.conn.execute('INSERT INTO session_fences VALUES (?,0,NULL)',(state.meta.session_id,))
        self.conn.commit()
        return manager,state

    def test_outbox_recovery_only_replays_committed_completion_markers(self):
        manager,state=self.make_session()
        state.messages=[{'role':'user','content':'Old chat is not an extraction trigger'}]
        manager.save(state,expected_version=state.version)
        with patch.object(jobs,'get_connection',self.connect):
            self.assertEqual(jobs.recover_outboxes(manager),0)
            jobs.begin_task(state,template_id='owner',run_id='new-run',user_message='Remember this reference')
            jobs.finish_task(state,status='completed',termination='completed',new_api_messages=[],
                             final_reply='Verified final result',raw_reply='Verified final result')
            manager.save(state,expected_version=state.version)
            self.assertEqual(jobs.recover_outboxes(manager),1)
            self.assertEqual(jobs.recover_outboxes(manager),0)
        record=self.conn.execute('SELECT snapshot FROM memory_jobs').fetchone()
        self.assertEqual(json.loads(record['snapshot'])['messages'][-1]['content'],'Verified final result')
        self.assertNotIn('Old chat',record['snapshot'])
        self.assertNotIn('memory_extraction_outbox',manager.load(state.meta.session_id).context)

    def test_outbox_survives_queue_failure_and_deduplicates_failed_acknowledgement(self):
        manager,state=self.make_session()
        jobs.begin_task(state,template_id='owner',run_id='new-run',user_message='Input')
        jobs.finish_task(state,status='completed',termination='completed',new_api_messages=[],final_reply='Final',raw_reply='Final')
        manager.save(state,expected_version=state.version)
        with patch.object(jobs,'enqueue_completed',side_effect=sqlite3.OperationalError('busy')):
            self.assertEqual(jobs.flush_outbox(manager,state),0)
        self.assertTrue(manager.load(state.meta.session_id).context['memory_extraction_outbox'])
        with patch.object(jobs,'get_connection',self.connect):
            with patch.object(manager,'save',side_effect=OSError('crash before ack')):
                self.assertEqual(jobs.recover_outboxes(manager),1)
            self.assertEqual(jobs.recover_outboxes(manager),1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM memory_jobs').fetchone()[0],1)

    def test_only_completed_tasks_create_outbox(self):
        manager,state=self.make_session()
        for status in ('waiting_for_user','failed','cancelled','budget_exhausted','partially_completed','blocked_runtime'):
            jobs.begin_task(state,template_id='owner',run_id=status,user_message='Input')
            jobs.finish_task(state,status=status,termination=status,new_api_messages=[],final_reply='Result',raw_reply='Result')
            self.assertNotIn('memory_extraction_outbox',state.context)

    def test_question_chain_keeps_initial_run_answers_and_intermediate_text_across_compact(self):
        manager,state=self.make_session()
        state.messages=[{'role':'user','content':'Use the image API','run_id':'initial-run',
                         'source_kind':'message','source_id':'original-message'}]
        jobs.begin_task(state,template_id='owner',run_id='initial-run',user_message='Use the image API')
        jobs.finish_task(state,status='waiting_for_user',termination='waiting_for_user',
            new_api_messages=[{'role':'assistant','content':'Checked the integration'}],final_reply='',raw_reply='')
        manager.save(state,expected_version=state.version)
        state=manager.load(state.meta.session_id)
        state.messages=[]
        continuation={'question_id':'q1','answer':{'answers':[{'question_id':'url',
            'other_text':'https://example.com/docs','selected_labels':[]}]}}
        jobs.begin_task(state,template_id='owner',run_id='resume-run',user_message='',continuation=continuation)
        jobs.begin_task(state,template_id='owner',run_id='resume-run',user_message='',continuation=continuation)
        jobs.finish_task(state,status='completed',termination='completed',
            new_api_messages=[{'role':'assistant','content':'Done'}],final_reply='Done',raw_reply='Done')
        receipt=state.context['memory_extraction_outbox'][0]
        self.assertEqual(receipt['source_key'],'completed:initial-run')
        self.assertEqual(receipt['messages'][0]['source_id'],'original-message')
        self.assertEqual(len(receipt['messages']),4)
        self.assertIn('https://example.com/docs',receipt['messages'][2]['content'])
        self.assertEqual((receipt['messages'][2]['source_kind'],receipt['messages'][2]['source_id']),('question','q1'))
        self.assertEqual(receipt['messages'][-1]['content'],'Done')

    def test_multiple_completed_receipts_survive_database_outage(self):
        manager,state=self.make_session()
        for run in ('first','second'):
            jobs.begin_task(state,template_id='owner',run_id=run,user_message=run)
            jobs.finish_task(state,status='completed',termination='completed',new_api_messages=[],
                final_reply='Done',raw_reply='Done')
        manager.save(state,expected_version=state.version)
        with patch.object(jobs,'get_connection',self.connect):
            self.assertEqual(jobs.recover_outboxes(manager),2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM memory_jobs').fetchone()[0],2)


if __name__ == '__main__':
    unittest.main()
