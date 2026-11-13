# -*- coding: utf-8 -*-
"""仅重算已有完整独立质检证据的计划，不把失败质检冒充通过。"""
import hashlib
import json
from pathlib import Path
from quality_routing import dependencies, route, evaluate_rules
from table_pipeline import execute, number, normalize, verification_view
from pipeline_contracts import validate


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_approved(path, question, base):
    path=Path(path).resolve();record=json.loads(path.read_text())
    if (record.get('evaluation_only') or record.get('quality_status')!='passed'
            or str(record['id'])!=str(question['id']) or record['file_name']!=question['file_name']):
        raise ValueError('原记录没有可复用的同题程序质检')
    evidence_path=Path(record['answer_evidence']);evidence=json.loads(evidence_path.read_text())
    table,context,plan=evidence['table'],evidence['context'],evidence['plan'];validate('plan',plan)
    if plan!=record.get('plan'):
        raise ValueError('原结果计划与证据不一致')
    expected=hashlib.sha256((base.FILES_DIR/question['file_name']).read_bytes()+record['recognition_revision'].encode()).hexdigest()
    if Path(record['recognition_artifact']).name!=expected:
        raise ValueError('原文件内容与识别证据不匹配')
    rounds=record.get('quality_rounds',[])
    if not rounds or rounds[-1].get('status')!='accepted' or rounds!=evidence.get('quality_rounds'):
        raise ValueError('最终质检状态或证据不一致')
    targets=dependencies(table,context,plan,question['question_type'],number,normalize)
    fields={v['id']:v for v in rounds[-1]['fields']}
    if set(fields)!=set(targets) or len(fields)!=len(rounds[-1]['fields']):
        raise ValueError('质检依赖缺失或重复')
    task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
    plan_calls=[];verification=[]
    for p in (path.parent/'pipeline').glob('call-*.json'):
        call=json.loads(p.read_text());data=call.get('input',{})
        if data.get('task')!=task or not call.get('contract_valid'):continue
        if call['phase']=='plan' and json.loads(call['content'])==plan:
            plan_calls.append(p)
        if call['phase']=='verify' and data.get('plan')==plan and data.get('targets')==targets:
            verdict=json.loads(call['content']);validate('verify',verdict)
            checks={v['id']:v for v in verdict['checks']}
            if (verdict['plan_valid'] and verdict['structure_valid'] and len(checks)==len(verdict['checks']) and set(checks)==set(targets)
                    and all(fields[k]['verification']==checks[k] and fields[k]['route']=='accepted'
                            and route(v,checks[k],[r for r in rounds[-1]['rules'] if k in r['ids']],base.CONFIDENCE_LOW,base.CONFIDENCE_HIGH)[0]=='accepted'
                            for k,v in targets.items())):
                verification.append(p)
    if not plan_calls or not verification:
        raise ValueError('缺少匹配当前题目和全部依赖的模型计划或独立核验调用')
    if any(r['state']=='failed' for r in evaluate_rules(table,plan['rules'],number)):
        raise ValueError('适用规则重算失败')
    value=execute(table,plan,question['question_type'],context)
    answer=base.format_answer(json.dumps({'answer':value},ensure_ascii=False),question['question_type'],question.get('answer_format'),[table,context])
    return record,evidence,answer,plan_calls[-1],verification[-1]


def validate_replay(evidence,record,question,base):
    link=evidence['approved_execution_replay']
    if sha(link['record'])!=link['record_sha256'] or sha(link['evidence'])!=link['evidence_sha256']:
        raise ValueError('复用的原质检记录或证据已变化')
    old,old_evidence,answer,_,_=inspect_approved(link['record'],question,base)
    if (answer!=record['answer'] or old_evidence['table']!=evidence['table']
            or old_evidence['context']!=evidence['context'] or old_evidence['plan']!=evidence['plan']):
        raise ValueError('重算结果或依据与原质检不一致')
