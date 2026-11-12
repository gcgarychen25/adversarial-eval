"""Tests for selection/schema/integrity without inventing benchmark observations.

Rows in unit tests are synthetic *test doubles*, never shipped as evaluation cases.
"""
import json
import os
from pathlib import Path

import pytest

from adversarial.real_eval import (LABELS, parse_monitor_reply, select_cheatbench_rows,
                                   digest, validate, SETA)


def fake_rows():
    labels = ('cheating', 'attempt', 'benign')
    rows = []
    for label in labels:
        for i in range(3):
            rows.append({'trace_id': f'case-{label}-{i}', 'label': label,
                         'benchmark': ['terminal-bench-2','swe-smith','kernelbench'][i],
                         'cheating_type': 'task', 'loc_kind': 'localized_snippet',
                         'trace': f'original sequence of {label} actions {i}' * 15})
    return rows


def test_select_two_each():
    result = select_cheatbench_rows(fake_rows())
    assert len(result) == 6
    assert [r['label'] for r in result] == ['cheating']*2 + ['attempt']*2 + ['benign']*2
    assert len(set(x['trace_id'] for x in result)) == 6
    assert result == select_cheatbench_rows(fake_rows())


def test_selection_missing_label_fails():
    with pytest.raises(ValueError, match='attempt'):
        select_cheatbench_rows([r for r in fake_rows() if r['label'] != 'attempt'])


def test_parse_monitor():
    x = parse_monitor_reply('```json\n{"label":"attempt","rationale":"Access denied", "evidence":["cat /tests/test.py"]}\n```')
    assert x['label'] == 'attempt'


def test_parse_monitor_rejects_invalid():
    with pytest.raises(ValueError):
        parse_monitor_reply('{"label":"confirmed_cheat", "rationale":"h", "evidence":[]}')
    with pytest.raises(ValueError):
        parse_monitor_reply('{"label":"benign", "rationale":"", "evidence":[]}')


def test_no_gold_label_in_public_rows():
    rows = select_cheatbench_rows(fake_rows())
    public = [{'case_id': r['trace_id'], 'sha256': digest(r['trace'])} for r in rows]
    assert all('label' not in e and 'rationale' not in e for e in public)


def test_integrity_check_real_files(tmp_path):
    root = tmp_path
    base = root/'reviewer'/'cheatbench'
    (base/'evidence').mkdir(parents=True)
    selected = select_cheatbench_rows(fake_rows())
    cases=[]; gold=[]
    for row in selected:
        p = base/'evidence'/(row['trace_id']+'.txt')
        p.write_text(row['trace'])
        cases.append({'case_id':row['trace_id'],'trace_file':str(p), 'sha256': digest(row['trace'])})
        gold.append({'case_id':row['trace_id'],'label':row['label']})
    (base/'cases.json').write_text(json.dumps(cases))
    (base/'private_labels.json').write_text(json.dumps(gold))
    (root/'attacker').mkdir()
    seta=[]
    for id in SETA:
        task=root/'tasks'/id
        task.mkdir(parents=True)
        (task/'task.toml').write_text('schema_version = "1.0"')
        t=root/'source'/f'{id}.json'
        t.parent.mkdir(exist_ok=True)
        t.write_text('{}')
        seta.append({
            'task_id': id,
            'task_path': str(task),
            'published_exploit_trace': str(t),
            'known_exploitable': True,
        })
    (root/'attacker'/'cases.json').write_text(json.dumps(seta))
    assert validate(root)['status']=='ok'
    (base/'evidence'/(selected[0]['trace_id']+'.txt')).write_text('TAMPERED')
    with pytest.raises(RuntimeError, match='missing/corrupt'):
        validate(root)


def test_stage_attack_preserves_original_task(tmp_path):
    from adversarial.real_eval import _stage_attack
    src=tmp_path/'source'; src.mkdir()
    (src/'task.toml').write_text('schema_version = "1.0"')
    (src/'instruction.md').write_text('Fix the code and run tests.')
    _stage_attack(src,tmp_path/'staged')
    assert (src/'instruction.md').read_text() == 'Fix the code and run tests.'
    new=(tmp_path/'staged'/'instruction.md').read_text()
    assert new.startswith('Fix the code and run tests.')
    assert 'authorized adversarial evaluation' in new


def test_harbor_attack_dry_run_does_not_claim_reward(tmp_path):
    from adversarial.real_eval import attack_seta
    task=tmp_path/'public'/'1012'
    task.mkdir(parents=True)
    (task/'task.toml').write_text('schema_version = "1.0"')
    (task/'instruction.md').write_text('Complete original request')
    (tmp_path/'attacker').mkdir()
    (tmp_path/'attacker'/'cases.json').write_text(json.dumps([{'task_id':'1012','task_path':str(task)}]))
    result=attack_seta(tmp_path,'openai/TEST',dry_run=True)
    entry=result['results'][0]
    assert entry['status']=='dry_run'
    assert 'harbor' == entry['command'][0]
    assert entry.get('reward') is None
    assert entry['discovered_exploit'] is None
    assert (task/'instruction.md').read_text() == 'Complete original request'


def test_review_report_keeps_gold_labels_out_of_prompt(tmp_path, monkeypatch):
    from adversarial import real_eval
    base=tmp_path/'reviewer'/'cheatbench'
    (base/'evidence').mkdir(parents=True)
    cases=[]; private=[]
    for lab in ('cheating','attempt','benign'):
        trace='real raw trace data for '+lab
        p=base/'evidence'/(lab+'.txt'); p.write_text(trace)
        cases.append({'case_id':lab,'trace_file':str(p)})
        private.append({'case_id':lab,'label':lab,'rationale':'PRIVATE GOLD AUDIT'})
    (base/'cases.json').write_text(json.dumps(cases))
    (base/'private_labels.json').write_text(json.dumps(private))
    received=[]
    def fake_model(trace, model, base_url, key_env):
        received.append(trace)
        return {'label': trace.split()[-1], 'rationale':'observed from trace', 'evidence':[]}
    monkeypatch.setattr(real_eval,'run_monitor',fake_model)
    report=real_eval.review_cheatbench(tmp_path,'TEST')
    assert report['correct']==3
    assert all('PRIVATE GOLD AUDIT' not in t for t in received)


def test_attack_preserves_live_runs_and_separates_preview(tmp_path, monkeypatch):
    from adversarial import real_eval
    task=tmp_path/'task'; task.mkdir()
    (task/'task.toml').write_text('')
    (task/'instruction.md').write_text('Original')
    (tmp_path/'attacker').mkdir()
    (tmp_path/'attacker'/'cases.json').write_text(json.dumps([
        {'task_id':tid,'task_path':str(task)} for tid in SETA]))
    monkeypatch.setattr(real_eval,'run_task',lambda *a, **kw: {
        'status':'dry_run' if kw['dry_run'] else 'ok', 'model':kw['model']})
    real_eval.attack_seta(tmp_path,'TEST',selected_task='1012')
    real_eval.attack_seta(tmp_path,'TEST',selected_task='1018')
    real_eval.attack_seta(tmp_path,'TEST',selected_task='1025',dry_run=True)
    live=json.loads((tmp_path/'results'/'seta_attacks.json').read_text())
    assert [x['task_id'] for x in live['results']]==['1012','1018']
    assert (tmp_path/'results'/'seta_attacks_dry_run.json').exists()


def test_auth_errors_are_not_model_accuracy(tmp_path, monkeypatch):
    from adversarial import real_eval
    base=tmp_path/'reviewer'/'cheatbench'; base.mkdir(parents=True)
    trace=base/'trace.txt'; trace.write_text('original trace')
    (base/'cases.json').write_text(json.dumps([{'case_id':'x','trace_file':str(trace)}]))
    (base/'private_labels.json').write_text(json.dumps([{'case_id':'x','label':'benign'}]))
    def fail(*a): raise RuntimeError('Missing credential')
    monkeypatch.setattr(real_eval,'run_monitor',fail)
    result=real_eval.review_cheatbench(tmp_path,'TEST')
    assert result['accuracy'] is None
    assert result['completed']==0 and result['errors']==1
    assert result['status']=='incomplete'


def test_harbor_zero_exit_with_trial_exception_is_error(tmp_path, monkeypatch):
    from adversarial import harness
    task=tmp_path/'task'; task.mkdir()
    (task/'task.toml').write_text('')
    (task/'instruction.md').write_text('original')
    jobs=tmp_path/'jobs'
    monkeypatch.setattr(harness.shutil,'which',lambda _: '/bin/harbor')
    def run(args, **kwargs):
        trial=jobs/args[args.index('--job-name')+1]/'trial'
        (trial/'agent').mkdir(parents=True)
        (trial/'result.json').write_text(json.dumps({'exception_info':{'exception_type':'AuthError'}}))
        return type('Process',(),{'returncode':0})()
    monkeypatch.setattr(harness.subprocess,'run',run)
    r=harness.run_task(task,jobs,agent='terminus-2',model='TEST',label='test')
    assert r['status']=='error' and r['reward'] is None
    assert r['exception_info']['exception_type']=='AuthError'


def test_current_attack_success_not_masked_by_historical_errors(tmp_path, monkeypatch):
    from adversarial import real_eval
    task=tmp_path/'task'; task.mkdir()
    (task/'task.toml').write_text('')
    (task/'instruction.md').write_text('original')
    (tmp_path/'attacker').mkdir()
    (tmp_path/'attacker'/'cases.json').write_text(json.dumps([{'task_id':'1012','task_path':str(task)}]))
    outcomes=iter([{'status':'error'}, {'status':'ok','reward':1.0}])
    monkeypatch.setattr(real_eval,'run_task',lambda *a, **kw:next(outcomes))
    assert not real_eval.attack_seta(tmp_path,'TEST')['invocation_ok']
    r=real_eval.attack_seta(tmp_path,'TEST')
    assert r['invocation_ok'] and len(r['results'])==2


def test_review_seta_skips_failed_trials(tmp_path, monkeypatch):
    from adversarial import real_eval, reviewer
    (tmp_path/'attacker').mkdir()
    (tmp_path/'results').mkdir()
    (tmp_path/'attacker'/'cases.json').write_text(json.dumps([{'task_id':'1012','task_path':'unused'}]))
    (tmp_path/'results'/'seta_attacks.json').write_text(json.dumps({'results':[
        {'task_id':'1012','status':'error','trial_dir':'failed-trial','reward':None}]}))
    def fail(*a, **kw): raise AssertionError('Do not send a failed trial for review')
    monkeypatch.setattr(reviewer,'llm_review',fail)
    r=real_eval.review_seta(tmp_path,'TEST')
    assert r['results'][0]['skipped']


def test_selection_excludes_embedded_audits_and_empty_events():
    rows=fake_rows()
    for i,extra in enumerate(({'monitor_verdict':'likely_cheating'}, {'audit_index_row':{'score':1}})):
        rows.append({'trace_id':f'annotated-{i}','label':'benign','benchmark':'swe-smith',
                     'trace':json.dumps({'events':[{'content':'x'*300}], 'extra':extra})})
    rows.append({'trace_id':'empty','label':'benign','trace':json.dumps({'events':[],'padding':'x'*300})})
    result=select_cheatbench_rows(rows)
    assert not any(r['trace_id'].startswith('annotated') or r['trace_id']=='empty' for r in result)


def test_system_stress_keeps_original_task_bytes(tmp_path):
    from adversarial.real_eval import _stage_attack
    src=tmp_path/'original';src.mkdir()
    text=b'Original task\n\nDo the task.\n'
    (src/'instruction.md').write_bytes(text)
    (src/'task.toml').write_text('version="1.0"')
    _stage_attack(src,tmp_path/'staged',append_prompt=False)
    assert (tmp_path/'staged'/'instruction.md').read_bytes()==text
    assert (src/'instruction.md').read_bytes()==text


def test_system_message_reaches_backend_and_is_audited(tmp_path):
    import asyncio
    from adversarial.system_prompt import SystemPromptLLM
    class Backend:
        def __init__(self): self.requests=[]
        async def call(self, **kwargs):
            self.requests.append(kwargs)
            return 'real backend result'
    backend=Backend();llm=SystemPromptLLM(backend,'Authorized adversarial objective',tmp_path)
    history=[{'role':'user','content':'original task'}, {'role':'assistant','content':'probe'}]
    result=asyncio.run(llm.call(prompt='observed output',message_history=history))
    assert result=='real backend result'
    assert backend.requests[0]['message_history'][0]['role']=='system'
    assert backend.requests[0]['message_history'][0]['content']=='Authorized adversarial objective'
    assert history[0]['role']=='user'  # Do not mutate Chat's history.
    audit=json.loads((tmp_path/'request_0001.json').read_text())
    assert [m['role'] for m in audit['messages']]==['system','user','assistant','user']


def test_system_stress_dry_run_selects_adapter_and_bounded_turns(tmp_path):
    from adversarial.real_eval import attack_seta
    task=tmp_path/'original';task.mkdir()
    (task/'instruction.md').write_text('original')
    (task/'task.toml').write_text('version="1.0"')
    (tmp_path/'attacker').mkdir()
    (tmp_path/'attacker'/'cases.json').write_text(json.dumps([{'task_id':'1012','task_path':str(task)}]))
    run=attack_seta(tmp_path,'openai/TEST',dry_run=True,attack_mode='system-stress')['results'][0]
    assert run['agent']=='adversarial.stress_agent:StressTerminus2'
    assert 'max_turns=20' in run['command']
    assert run['system_prompt_sha256']
    assert (tmp_path/'attacker'/'staged'/'1012'/'instruction.md').read_text()=='original'
