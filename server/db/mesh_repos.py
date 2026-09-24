"""Transactional, task-scoped collaboration between persistent assistants."""
from __future__ import annotations

import contextvars
import hashlib
import json
import time
import uuid
import mimetypes
import re
from pathlib import Path
from contextlib import contextmanager

from .connection import get_connection
from .workflow_repos import _append_event_tx
from avent_paths import data_root

MIGRATION_STATEMENTS = (
    "CREATE TABLE mesh_agent_profiles (agent_id TEXT PRIMARY KEY, accepting_tasks INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1)",
    "CREATE TABLE mesh_agent_links (caller_id TEXT NOT NULL, target_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(caller_id,target_id))",
    "CREATE TABLE mesh_task_bindings (task_id TEXT PRIMARY KEY REFERENCES workflow_tasks(task_id), caller_id TEXT NOT NULL, target_id TEXT NOT NULL, origin_session_id TEXT NOT NULL DEFAULT '', origin_group_id TEXT NOT NULL DEFAULT '', caller_snapshot_json TEXT NOT NULL, target_snapshot_json TEXT NOT NULL, acceptance_criteria_json TEXT NOT NULL, context_json TEXT NOT NULL, result_revision INTEGER NOT NULL DEFAULT 0, wait_reason TEXT NOT NULL DEFAULT '', waiting_on_id TEXT NOT NULL DEFAULT '', partial_json TEXT NOT NULL DEFAULT '[]')",
    "CREATE TABLE mesh_messages (message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES workflow_tasks(task_id), sequence INTEGER NOT NULL, sender_id TEXT NOT NULL, recipient_id TEXT NOT NULL, kind TEXT NOT NULL, body_json TEXT NOT NULL, reply_to TEXT NOT NULL DEFAULT '', artifact_refs_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, UNIQUE(task_id,sequence))",
    "CREATE TABLE mesh_task_sessions (task_id TEXT NOT NULL, agent_id TEXT NOT NULL, session_id TEXT NOT NULL, last_consumed_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id,agent_id))",
    "CREATE TABLE mesh_inbox (work_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, task_id TEXT NOT NULL, agent_id TEXT NOT NULL, sequence INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', owner_id TEXT NOT NULL DEFAULT '', lease_token TEXT NOT NULL DEFAULT '', fencing_token INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL DEFAULT 0, attempt_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, UNIQUE(event_id,agent_id))",
    "CREATE INDEX mesh_inbox_pending ON mesh_inbox(status,created_at)",
    "CREATE TABLE mesh_submissions (task_id TEXT NOT NULL, revision INTEGER NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id,revision))",
    "CREATE TABLE mesh_reviews (review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, submission_revision INTEGER NOT NULL, reviewer_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE TABLE mesh_pending_decisions (decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, reason TEXT NOT NULL, allowed_actions_json TEXT NOT NULL, expected_task_revision INTEGER NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', answer_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)",
    "CREATE TABLE mesh_idempotency (scope TEXT NOT NULL, key TEXT NOT NULL, payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(scope,key))",
    "CREATE TABLE mesh_limits (workflow_id TEXT PRIMARY KEY, depth INTEGER NOT NULL DEFAULT 5, tasks INTEGER NOT NULL DEFAULT 32, messages INTEGER NOT NULL DEFAULT 100, revisions INTEGER NOT NULL DEFAULT 3)",
)

TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'timeout'})
_fence = contextvars.ContextVar('mesh_fence', default=None)
_transaction = contextvars.ContextVar('mesh_transaction', default=None)


class MeshError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _id(prefix):
    return prefix + '_' + uuid.uuid4().hex


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _decode(row):
    if row is None:
        return None
    data = dict(row)
    for key in list(data):
        if key.endswith('_json'):
            data[key[:-5]] = json.loads(data.pop(key))
    return data


@contextmanager
def work_context(work_id, lease_token, fencing_token):
    token = _fence.set((work_id, lease_token, fencing_token))
    try:
        yield
    finally:
        _fence.reset(token)


def _check_fence(conn):
    fence = _fence.get()
    if fence and not conn.execute("SELECT 1 FROM mesh_inbox WHERE work_id=? AND lease_token=? AND fencing_token=? AND status='running' AND expires_at>?", (*fence, time.time())).fetchone():
        raise MeshError('stale_revision', 'Execution lease expired', 409)


@contextmanager
def _tx():
    active=_transaction.get()
    if active is not None:
        _check_fence(active)
        yield active
        return
    conn = get_connection()
    token=_transaction.set(conn)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _check_fence(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _transaction.reset(token)
        conn.close()


def _cached(conn, scope, key, payload):
    if not key:
        return None
    row = conn.execute('SELECT * FROM mesh_idempotency WHERE scope=? AND key=?', (scope,key)).fetchone()
    if row:
        if row['payload_hash'] != hashlib.sha256(_json(payload).encode()).hexdigest():
            raise MeshError('stale_revision', 'Idempotency key reused with different payload',409)
        return json.loads(row['result_json'])


def _cache(conn, scope, key, payload, result):
    if key:
        conn.execute('INSERT INTO mesh_idempotency VALUES (?,?,?,?)',(scope,key,hashlib.sha256(_json(payload).encode()).hexdigest(),_json(result)))
    return result


def _profile(conn, agent_id):
    row = conn.execute('SELECT template_id,name,role,capabilities FROM agent_templates WHERE template_id=?',(agent_id,)).fetchone()
    if not row:
        raise MeshError('not_found','Assistant not found',404)
    config = conn.execute('SELECT * FROM mesh_agent_profiles WHERE agent_id=?',(agent_id,)).fetchone()
    enabled=json.loads(row['capabilities'] or '{}')
    capabilities=[Path(str(value)).name for key in ('tools','skills','plugins') for value in enabled.get(key,[]) if isinstance(value,str)]
    return {'agent_id':agent_id,'name':row['name'],'role':row['role'],'description':row['role'], 'capabilities':capabilities, 'input_requirements':['objective','acceptance_criteria'],'output_types':['result','artifact'], 'accepting_tasks':bool(config['accepting_tasks']) if config else True,'contract_revision':config['revision'] if config else 1}


def configure(agent_id, outgoing, accepting_tasks=True, expected_revision=None):
    with _tx() as conn:
        current = _profile(conn,agent_id)
        if expected_revision is not None and expected_revision != current['contract_revision']:
            raise MeshError('stale_revision','Configuration changed',409)
        if not isinstance(outgoing,list) or any(not isinstance(x,str) for x in outgoing) or agent_id in outgoing:
            raise MeshError('invalid_input','Outgoing must contain other assistant IDs')
        for target in outgoing:
            _profile(conn,target)
        conn.execute('INSERT INTO mesh_agent_profiles VALUES (?,?,?) ON CONFLICT(agent_id) DO UPDATE SET accepting_tasks=excluded.accepting_tasks,revision=excluded.revision',(agent_id,int(accepting_tasks),current['contract_revision']+1))
        conn.execute('DELETE FROM mesh_agent_links WHERE caller_id=?',(agent_id,))
        conn.executemany('INSERT INTO mesh_agent_links VALUES (?,?,?)',[(agent_id,target,time.time()) for target in set(outgoing)])
    return collaboration(agent_id)


def collaboration(agent_id):
    with _tx() as conn:
        result = _profile(conn,agent_id)
        result['revision'] = result['contract_revision']
        result['outgoing'] = [r[0] for r in conn.execute('SELECT target_id FROM mesh_agent_links WHERE caller_id=? ORDER BY target_id',(agent_id,))]
        result['incoming'] = [r[0] for r in conn.execute('SELECT caller_id FROM mesh_agent_links WHERE target_id=? ORDER BY caller_id',(agent_id,))]
        return result


def profile(agent_id,actor_id=None):
    with _tx() as conn:
        if actor_id and actor_id != agent_id and not conn.execute('SELECT 1 FROM mesh_agent_links WHERE caller_id=? AND target_id=?',(actor_id,agent_id)).fetchone():
            raise MeshError('permission_denied','Assistant not in outgoing allowlist',403)
        return _profile(conn,agent_id)


def discover(caller_id,query='',limit=5):
    with _tx() as conn:
        _profile(conn,caller_id)
        query = str(query or '').strip().casefold()
        terms = [term for term in query.split() if term]
        found=[]
        for row in conn.execute('SELECT target_id FROM mesh_agent_links WHERE caller_id=? ORDER BY target_id',(caller_id,)):
            try:
                item=_profile(conn,row[0])
            except MeshError:
                continue
            haystack = (item['name']+' '+item['role']+' '+' '.join(item['capabilities'])).casefold()
            matches = not query or query in haystack or any(term in haystack for term in terms)
            if item['accepting_tasks'] and matches:
                item['match_reason']='Allowed assistant matching name or role' if query else 'Allowed assistant accepting tasks'
                found.append(item)
        return found[:max(1,min(20,int(limit))) ]


def _task_title(text):
    first = re.split(r'[\n。！？]|\\n', text.strip(), maxsplit=1)[0].strip()
    return first[:40] + ('…' if len(first) > 40 else '')


def _task(conn,task_id,actor_id=None,open_only=False):
    row=conn.execute('SELECT t.*,b.* FROM workflow_tasks t JOIN mesh_task_bindings b USING(task_id) WHERE t.task_id=?',(task_id,)).fetchone()
    if row is None:
        raise MeshError('not_found','Task not found',404)
    data=_decode(row)
    if actor_id and actor_id not in (data['caller_id'],data['target_id']):
        raise MeshError('permission_denied','Not a task participant',403)
    if open_only and data['status'] in TERMINAL:
        raise MeshError('task_closed','Task has ended',409)
    data['objective']=data.get('intent',{}).get('objective') or data['title']
    data['title']=_task_title(data['title'])
    data['workspace_path']=task_workspace(task_id)
    return data


def _state(conn,task,status,reason='',waiting_on=''):
    conn.execute('UPDATE workflow_tasks SET status=?,revision=revision+1,updated_at=? WHERE task_id=?',(status,time.time(),task['task_id']))
    conn.execute('UPDATE mesh_task_bindings SET wait_reason=?,waiting_on_id=? WHERE task_id=?',(reason,waiting_on,task['task_id']))
    conn.execute('UPDATE workflow_runs SET updated_at=?,revision=revision+1 WHERE workflow_id=?',(time.time(),task['workflow_id']))


def _message(conn,task,sender,kind,body,reply_to='',artifact_refs=None,wake=True):
    recipient=task['target_id'] if sender==task['caller_id'] else task['caller_id']
    sequence=conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM mesh_messages WHERE task_id=?',(task['task_id'],)).fetchone()[0]
    message={'message_id':_id('mm'),'task_id':task['task_id'],'sequence':sequence,'sender_id':sender,'recipient_id':recipient,'kind':kind,'body':body,'reply_to':reply_to,'artifact_refs':artifact_refs or [],'created_at':time.time()}
    conn.execute('INSERT INTO mesh_messages VALUES (?,?,?,?,?,?,?,?,?,?)',(message['message_id'],task['task_id'],sequence,sender,recipient,kind,_json(body),reply_to,_json(artifact_refs or []),message['created_at']))
    event=_append_event_tx(conn,workflow_id=task['workflow_id'],task_id=task['task_id'],event_type='mesh.'+kind,payload=message)
    conn.execute('INSERT INTO workflow_outbox (outbox_id,event_id,workflow_id,kind,payload_json,available_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',(_id('mo'),event['event_id'],task['workflow_id'],'mesh.notification',_json(message),time.time(),time.time(),time.time()))
    if wake:
        _enqueue(conn,task,recipient,event['event_id'],sequence)
    return message


def _enqueue(conn,task,agent,event_id,sequence):
    conn.execute('INSERT OR IGNORE INTO mesh_task_sessions(task_id,agent_id,session_id) VALUES (?,?,?)',(task['task_id'],agent,f"mesh:{task['task_id']}:{agent}"))
    conn.execute('INSERT OR IGNORE INTO mesh_inbox(work_id,event_id,task_id,agent_id,sequence,created_at) VALUES (?,?,?,?,?,?)',(_id('mi'),event_id,task['task_id'],agent,sequence,time.time()))


def _artifacts(conn,task,actor,refs):
    for ref in refs or []:
        if not isinstance(ref,str):
            raise MeshError('invalid_input','Artifact references must be IDs')
        row=conn.execute('SELECT * FROM workflow_artifacts WHERE artifact_id=?',(ref,)).fetchone()
        if not row:
            raise MeshError('not_found','Artifact not found',404)
        metadata=json.loads(row['metadata_json'])
        source=_task(conn,row['task_id'])
        same_grant=task['task_id'] in metadata.get('forwarded_to_tasks',[]) and actor in metadata.get('visible_to',[])
        if actor not in (source['caller_id'],source['target_id']) and not same_grant:
            raise MeshError('permission_denied','No readable artifact grant',403)
        if source['task_id']!=task['task_id'] and not same_grant and not metadata.get('allow_forward') and metadata.get('created_by')!=actor:
            raise MeshError('permission_denied','Artifact cannot be forwarded',403)
        if source['task_id']!=task['task_id'] and not same_grant:
            metadata['visible_to']=sorted(set(metadata.get('visible_to',[])+[task['caller_id'],task['target_id']]))
            metadata['forwarded_to_tasks']=sorted(set(metadata.get('forwarded_to_tasks',[])+[task['task_id']]))
            conn.execute('UPDATE workflow_artifacts SET metadata_json=? WHERE artifact_id=?',(_json(metadata),ref))


def _decision(conn,task,reason,payload=None,actions=None):
    existing=conn.execute("SELECT * FROM mesh_pending_decisions WHERE task_id=? AND reason=? AND status='pending'",(task['task_id'],reason)).fetchone()
    if existing:
        return _decode(existing)
    decision_id=_id('md')
    payload={**(payload or {}),'previous_status':task['status']}
    conn.execute("UPDATE mesh_pending_decisions SET status='superseded' WHERE task_id=? AND status='pending'",(task['task_id'],))
    _state(conn,task,'waiting',reason,decision_id)
    revision=conn.execute('SELECT revision FROM workflow_tasks WHERE task_id=?',(task['task_id'],)).fetchone()[0]
    conn.execute('INSERT INTO mesh_pending_decisions(decision_id,task_id,reason,allowed_actions_json,expected_task_revision,payload_json,created_at) VALUES (?,?,?,?,?,?,?)',(decision_id,task['task_id'],reason,_json(actions or ['answer','cancel']),revision,_json(payload or {}),time.time()))
    return _decode(conn.execute('SELECT * FROM mesh_pending_decisions WHERE decision_id=?',(decision_id,)).fetchone())


def _limit(conn,task,name,value,payload):
    limits=conn.execute('SELECT * FROM mesh_limits WHERE workflow_id=?',(task['workflow_id'],)).fetchone()
    if value>limits[name]:
        return _decision(conn,task,'limit_reached',{'limit':name,'current':limits[name],'required':value,'command':payload},['raise_limit','cancel'])
    return None


def delegate(caller_id,target_id,objective,acceptance_criteria,context=None,parent_task_id='',origin_session_id='',origin_group_id='',root_request_id='',depends_on=None,idempotency_key='',artifact_refs=None,title=''):
    payload=locals().copy()
    with _tx() as conn:
        cached=_cached(conn,'delegate:'+caller_id,idempotency_key,payload)
        if cached is not None:
            return cached
        caller=_profile(conn,caller_id); target=_profile(conn,target_id)
        if caller_id==target_id or not conn.execute('SELECT 1 FROM mesh_agent_links WHERE caller_id=? AND target_id=?',(caller_id,target_id)).fetchone():
            raise MeshError('permission_denied','No permission to create task for target',403)
        if not target['accepting_tasks']:
            raise MeshError('permission_denied','Target is not accepting tasks',403)
        if not isinstance(objective,str) or not objective.strip() or not acceptance_criteria:
            raise MeshError('invalid_input','Objective and acceptance criteria are required')
        parent=None
        if parent_task_id:
            parent=_task(conn,parent_task_id,caller_id,True)
            if parent['target_id']!=caller_id:
                raise MeshError('permission_denied','Only receiver may delegate a child',403)
            workflow_id=parent['workflow_id']; origin_session_id=parent['origin_session_id']; origin_group_id=parent['origin_group_id']
            ancestors=[]; current=parent
            while current and current['executor_kind']=='mesh_agent':
                ancestors.extend([current['caller_id'],current['target_id']])
                row=conn.execute('SELECT 1 FROM mesh_task_bindings WHERE task_id=?',(current['parent_task_id'],)).fetchone()
                current=_task(conn,current['parent_task_id']) if row else None
            if target_id in ancestors:
                raise MeshError('dependency_cycle','Use existing task messages to contact an ancestor',409)
            depth=len(ancestors)//2+1
        else:
            key=root_request_id or idempotency_key or _id('request')
            root=conn.execute("SELECT * FROM workflow_runs WHERE legacy_source='agent_mesh' AND legacy_id=?",(caller_id+':'+key,)).fetchone()
            if root:
                if root['status'] in TERMINAL:
                    raise MeshError('task_closed','Source collaboration has ended',409)
                workflow_id=root['workflow_id']
                parent_task_id=conn.execute("SELECT task_id FROM workflow_tasks WHERE workflow_id=? AND executor_kind='mesh_coordination'",(workflow_id,)).fetchone()[0]
            else:
                workflow_id=_id('mw'); parent_task_id=_id('mc'); now=time.time()
                origin_fence=conn.execute('SELECT epoch,deleted_at FROM session_fences WHERE session_id=?',(origin_session_id,)).fetchone() if origin_session_id else None
                conn.execute('INSERT INTO workflow_runs(workflow_id,kind,status,owner_session_id,input_snapshot_json,legacy_source,legacy_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',(workflow_id,'agent_mesh','running',origin_session_id,_json({'source_agent_id':caller_id,'origin_session_id':origin_session_id,'origin_group_id':origin_group_id,'origin_epoch':origin_fence['epoch'] if origin_fence and origin_fence['deleted_at'] is None else None}),'agent_mesh',caller_id+':'+key,now,now))
                conn.execute('INSERT INTO workflow_tasks(task_id,workflow_id,title,status,executor_kind,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',(parent_task_id,workflow_id,objective,'waiting','mesh_coordination',now,now))
                conn.execute('INSERT INTO mesh_task_bindings(task_id,caller_id,target_id,origin_session_id,origin_group_id,caller_snapshot_json,target_snapshot_json,acceptance_criteria_json,context_json) VALUES (?,?,?,?,?,?,?,?,?)',(parent_task_id,caller_id,caller_id,origin_session_id,origin_group_id,_json(caller),_json(caller),_json('Summarize accepted outcomes and disclose incomplete work'),'{}'))
                # Older user databases retain the historical SQLite defaults.
                # Set runtime defaults explicitly for every newly created workflow.
                from server.runtime.mesh_budget import DEFAULT_LIMITS
                conn.execute(
                    'INSERT INTO mesh_limits(workflow_id,tokens,tools,rounds) VALUES (?,?,?,?)',
                    (workflow_id, DEFAULT_LIMITS['tokens'], DEFAULT_LIMITS['tools'], DEFAULT_LIMITS['rounds']),
                )
            depth=1
        task={'task_id':_id('mt'),'workflow_id':workflow_id,'caller_id':caller_id,'target_id':target_id}
        count=conn.execute("SELECT count(*) FROM workflow_tasks WHERE workflow_id=? AND executor_kind='mesh_agent'",(workflow_id,)).fetchone()[0]+1
        if parent:
            for name,value in [('depth',depth),('tasks',count)]:
                decision=_limit(conn,parent,name,value,{'operation':'delegate','arguments':payload})
                if decision:
                    return {'status':'waiting','decision':decision,'task_id':parent['task_id'],'limit_reached':True}
        else:
            limits=conn.execute('SELECT * FROM mesh_limits WHERE workflow_id=?',(workflow_id,)).fetchone()
            if count>limits['tasks']:
                existing=_task(conn,conn.execute('SELECT task_id FROM mesh_task_bindings WHERE task_id IN (SELECT task_id FROM workflow_tasks WHERE workflow_id=?) LIMIT 1',(workflow_id,)).fetchone()[0])
                decision=_limit(conn,existing,'tasks',count,{'operation':'delegate','arguments':payload})
                return {'status':'waiting','decision':decision,'task_id':existing['task_id'],'limit_reached':True}
        deps=depends_on or []
        if not isinstance(deps,list):
            raise MeshError('invalid_input','depends_on must be a list')
        for dep_id in deps:
            dep=_task(conn,dep_id,caller_id)
            if dep['workflow_id']!=workflow_id:
                raise MeshError('invalid_input','Dependency belongs to a different collaboration')
        _artifacts(conn,task,caller_id,artifact_refs)
        now=time.time()
        conn.execute('INSERT INTO workflow_tasks(task_id,workflow_id,title,intent_json,status,executor_kind,acceptance_policy,parent_task_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(task['task_id'],workflow_id,_task_title(title or objective),_json({'objective':objective,'context':context or {},'artifact_refs':artifact_refs or []}),'queued','mesh_agent','caller_review',parent_task_id,now,now))
        conn.execute('INSERT INTO mesh_task_bindings(task_id,caller_id,target_id,origin_session_id,origin_group_id,caller_snapshot_json,target_snapshot_json,acceptance_criteria_json,context_json) VALUES (?,?,?,?,?,?,?,?,?)',(task['task_id'],caller_id,target_id,origin_session_id,origin_group_id,_json(caller),_json(target),_json(acceptance_criteria),_json(context or {})))
        conn.executemany('INSERT INTO workflow_task_deps(task_id,depends_on_task_id) VALUES (?,?)',[(task['task_id'],dep) for dep in set(deps)])
        _check_dag(conn,workflow_id)
        _message(conn,task,caller_id,'task_created',{'objective':objective,'acceptance_criteria':acceptance_criteria},artifact_refs=artifact_refs)
        return _cache(conn,'delegate:'+caller_id,idempotency_key,payload,_task(conn,task['task_id']))


def _check_dag(conn,workflow_id):
    nodes=conn.execute('SELECT task_id,parent_task_id FROM workflow_tasks WHERE workflow_id=?',(workflow_id,)).fetchall()
    edges={r['task_id']:[] for r in nodes}
    for row in nodes:
        if row['parent_task_id'] in edges:
            edges[row['parent_task_id']].append(row['task_id'])
    for row in conn.execute('SELECT d.* FROM workflow_task_deps d JOIN workflow_tasks t ON t.task_id=d.task_id WHERE t.workflow_id=?',(workflow_id,)):
        edges[row['task_id']].append(row['depends_on_task_id'])
    active=set(); done=set()
    def visit(node):
        if node in active:
            raise MeshError('dependency_cycle','Delegation and dependencies form a waiting cycle',409)
        if node in done:
            return
        active.add(node)
        for other in edges.get(node,[]): visit(other)
        active.remove(node); done.add(node)
    for node in edges: visit(node)


def get_task(task_id,actor_id=None):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id)
        task['submissions']=[_decode(r) for r in conn.execute('SELECT * FROM mesh_submissions WHERE task_id=? ORDER BY revision',(task_id,))]
        task['reviews']=[dict(r) for r in conn.execute('SELECT * FROM mesh_reviews WHERE task_id=? ORDER BY created_at',(task_id,))]
        task['decisions']=[_decode(r) for r in conn.execute('SELECT * FROM mesh_pending_decisions WHERE task_id=? ORDER BY created_at',(task_id,))]
        if task['parent_task_id']:
            parent=conn.execute('SELECT t.executor_kind,t.status,b.caller_id,b.wait_reason FROM workflow_tasks t JOIN mesh_task_bindings b USING(task_id) WHERE t.task_id=?',(task['parent_task_id'],)).fetchone()
            if parent and parent['executor_kind']=='mesh_coordination' and (actor_id is None or actor_id==parent['caller_id']):
                task['coordination_status']=parent['status']
                task['coordination_wait_reason']=parent['wait_reason']
                task['decisions'].extend(_decode(r) for r in conn.execute("SELECT * FROM mesh_pending_decisions WHERE task_id=? AND status='pending' ORDER BY created_at",(task['parent_task_id'],)))
        task['depends_on']=[r[0] for r in conn.execute('SELECT depends_on_task_id FROM workflow_task_deps WHERE task_id=?',(task_id,))]
        task['children']=[{'task_id':r['task_id'],'status':r['status'],'title':r['title']} for r in conn.execute('SELECT * FROM workflow_tasks WHERE parent_task_id=?',(task_id,))]
        task['artifacts']=[_public_artifact(r,actor_id) for r in conn.execute('SELECT * FROM workflow_artifacts WHERE task_id=?',(task_id,))]
        task['executions_stopping']=conn.execute("SELECT count(*) FROM mesh_inbox WHERE task_id=? AND status='stopping'",(task_id,)).fetchone()[0]
        usage=conn.execute('SELECT SUM(u.input_tokens) input_tokens,SUM(u.cached_input_tokens) cached_input_tokens,SUM(u.uncached_input_tokens) uncached_input_tokens,SUM(u.output_tokens) output_tokens,SUM(u.total_tokens) total_tokens,COUNT(*) call_count,SUM(MAX(0,u.completed_at-u.started_at)) duration_seconds FROM llm_usage_events u WHERE u.session_id IN (SELECT session_id FROM mesh_task_sessions WHERE task_id=?)',(task_id,)).fetchone()
        model_usage=[dict(row) for row in conn.execute('SELECT u.model,SUM(u.uncached_input_tokens) uncached_input_tokens,SUM(u.cached_input_tokens) cached_input_tokens,SUM(u.output_tokens) output_tokens FROM llm_usage_events u WHERE u.session_id IN (SELECT session_id FROM mesh_task_sessions WHERE task_id=?) GROUP BY u.model',(task_id,))]
        task['usage']=dict(usage)
        task['stalled']=task['status'] not in TERMINAL and time.time()-task['updated_at']>=86400
    # Pricing may refresh a currency rate; never hold the write transaction for it.
    from server.runtime import pricing
    priced=pricing.apply_model_costs(model_usage) if model_usage else []
    unknown=sum(row.get('spend_cny') is None for row in priced)
    known=sum(row['spend_cny'] for row in priced if row.get('spend_cny') is not None)
    total=known if priced and not unknown else None
    task['usage'].update(cost=total,spend_cny=total,currency='CNY',known_spend_cny=known if any(row.get('spend_cny') is not None for row in priced) else None,unpriced_model_count=unknown,models=priced)
    return task


def list_tasks(actor_id=None,role='all',status='',group_id='',cursor='',limit=50):
    if role not in ('all','sent','received'):
        raise MeshError('invalid_input','Unknown role')
    with _tx() as conn:
        where=["t.executor_kind='mesh_agent'"]; args=[]
        if actor_id:
            if role=='sent': where.append('b.caller_id=?'); args.append(actor_id)
            elif role=='received': where.append('b.target_id=?'); args.append(actor_id)
            else: where.append('(b.caller_id=? OR b.target_id=?)'); args.extend([actor_id,actor_id])
        if status: where.append('t.status=?'); args.append(status)
        if group_id: where.append('b.origin_group_id=?'); args.append(group_id)
        if cursor:
            try:
                stamp,task_id=json.loads(cursor)
            except (ValueError,TypeError): raise MeshError('invalid_input','Invalid cursor')
            where.append('(t.created_at<? OR (t.created_at=? AND t.task_id<?))'); args.extend([stamp,stamp,task_id])
        limit=max(1,min(100,int(limit)))
        rows=conn.execute('SELECT t.task_id,t.created_at FROM workflow_tasks t JOIN mesh_task_bindings b USING(task_id)'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY t.created_at DESC,t.task_id DESC LIMIT ?',(*args,limit+1)).fetchall()
        next_cursor=_json([rows[limit-1]['created_at'],rows[limit-1]['task_id']]) if len(rows)>limit else ''
        items=[_task(conn,r['task_id']) for r in rows[:limit]]
        for task in items:
            parent=conn.execute('SELECT t.executor_kind,t.status,b.caller_id,b.wait_reason FROM workflow_tasks t JOIN mesh_task_bindings b USING(task_id) WHERE t.task_id=?',(task['parent_task_id'],)).fetchone()
            if parent and parent['executor_kind']=='mesh_coordination' and (actor_id is None or actor_id==parent['caller_id']):
                task['coordination_status']=parent['status']
                task['coordination_wait_reason']=parent['wait_reason']
        return {'items':items,'next_cursor':next_cursor}


def messages(task_id,actor_id=None,after_sequence=0):
    with _tx() as conn:
        _task(conn,task_id,actor_id)
        return [_decode(r) for r in conn.execute('SELECT * FROM mesh_messages WHERE task_id=? AND sequence>? ORDER BY sequence',(task_id,after_sequence))]


def graph(workflow_id,actor_id=None):
    with _tx() as conn:
        run=conn.execute("SELECT * FROM workflow_runs WHERE workflow_id=? AND kind='agent_mesh'",(workflow_id,)).fetchone()
        if not run: raise MeshError('not_found','Collaboration not found',404)
        tasks=[]
        for row in conn.execute('SELECT task_id FROM workflow_tasks WHERE workflow_id=? AND executor_kind=?',(workflow_id,'mesh_agent')):
            task=_task(conn,row[0])
            if actor_id is None or actor_id in (task['caller_id'],task['target_id']): tasks.append(task)
        if actor_id and not tasks: raise MeshError('permission_denied','No visible tasks',403)

        coordination_row=conn.execute('SELECT task_id FROM workflow_tasks WHERE workflow_id=? AND executor_kind=?',(workflow_id,'mesh_coordination')).fetchone()
        coordination=_task(conn,coordination_row[0]) if coordination_row else None
        ids={task['task_id'] for task in tasks}
        nodes=[]
        origins={}
        for task in tasks:
            if not task['parent_task_id'] or task['parent_task_id'] not in ids:
                caller_id=task['caller_id']
                if caller_id and caller_id not in origins:
                    snapshot=task.get('caller_snapshot') or {}
                    origins[caller_id]={
                        'node_id':'agent:origin:'+caller_id,
                        'kind':'agent',
                        'assistant_id':caller_id,
                        'title':snapshot.get('name') or caller_id,
                        'assistant_name':'项目发起',
                        'status':'active',
                    }
        nodes.extend(origins.values())
        for task in tasks:
            snapshot=task.get('target_snapshot') or {}
            nodes.append({
                'node_id':'agent:task:'+task['task_id'],
                'kind':'agent',
                'task_id':task['task_id'],
                'assistant_id':task['target_id'],
                'title':snapshot.get('name') or task['target_id'],
                'task_title':task['title'],
                'assistant_name':'执行任务',
                'status':task['status'],
            })
        summary_id=None
        if coordination:
            summary_id='summary:'+coordination['task_id']
            nodes.append({
                'node_id':summary_id,
                'kind':'summary',
                'task_id':coordination['task_id'],
                'title':'最终汇总',
                'status':coordination['status'],
                'assistant_id':coordination['caller_id'],
                'assistant_name':(coordination.get('caller_snapshot') or {}).get('name') or coordination['caller_id'],
            })

        edges=[]
        seen=set()
        def add_edge(source,target,kind):
            key=(source,target,kind)
            if key not in seen:
                seen.add(key); edges.append({'source':source,'target':target,'kind':kind})

        parents={task['parent_task_id'] for task in tasks if task['parent_task_id'] in ids}
        for task in tasks:
            for dep in conn.execute('SELECT depends_on_task_id FROM workflow_task_deps WHERE task_id=?',(task['task_id'],)):
                if dep[0] in ids: parents.add(dep[0])
        for task in tasks:
            target='agent:task:'+task['task_id']
            if task['parent_task_id'] in ids:
                add_edge('agent:task:'+task['parent_task_id'],target,'delegation')
            else:
                add_edge('agent:origin:'+task['caller_id'],target,'dispatch')
            for dep in conn.execute('SELECT depends_on_task_id FROM workflow_task_deps WHERE task_id=?',(task['task_id'],)):
                if dep[0] in ids:
                    add_edge('agent:task:'+dep[0],target,'dependency')
            if summary_id and task['task_id'] not in parents: add_edge(target,summary_id,'aggregation')
        return {'workflow':_decode(run),'workflow_id':workflow_id,'nodes':nodes,'edges':edges}


def send_message(task_id,actor_id,kind,body,reply_to='',artifact_refs=None,idempotency_key=''):
    payload=locals().copy()
    with _tx() as conn:
        cached=_cached(conn,'message:'+actor_id+':'+task_id,idempotency_key,payload)
        if cached is not None: return cached
        task=_task(conn,task_id,actor_id,True)
        if kind not in ('message','clarification_request','information_request','decision_request','progress','artifact') or not body:
            raise MeshError('invalid_input','Invalid business message')
        if reply_to and not conn.execute('SELECT 1 FROM mesh_messages WHERE task_id=? AND message_id=?',(task_id,reply_to)).fetchone():
            raise MeshError('invalid_input','Reply must reference this task')
        _artifacts(conn,task,actor_id,artifact_refs)
        count=conn.execute('SELECT count(*) FROM mesh_messages m JOIN workflow_tasks t USING(task_id) WHERE t.workflow_id=?',(task['workflow_id'],)).fetchone()[0]+1
        decision=_limit(conn,task,'messages',count,{'operation':'send_message','arguments':payload})
        if decision: return {'status':'waiting','decision':decision,'limit_reached':True}
        msg=_message(conn,task,actor_id,kind,body,reply_to,artifact_refs,wake=kind not in ('progress','artifact'))
        if reply_to and task['waiting_on_id']==reply_to:
            _state(conn,task,'queued')
        return _cache(conn,'message:'+actor_id+':'+task_id,idempotency_key,payload,msg)


def submit(task_id,actor_id,result,idempotency_key=''):
    payload=locals().copy()
    with _tx() as conn:
        cached=_cached(conn,'submit:'+actor_id+':'+task_id,idempotency_key,payload)
        if cached is not None: return cached
        task=_task(conn,task_id,actor_id,True)
        if actor_id!=task['target_id']: raise MeshError('permission_denied','Only receiver submits',403)
        if task['executor_kind']=='mesh_coordination': raise MeshError('invalid_input','Return the coordination summary as a final reply, not a peer submission')
        if task['status']=='submitted': raise MeshError('stale_revision','Submission awaiting review',409)
        if not isinstance(result,dict) or not result.get('summary'): raise MeshError('invalid_input','Result summary required')
        children=conn.execute('SELECT task_id,status FROM workflow_tasks WHERE parent_task_id=?',(task_id,)).fetchall()
        if any(r['status']!='completed' and not (r['task_id'] in task['partial'] and r['status'] in TERMINAL) for r in children):
            raise MeshError('invalid_input','Children must be accepted or explicitly waived')
        refs=result.get('artifact_refs',[]); _artifacts(conn,task,actor_id,refs)
        revision=task['result_revision']+1
        conn.execute('INSERT INTO mesh_submissions VALUES (?,?,?,?)',(task_id,revision,_json(result),time.time()))
        conn.execute('UPDATE mesh_task_bindings SET result_revision=? WHERE task_id=?',(revision,task_id))
        _state(conn,task,'submitted')
        _message(conn,task,actor_id,'submitted',{'submission_revision':revision,'result':result},artifact_refs=refs)
        response=_task(conn,task_id); response['submission_revision']=revision
        return _cache(conn,'submit:'+actor_id+':'+task_id,idempotency_key,payload,response)


def review(task_id,actor_id,submission_revision,decision,reason='',idempotency_key=''):
    payload=locals().copy()
    with _tx() as conn:
        cached=_cached(conn,'review:'+actor_id+':'+task_id,idempotency_key,payload)
        if cached is not None: return cached
        task=_task(conn,task_id,actor_id,True)
        if actor_id!=task['caller_id']: raise MeshError('permission_denied','Only initiator reviews',403)
        if task['status']!='submitted' or submission_revision!=task['result_revision']: raise MeshError('stale_revision','Submission no longer current',409)
        if decision not in ('accept','request_changes') or (decision=='request_changes' and not reason.strip()): raise MeshError('invalid_input','Review requires accept or specific changes')
        if decision=='request_changes':
            count=conn.execute("SELECT count(*) FROM mesh_reviews WHERE task_id=? AND decision='request_changes'",(task_id,)).fetchone()[0]+1
            pending=_limit(conn,task,'revisions',count,{'operation':'review','arguments':payload})
            if pending: return {'status':'waiting','decision':pending,'limit_reached':True}
        conn.execute('INSERT INTO mesh_reviews VALUES (?,?,?,?,?,?,?)',(_id('mr'),task_id,submission_revision,actor_id,decision,reason,time.time()))
        _state(conn,task,'completed' if decision=='accept' else 'revision_requested')
        _message(conn,task,actor_id,'accepted' if decision=='accept' else 'changes_requested',{'submission_revision':submission_revision,'reason':reason},wake=decision!='accept')
        if decision=='accept':
            conn.execute("UPDATE mesh_inbox SET status='cancelled' WHERE task_id=? AND status='pending'",(task_id,))
            _wake_parent(conn,task)
        return _cache(conn,'review:'+actor_id+':'+task_id,idempotency_key,payload,_task(conn,task_id))


def _wake_parent(conn,task):
    if not conn.execute('SELECT 1 FROM mesh_task_bindings WHERE task_id=?',(task['parent_task_id'],)).fetchone(): return
    parent=_task(conn,task['parent_task_id'])
    if parent['executor_kind']=='mesh_coordination': return
    if parent['status'] in TERMINAL: return
    children=conn.execute('SELECT task_id,status FROM workflow_tasks WHERE parent_task_id=?',(parent['task_id'],)).fetchall()
    failed=[r['task_id'] for r in children if r['status'] in TERMINAL and r['status']!='completed' and r['task_id'] not in parent['partial']]
    if failed:
        _state(conn,parent,'waiting','child_failed',task['task_id'])
        _message(conn,parent,parent['target_id'],'decision_request',{'failed_child_ids':failed,'reason':'Child tasks did not complete'})
    elif all(r['status']=='completed' or r['task_id'] in parent['partial'] for r in children) and parent['wait_reason']=='children':
        _state(conn,parent,'queued')
        _message(conn,parent,parent['caller_id'],'children_completed',{'child_task_ids':[r['task_id'] for r in children]})


def yield_task(task_id,actor_id,reason,request_id='',child_task_ids=None):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id,True)
        if task['status']=='submitted': raise MeshError('stale_revision','Cannot yield a submitted task',409)
        if reason in ('children','child_tasks'):
            if actor_id!=task['target_id'] or not child_task_ids: raise MeshError('invalid_input','Receiver must identify children')
            for child_id in child_task_ids:
                child=_task(conn,child_id,actor_id)
                if child['parent_task_id']!=task_id: raise MeshError('invalid_input','Not a child of this task')
            reason='children'; request_id=_json(child_task_ids)
        elif reason in ('clarification','information','message','decision'):
            row=conn.execute('SELECT * FROM mesh_messages WHERE task_id=? AND message_id=? AND sender_id=?',(task_id,request_id,actor_id)).fetchone()
            if not row: raise MeshError('invalid_input','Waiting requires an outgoing request')
        elif reason in ('user_decision','reconciliation','permission'):
            if reason=='permission' and request_id:
                _state(conn,task,'waiting',reason,request_id)
                return _task(conn,task_id)
            pending=_decision(conn,task,reason,{'requester_id':actor_id},['answer','cancel'] if reason!='reconciliation' else ['confirm_success','retry','cancel'])
            return {**_task(conn,task_id),'decision':pending}
        else: raise MeshError('invalid_input','Unknown wait reason')
        _state(conn,task,'waiting',reason,request_id)
        if reason=='children':
            _wake_parent(conn,child)
        elif request_id and conn.execute('SELECT 1 FROM mesh_messages WHERE task_id=? AND reply_to=?',(task_id,request_id)).fetchone():
            _state(conn,task,'queued')
        return _task(conn,task_id)


def _close(conn,task,status,reason):
    stack=[task['task_id']]; affected=[]
    while stack:
        task_id=stack.pop()
        stack.extend(r[0] for r in conn.execute('SELECT task_id FROM workflow_tasks WHERE parent_task_id=?',(task_id,)))
        current=_task(conn,task_id)
        if current['status'] in TERMINAL: continue
        own_status=status if task_id==task['task_id'] else 'cancelled'
        _state(conn,current,own_status,reason if task_id==task['task_id'] else 'ancestor_'+status)
        conn.execute("UPDATE mesh_inbox SET status=CASE WHEN status='running' THEN 'stopping' ELSE 'cancelled' END,fencing_token=fencing_token+1,expires_at=0 WHERE task_id=? AND status IN ('pending','running')",(task_id,))
        conn.execute("UPDATE mesh_pending_decisions SET status='cancelled' WHERE task_id=? AND status='pending'",(task_id,))
        _message(conn,current,current['target_id'],own_status,{'reason':reason},wake=False)
        affected.append(task_id)
    _wake_parent(conn,task)
    if task['executor_kind']=='mesh_coordination':
        conn.execute('UPDATE workflow_runs SET status=?,terminal_at=?,updated_at=?,revision=revision+1 WHERE workflow_id=?',(status,time.time(),time.time(),task['workflow_id']))
    for row in conn.execute('SELECT DISTINCT t.task_id FROM workflow_task_deps d JOIN workflow_tasks t ON t.task_id=d.task_id WHERE d.depends_on_task_id IN ('+','.join('?' for _ in affected)+')',affected) if affected else []:
        dependent=_task(conn,row[0])
        if dependent['status'] not in TERMINAL:
            _state(conn,dependent,'waiting','dependency_failed',task['task_id'])
            _message(conn,dependent,dependent['target_id'],'decision_request',{'reason':'Hard dependency failed','dependency_id':task['task_id']})
    return affected


def fail_task(task_id,actor_id,error_code,reason=''):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id,True)
        if actor_id!=task['target_id']: raise MeshError('permission_denied','Only receiver reports failure',403)
        affected=_close(conn,task,'timeout' if error_code=='timeout' else 'failed',reason or error_code)
        return {**_task(conn,task_id),'affected_task_ids':affected}


def cancel_task(task_id,actor_id=None,reason=''):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id)
        if actor_id and actor_id!=task['caller_id']: raise MeshError('permission_denied','Only initiator cancels',403)
        affected=_close(conn,task,'cancelled',reason)
        return {**_task(conn,task_id),'affected_task_ids':affected,'affected_count':len(affected)}


def cancel_workflow(workflow_id,reason=''):
    with _tx() as conn:
        run=conn.execute("SELECT * FROM workflow_runs WHERE workflow_id=? AND kind='agent_mesh'",(workflow_id,)).fetchone()
        if not run: raise MeshError('not_found','Collaboration not found',404)
        affected=[]
        for row in conn.execute('SELECT task_id FROM mesh_task_bindings WHERE task_id IN (SELECT task_id FROM workflow_tasks WHERE workflow_id=?)',(workflow_id,)).fetchall():
            affected.extend(_close(conn,_task(conn,row[0]),'cancelled',reason))
        conn.execute("UPDATE workflow_tasks SET status='cancelled',revision=revision+1,updated_at=? WHERE workflow_id=? AND executor_kind='mesh_coordination' AND status NOT IN ('completed','cancelled','failed','timeout')",(time.time(),workflow_id))
        conn.execute("UPDATE workflow_runs SET status='cancelled',terminal_at=?,updated_at=?,revision=revision+1 WHERE workflow_id=?",(time.time(),time.time(),workflow_id))
        return {'workflow_id':workflow_id,'status':'cancelled','affected_task_ids':affected,'affected_count':len(affected)}


def allow_partial(task_id,actor_id,failed_child_ids,reason):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id,True)
        if actor_id!=task['caller_id']: raise MeshError('permission_denied','Only initiator approves partial delivery',403)
        if not failed_child_ids or not reason.strip(): raise MeshError('invalid_input','Failed children and reason required')
        for child_id in failed_child_ids:
            child=_task(conn,child_id)
            if child['parent_task_id']!=task_id or child['status'] not in TERMINAL-{'completed'}: raise MeshError('invalid_input','Can waive only failed direct children')
        conn.execute('UPDATE mesh_task_bindings SET partial_json=? WHERE task_id=?',(_json(sorted(set(task['partial']+failed_child_ids))),task_id))
        _state(conn,task,'queued')
        _message(conn,task,actor_id,'partial_delivery_allowed',{'failed_child_ids':failed_child_ids,'reason':reason})
        return _task(conn,task_id)


def pending_permission_decisions():
    """Trusted user inbox, including source coordination nodes."""
    with _tx() as conn:
        rows=conn.execute("""SELECT d.*,t.title AS task_title,
            COALESCE(json_extract(d.payload_json,'$.requester_id'),b.target_id) AS requester_id,
            COALESCE(a.name,json_extract(d.payload_json,'$.requester_id'),b.target_id) AS requester_name
            FROM mesh_pending_decisions d
            JOIN workflow_tasks t ON t.task_id=d.task_id
            JOIN mesh_task_bindings b ON b.task_id=d.task_id
            LEFT JOIN agent_templates a ON a.template_id=COALESCE(json_extract(d.payload_json,'$.requester_id'),b.target_id)
            WHERE d.status='pending' AND d.reason IN ('permission','budget','limit_reached','user_input','user_decision','reconciliation')
              AND t.status NOT IN ('completed','failed','cancelled','timeout')
              AND b.waiting_on_id=d.decision_id AND t.revision=d.expected_task_revision
            ORDER BY d.created_at,d.decision_id""").fetchall()
        return {'items':[_decode(row) for row in rows]}


def decide(decision_id,action,expected_task_revision,payload=None,idempotency_key=''):
    arguments=locals().copy()
    replay=None
    with _tx() as conn:
        cached=_cached(conn,'decision:'+decision_id,idempotency_key,arguments)
        if cached is not None: return cached
        row=conn.execute('SELECT * FROM mesh_pending_decisions WHERE decision_id=?',(decision_id,)).fetchone()
        if not row: raise MeshError('not_found','Decision not found',404)
        decision=_decode(row); task=_task(conn,decision['task_id'],open_only=True)
        if decision['status']!='pending' or task['waiting_on_id']!=decision_id or task['revision']!=expected_task_revision or expected_task_revision!=decision['expected_task_revision']: raise MeshError('stale_revision','Decision no longer current',409)
        if action not in decision['allowed_actions']: raise MeshError('invalid_input','Action not allowed')
        data=payload or {}
        if action=='cancel':
            _close(conn,task,'cancelled','User cancelled')
        elif action=='raise_limit':
            name=decision['payload']['limit']; value=data.get('value')
            if name not in ('depth','tasks','messages','revisions','tokens','tools','rounds'): raise MeshError('invalid_input','Unknown limit')
            if not isinstance(value,int) or value<decision['payload']['required'] or value>100000000: raise MeshError('invalid_input','Provide an explicit sufficient limit')
            conn.execute('UPDATE mesh_limits SET '+name+'=? WHERE workflow_id=?',(value,task['workflow_id']))
            restored='submitted' if decision['payload'].get('previous_status')=='submitted' else 'queued'
            _state(conn,task,restored)
            replay=decision['payload'].get('command')
            if not replay:
                requester=decision['payload'].get('requester_id',task['target_id'])
                sender=task['caller_id'] if requester==task['target_id'] else task['target_id']
                _message(conn,task,sender,'budget_increased',{'limit':name,'value':value})
        else:
            if not data and action not in ('approve','deny'): raise MeshError('invalid_input','Decision requires an answer or confirmation evidence')
            restored='submitted' if decision['payload'].get('previous_status')=='submitted' else 'queued'
            _state(conn,task,restored)
            uncertain_work=decision['payload'].get('work_id')
            if uncertain_work:
                conn.execute("UPDATE mesh_inbox SET status='consumed',error=? WHERE work_id=? AND status='uncertain'",('user_'+action,uncertain_work))
            requester=decision['payload'].get('requester_id',task['target_id'])
            sender=task['caller_id'] if requester==task['target_id'] else task['target_id']
            _message(conn,task,sender,'user_decision',{'action':action,'answer':data})
        conn.execute("UPDATE mesh_pending_decisions SET status='resolved',answer_json=? WHERE decision_id=?",(_json({'action':action,'payload':data}),decision_id))
        result={**_task(conn,task['task_id']),'decision_id':decision_id,'action':action}
        if replay:
            function={'delegate':delegate,'send_message':send_message,'review':review}.get(replay['operation'])
            if function: result=function(**replay['arguments'])
        _cache(conn,'decision:'+decision_id,idempotency_key,arguments,result)
    return result


def claim_work(owner_id,lease_seconds=30):
    with _tx() as conn:
        now=time.time()
        _queue_coordinations(conn)
        expired=conn.execute("SELECT * FROM mesh_inbox WHERE status='running' AND expires_at<=?",(now,)).fetchall()
        for row in expired:
            conn.execute("UPDATE mesh_inbox SET status='uncertain',fencing_token=fencing_token+1,error='lease_expired' WHERE work_id=?",(row['work_id'],))
            task=_task(conn,row['task_id'])
            if task['status'] not in TERMINAL:
                _decision(conn,task,'reconciliation',{'requester_id':row['agent_id'],'work_id':row['work_id']},['confirm_success','retry','cancel'])
        if conn.execute("SELECT count(*) FROM mesh_inbox WHERE status='running'").fetchone()[0]>=4: return None
        for row in conn.execute("SELECT * FROM mesh_inbox WHERE status='pending' ORDER BY created_at,sequence").fetchall():
            task=_task(conn,row['task_id'])
            if task['status'] in TERMINAL:
                conn.execute("UPDATE mesh_inbox SET status='cancelled' WHERE work_id=?",(row['work_id'],)); continue
            if conn.execute("SELECT 1 FROM mesh_pending_decisions WHERE task_id=? AND status='pending'",(task['task_id'],)).fetchone(): continue
            if conn.execute("SELECT count(*) FROM mesh_inbox WHERE agent_id=? AND status='running'",(row['agent_id'],)).fetchone()[0]>=2: continue
            if conn.execute("SELECT 1 FROM mesh_inbox WHERE task_id=? AND agent_id=? AND status='running'",(row['task_id'],row['agent_id'])).fetchone(): continue
            if row['agent_id']==task['target_id'] and task['status']=='submitted':
                conn.execute("UPDATE mesh_inbox SET status='consumed' WHERE work_id=?",(row['work_id'],)); continue
            if row['agent_id']==task['target_id'] and conn.execute("SELECT 1 FROM workflow_task_deps d JOIN workflow_tasks t ON t.task_id=d.depends_on_task_id WHERE d.task_id=? AND t.status!='completed'",(row['task_id'],)).fetchone(): continue
            sequence=conn.execute('SELECT COALESCE(MAX(sequence),0) FROM mesh_messages WHERE task_id=?',(row['task_id'],)).fetchone()[0]
            attempt_id=_id('ma'); lease=_id('lease'); fence=row['fencing_token']+1
            number=conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM workflow_attempts WHERE task_id=?',(row['task_id'],)).fetchone()[0]
            conn.execute('INSERT INTO workflow_attempts(attempt_id,task_id,sequence,status,intent_revision,idempotency_key,dispatch_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',(attempt_id,row['task_id'],number,'running',task['revision'],attempt_id,_json({'agent_id':row['agent_id'],'work_id':row['work_id']}),now,now))
            conn.execute("UPDATE mesh_inbox SET status='running',owner_id=?,lease_token=?,fencing_token=?,expires_at=?,attempt_id=?,sequence=? WHERE work_id=?",(owner_id,lease,fence,now+lease_seconds,attempt_id,sequence,row['work_id']))
            if row['agent_id']==task['target_id'] and task['status'] in ('queued','revision_requested'): _state(conn,task,'running')
            session=conn.execute('SELECT * FROM mesh_task_sessions WHERE task_id=? AND agent_id=?',(row['task_id'],row['agent_id'])).fetchone()
            work=dict(conn.execute('SELECT * FROM mesh_inbox WHERE work_id=?',(row['work_id'],)).fetchone())
            work.update(session_id=session['session_id'],task=_task(conn,row['task_id']),messages=[_decode(r) for r in conn.execute('SELECT * FROM mesh_messages WHERE task_id=? AND sequence>? AND sequence<=? ORDER BY sequence',(row['task_id'],session['last_consumed_sequence'],sequence))])
            work['role']='coordination' if task['executor_kind']=='mesh_coordination' else ('receiver' if row['agent_id']==task['target_id'] else 'caller')
            work['workspace_path']=task_workspace(row['task_id'])
            if work['role']=='coordination':
                work['task']['outcomes']=[_task(conn,r[0]) for r in conn.execute('SELECT task_id FROM workflow_tasks WHERE parent_task_id=?',(row['task_id'],))]
                for outcome in work['task']['outcomes']:
                    outcome['submissions']=[_decode(r) for r in conn.execute('SELECT * FROM mesh_submissions WHERE task_id=? ORDER BY revision',(outcome['task_id'],))]
            return work
        return None


def heartbeat(work_id,lease_token,fencing_token,lease_seconds=30):
    with _tx() as conn:
        now=time.time()
        work=conn.execute("SELECT i.*,a.created_at AS started_at FROM mesh_inbox i JOIN workflow_attempts a USING(attempt_id) WHERE i.work_id=? AND i.lease_token=? AND i.fencing_token=? AND i.status='running'",(work_id,lease_token,fencing_token)).fetchone()
        if work and now-work['started_at']>=300:
            task=_task(conn,work['task_id'])
            _close(conn,task,'timeout','Execution exceeded 300 seconds')
            return False
        return bool(conn.execute("UPDATE mesh_inbox SET expires_at=? WHERE work_id=? AND lease_token=? AND fencing_token=? AND status='running' AND expires_at>?",(now+lease_seconds,work_id,lease_token,fencing_token,now)).rowcount)


def finish_work(work_id,lease_token,fencing_token,error='',retry_safe=False):
    with _tx() as conn:
        row=conn.execute("SELECT * FROM mesh_inbox WHERE work_id=? AND lease_token=? AND fencing_token=? AND status='running' AND expires_at>?",(work_id,lease_token,fencing_token,time.time())).fetchone()
        if not row: return False
        task=_task(conn,row['task_id'])
        conn.execute('UPDATE workflow_attempts SET status=?,receipt_json=?,error_code=?,updated_at=? WHERE attempt_id=?',('failed' if error else 'completed',_json({'error':error}),error,time.time(),row['attempt_id']))
        if error:
            previous=conn.execute("SELECT count(*) FROM workflow_attempts WHERE task_id=? AND status='failed'",(row['task_id'],)).fetchone()[0]
            if retry_safe and previous<=1:
                conn.execute("UPDATE mesh_inbox SET status='pending',error=?,expires_at=0 WHERE work_id=?",(error,work_id))
            else:
                conn.execute("UPDATE mesh_inbox SET status='uncertain',error=?,expires_at=0 WHERE work_id=?",(error,work_id))
                if task['status'] not in TERMINAL: _decision(conn,task,'reconciliation',{'requester_id':row['agent_id'],'work_id':work_id,'error':error},['confirm_success','retry','cancel'])
        else:
            conn.execute("UPDATE mesh_inbox SET status='consumed',expires_at=0 WHERE task_id=? AND agent_id=? AND sequence<=? AND (status='pending' OR work_id=?)",(row['task_id'],row['agent_id'],row['sequence'],work_id))
            conn.execute('UPDATE mesh_task_sessions SET last_consumed_sequence=MAX(last_consumed_sequence,?) WHERE task_id=? AND agent_id=?',(row['sequence'],row['task_id'],row['agent_id']))
            if task['status']=='running' or (task['status']=='submitted' and row['agent_id']==task['caller_id']):
                _decision(conn,task,'user_decision',{'requester_id':row['agent_id'],'reason':'Execution ended without submit or explicit wait'},['answer','cancel'])
        return True


def acknowledge_stopped(work_id):
    with _tx() as conn:
        conn.execute("UPDATE workflow_attempts SET status='cancelled',updated_at=? WHERE attempt_id IN (SELECT attempt_id FROM mesh_inbox WHERE work_id=? AND status='stopping')",(time.time(),work_id))
        conn.execute("UPDATE mesh_inbox SET status='cancelled' WHERE work_id=? AND status='stopping'",(work_id,))


def recover_stopped_after_restart():
    """Call only once at process startup, after old runtime workers are gone."""
    with _tx() as conn:
        conn.execute("UPDATE workflow_attempts SET status='cancelled',updated_at=? WHERE attempt_id IN (SELECT attempt_id FROM mesh_inbox WHERE status='stopping')",(time.time(),))
        return conn.execute("UPDATE mesh_inbox SET status='cancelled' WHERE status='stopping'").rowcount


def active_tasks_for_agent(agent_id):
    with _tx() as conn:
        return [r[0] for r in conn.execute("SELECT t.task_id FROM workflow_tasks t JOIN mesh_task_bindings b USING(task_id) WHERE (b.caller_id=? OR b.target_id=?) AND t.status NOT IN ('completed','failed','cancelled','timeout')",(agent_id,agent_id))]


def has_active_tasks(agent_id):
    return bool(active_tasks_for_agent(agent_id))


def task_workspace(task_id):
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in task_id):
        raise MeshError('invalid_input','Invalid task ID')
    return str(data_root() / 'data' / 'mesh' / task_id)


def _public_artifact(row,actor_id=None):
    item=_decode(row)
    item.pop('uri',None)
    metadata=item['metadata']
    item['can_forward']=bool(actor_id and (metadata.get('created_by')==actor_id or metadata.get('allow_forward')))
    # This stored override is not the complete rule: creators may also forward.
    # Expose the effective capability for the requesting actor instead.
    metadata.pop('allow_forward',None)
    item['name']=item['metadata'].get('name','Artifact')
    item['mime']=item['metadata'].get('mime','application/octet-stream')
    return item


def register_artifact(task_id,actor_id,path,name='',mime=''):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id,True)
        root=Path(task_workspace(task_id)).resolve()
        candidate=Path(path).expanduser().resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise MeshError('permission_denied','Artifact must be a file inside this task workspace',403)
        artifact_id=_id('mar')
        metadata={'name':name or candidate.name,'mime':mime or mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream','visible_to':[task['caller_id'],task['target_id']],'allow_forward':False,'created_by':actor_id,'submission_revision':task['result_revision']+1}
        conn.execute('INSERT INTO workflow_artifacts VALUES (?,?,?,?,?,?,?)',(artifact_id,task_id,'mesh_file',str(candidate),hashlib.sha256(candidate.read_bytes()).hexdigest(),_json(metadata),time.time()))
        return _public_artifact(conn.execute('SELECT * FROM workflow_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone(),actor_id)


def artifact(artifact_id,actor_id=None):
    with _tx() as conn:
        row=conn.execute('SELECT * FROM workflow_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
        if not row: raise MeshError('not_found','Artifact not found',404)
        metadata=json.loads(row['metadata_json'])
        source=_task(conn,row['task_id'])
        if actor_id and actor_id not in (source['caller_id'],source['target_id']):
            granted=False
            if actor_id in metadata.get('visible_to',[]):
                for target_task_id in metadata.get('forwarded_to_tasks',[]):
                    target_task=_task(conn,target_task_id)
                    if actor_id in (target_task['caller_id'],target_task['target_id']): granted=True
            if not granted: raise MeshError('permission_denied','No task-scoped artifact grant',403)
        candidate=Path(row['uri']).resolve()
        if not candidate.is_relative_to(Path(task_workspace(row['task_id'])).resolve()) or not candidate.is_file():
            raise MeshError('permission_denied','Artifact path is outside its task workspace',403)
        if hashlib.sha256(candidate.read_bytes()).hexdigest()!=row['sha256']:
            raise MeshError('stale_revision','Artifact content changed since publication',409)
        return {**_public_artifact(row,actor_id),'path':str(candidate)}


def request_decision(task_id,actor_id,reason,allowed_actions,payload):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id,True)
        if reason not in ('permission','user_input','user_decision','reconciliation','budget'):
            raise MeshError('invalid_input','Invalid decision reason')
        valid={'permission':{'approve','deny','cancel'},'user_input':{'answer','cancel'},'user_decision':{'answer','cancel'},'reconciliation':{'confirm_success','retry','cancel'},'budget':{'raise_limit','cancel'}}[reason]
        if not allowed_actions or not set(allowed_actions)<=valid:
            raise MeshError('invalid_input','Invalid decision actions')
        return _decision(conn,task,reason,{**payload,'requester_id':actor_id},allowed_actions)


def consume_permission(task_id,actor_id,fingerprint):
    with _tx() as conn:
        _task(conn,task_id,actor_id,True)
        for row in conn.execute("SELECT * FROM mesh_pending_decisions WHERE task_id=? AND reason='permission' AND status='resolved' ORDER BY created_at DESC",(task_id,)):
            decision=_decode(row)
            if decision['payload'].get('requester_id')==actor_id and decision['payload'].get('fingerprint')==fingerprint:
                answer=decision['answer']
                claimed=answer.get('consumed_by')
                work_id=_fence.get()[0] if _fence.get() else 'direct'
                if claimed: continue
                answer['consumed_by']=work_id
                conn.execute('UPDATE mesh_pending_decisions SET answer_json=? WHERE decision_id=?',(_json(answer),decision['decision_id']))
                return answer
        return None


def bind_work_session(work_id,session_id,run_id=''):
    with _tx() as conn:
        row=conn.execute("SELECT * FROM mesh_inbox WHERE work_id=? AND status='running' AND expires_at>?",(work_id,time.time())).fetchone()
        if not row: return False
        conn.execute('UPDATE mesh_task_sessions SET session_id=? WHERE task_id=? AND agent_id=?',(session_id,row['task_id'],row['agent_id']))
        conn.execute('UPDATE workflow_attempts SET dispatch_json=? WHERE attempt_id=?',(_json({'work_id':work_id,'agent_id':row['agent_id'],'session_id':session_id,'run_id':run_id}),row['attempt_id']))
        return True


def _queue_coordinations(conn):
    for row in conn.execute("SELECT task_id,workflow_id FROM workflow_tasks WHERE executor_kind='mesh_coordination' AND status='waiting'").fetchall():
        task=_task(conn,row['task_id'])
        # Coordination waits for the whole workflow, including explicit child waits.
        # Keep human decisions and other waits intact; polling also repairs persisted waits.
        if task['wait_reason'] not in ('','children'): continue
        if conn.execute("SELECT 1 FROM mesh_pending_decisions WHERE task_id=? AND status='pending'",(row['task_id'],)).fetchone(): continue
        if conn.execute("SELECT 1 FROM workflow_tasks WHERE workflow_id=? AND executor_kind='mesh_agent' AND status NOT IN ('completed','failed','cancelled','timeout')",(row['workflow_id'],)).fetchone(): continue
        if conn.execute("SELECT 1 FROM mesh_inbox i JOIN workflow_tasks t USING(task_id) WHERE t.workflow_id=? AND i.status IN ('running','stopping')",(row['workflow_id'],)).fetchone(): continue
        _state(conn,task,'queued')
        _message(conn,task,task['caller_id'],'coordination_ready',{'reason':'All delegated tasks ended'})


def finish_coordination(task_id,actor_id,result):
    with _tx() as conn:
        task=_task(conn,task_id,actor_id,True)
        if task['executor_kind']!='mesh_coordination': raise MeshError('invalid_input','Not a coordination task')
        if conn.execute("SELECT 1 FROM workflow_tasks WHERE workflow_id=? AND executor_kind='mesh_agent' AND status NOT IN ('completed','failed','cancelled','timeout')",(task['workflow_id'],)).fetchone():
            raise MeshError('invalid_input','Collaboration has unfinished tasks')
        if not isinstance(result,dict) or not (result.get('summary') or result.get('reply')) or result.get('termination') in ('mesh_yield','wait','failed','cancelled'):
            raise MeshError('invalid_input','A successful final summary is required')
        revision=task['result_revision']+1
        conn.execute('INSERT INTO mesh_submissions VALUES (?,?,?,?)',(task_id,revision,_json(result),time.time()))
        conn.execute('UPDATE mesh_task_bindings SET result_revision=? WHERE task_id=?',(revision,task_id))
        conn.execute("UPDATE mesh_pending_decisions SET status='cancelled' WHERE task_id=? AND status='pending'",(task_id,))
        conn.execute("UPDATE mesh_inbox SET status='cancelled' WHERE task_id=? AND status='pending'",(task_id,))
        partial=bool(conn.execute("SELECT 1 FROM workflow_tasks WHERE workflow_id=? AND executor_kind='mesh_agent' AND status!='completed'",(task['workflow_id'],)).fetchone())
        _state(conn,task,'completed')
        conn.execute('UPDATE workflow_runs SET status=?,terminal_at=?,updated_at=?,revision=revision+1 WHERE workflow_id=?',('partial' if partial else 'completed',time.time(),time.time(),task['workflow_id']))
        _message(conn,task,actor_id,'coordination_completed',{'result':result,'partial':partial},wake=False)
        _deliver_summary(conn,task,result,partial)
        return _task(conn,task_id)


def _deliver_summary(conn,task,result,partial):
    from server.runtime.session_history import record
    run=conn.execute('SELECT input_snapshot_json FROM workflow_runs WHERE workflow_id=?',(task['workflow_id'],)).fetchone()
    origin=json.loads(run[0]); session_id=origin.get('origin_session_id','')
    fence=conn.execute('SELECT * FROM session_fences WHERE session_id=?',(session_id,)).fetchone()
    if not fence or fence['deleted_at'] is not None or origin.get('origin_epoch')!=fence['epoch']: return
    body=result.get('summary') or result.get('reply')
    if partial: body='部分协作结果（存在未完成分支）：\n'+body
    message_id='mesh_summary_'+task['workflow_id']
    meta={'mesh_workflow_id':task['workflow_id'],'mesh_task_id':task['task_id'],'partial':partial}
    if task['origin_group_id']:
        member=conn.execute('SELECT instance_id FROM group_memberships WHERE group_id=? AND template_id=?',(task['origin_group_id'],task['caller_id'])).fetchone()
        if not member: return
        conn.execute('INSERT OR IGNORE INTO feed_events(event_id,group_id,speaker_type,speaker_id,content,ts,meta) VALUES (?,?,?,?,?,?,?)',(message_id,task['origin_group_id'],'agent',member[0],body,time.time(),_json(meta)))
        record(conn,session_id,'feed',message_id,instance_id=member[0],expected_epoch=fence['epoch'])
    elif conn.execute('SELECT 1 FROM agent_templates WHERE template_id=?',(task['caller_id'],)).fetchone():
        conn.execute('INSERT OR IGNORE INTO assistant_messages(message_id,template_id,role,content,ts,meta,session_id) VALUES (?,?,?,?,?,?,?)',(message_id,task['caller_id'],'assistant',body,time.time(),_json(meta),session_id))
        record(conn,session_id,'message',message_id,expected_epoch=fence['epoch'])
