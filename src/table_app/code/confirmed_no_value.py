# -*- coding: utf-8 -*-
"""复用原图质检和用户确认，将指定符号记为无值而不修改原始识别。"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import document_parser
import table_solver as base
from quality_routing import cell_at
from table_pipeline import atomic_json
from table_pipeline import execute


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_filter_replay(record, question, manifest_path):
    """重算筛选题：确认只覆盖原计划中的真实破折号位置。"""
    manifest_path=Path(manifest_path); link=record['confirmed_no_value_filter']
    if digest(manifest_path)!=link['manifest_sha256']:
        raise ValueError('无值确认文件已变化')
    item=next((x for x in json.loads(manifest_path.read_text()) if str(x['id'])==str(question['id'])),None)
    if (not item or item.get('scope')!='user_confirmed_scope_unknown' or item.get('meaning')!='no_value'
            or item.get('question')!=question['question'] or item.get('file_name')!=question['file_name']
            or item.get('file_sha256')!=digest(base.FILES_DIR/question['file_name'])):
        raise ValueError('无值确认与原题或文件不匹配')
    source=Path(item['prior_record'])
    if digest(source)!=item['prior_record_sha256']:raise ValueError('原质检记录已变化')
    prior=json.loads(source.read_text());evidence=json.loads(Path(prior['answer_evidence']).read_text())
    if prior.get('quality_status')!='passed' or evidence['plan']!=prior.get('plan'):
        raise ValueError('原计划未通过质检或与证据不符')
    document_parser.validate_saved_result(prior,evidence,question,base,source.parent/'pipeline',evidence_only=True)
    expressions=evidence['plan'].get('expressions',[])
    if len(expressions)!=1 or expressions[0].get('op')!='query':raise ValueError('无值确认仅适用于筛选计划')
    expr=expressions[0];numeric_cols={x['column'] for x in expr['filters'] if x['cmp'] in ('gt','ge','lt','le')}
    expected=[]
    for row in expr['rows']:
        for col in sorted(numeric_cols):
            cell=cell_at(evidence['table'],[row,col])
            if cell['state']=='value' and cell['text'].strip() in ('-','--','—','–','－'):expected.append([row,col])
    if not expected or expected!=item.get('refs'):raise ValueError('无值位置与原计划或原始符号不一致')
    value=execute(evidence['table'],evidence['plan'],question['question_type'],evidence['context'],confirmed_no_value_refs=expected)
    answer=base.format_answer(json.dumps({'answer':value},ensure_ascii=False),question['question_type'],question.get('answer_format'))
    if record.get('answer')!=answer:raise ValueError('无值筛选重算结果不一致')
    return answer


def validate_replayed_state(state, question):
    """续跑时重新绑定用户确认、原始质检、文件和符号。"""
    record_path, evidence_path = Path(state['record_path']), Path(state['evidence_path'])
    if digest(record_path) != state['record_sha256'] or digest(evidence_path) != state['evidence_sha256']:
        raise ValueError('无值状态记录或证据已变化')
    record = json.loads(record_path.read_text(encoding='utf-8'))
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    manifest = Path(evidence['confirmation_manifest']); source = Path(evidence['source_record'])
    prior_evidence_path = Path(evidence['source_evidence']); confirmation = evidence['confirmation']
    if (digest(manifest) != evidence['confirmation_manifest_sha256']
            or confirmation not in json.loads(manifest.read_text(encoding='utf-8'))
            or digest(source) != evidence['source_record_sha256']
            or digest(prior_evidence_path) != evidence['source_evidence_sha256']
            or confirmation.get('file_sha256') != digest(base.FILES_DIR / question['file_name'])
            or confirmation.get('question') != question['question']
            or str(confirmation.get('id')) != str(question['id'])
            or record.get('answer_state') != 'verified_no_value'):
        raise ValueError('无值状态来源、确认或题目不匹配')
    prior = json.loads(source.read_text(encoding='utf-8'))
    facts = json.loads(prior_evidence_path.read_text(encoding='utf-8'))
    expressions = facts['plan'].get('expressions', [])
    if (len(expressions) != 1 or expressions[0].get('op') != 'cell'
            or expressions[0].get('ref') != confirmation.get('ref')):
        raise ValueError('无值状态不对应原始单值计划')
    cell = cell_at(facts['table'], confirmation['ref'])
    if (cell.get('state') != 'value' or cell.get('text') != confirmation.get('original_symbol')
            or cell.get('text') not in ('-', '--', '—', '–')):
        raise ValueError('无值状态原符号不匹配')
    document_parser.validate_saved_result(prior, facts, question, base, source.parent/'pipeline', evidence_only=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--formal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    confirmations = json.loads(args.manifest.read_text(encoding='utf-8'))
    questions, _ = base.apply_corrections(base.read_questions(base.TESTS_PATH))
    by_id = {str(q['id']):q for q in questions}
    prior = base.read_submission(args.formal)
    if len(prior) != len(by_id) or set(prior) != set(by_id):
        raise ValueError('正式汇总题目ID不完整')
    output = args.output / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output.mkdir(parents=True, exist_ok=False)
    prepared = []
    for item in confirmations:
        key = str(item['id']); q = by_id[key]; source = Path(item['prior_record'])
        if (item.get('meaning') != 'no_value' or item.get('scope') != 'user_confirmed_scope_unknown'
                or item.get('question') != q['question'] or item.get('file_name') != q['file_name']
                or digest(base.FILES_DIR / q['file_name']) != item['file_sha256']
                or not source.is_file() or digest(source) != item['prior_record_sha256']
                or prior[key] not in ('', None)):
            raise ValueError('用户确认、原文件、题目或历史记录不匹配')
        record = json.loads(source.read_text(encoding='utf-8'))
        evidence_path = Path(record['answer_evidence'])
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        plan = evidence['plan']; expressions = plan.get('expressions', [])
        if (str(record.get('id')) != key or record.get('quality_status') != 'passed'
                or plan.get('kind') != 'calculate' or plan.get('as_array') or plan.get('rules')
                or plan.get('scale') != '1' or plan.get('decimals') is not None or plan.get('percent')
                or len(expressions) != 1 or expressions[0].get('op') != 'cell'
                or expressions[0].get('ref') != item.get('ref')):
            raise ValueError('历史计划不是经核验的单值提取')
        cell = cell_at(evidence['table'], item['ref'])
        if (cell.get('state') != 'value' or cell.get('text') != item.get('original_symbol')
                or cell['text'] not in ('-', '--', '—', '–')):
            raise ValueError('目标不是用户确认的原始无值符号')
        document_parser.validate_saved_result(record, evidence, q, base, source.parent/'pipeline', evidence_only=True)
        prepared.append((key, item, record, evidence_path, source))
    if len({key for key, *_ in prepared}) != len(prepared):
        raise ValueError('用户确认题目重复')
    submission = output/'submission.xlsx'
    base.write_submission(questions, {k:v for k,v in prior.items() if v not in ('',None)}, submission)
    reread = base.read_submission(submission)
    if reread != prior:
        raise ValueError('Excel回读与正式汇总不一致')
    entries=[]
    for key, item, original, old_evidence, source in prepared:
        evidence_path = output / f'evidence-{key}.json'
        result_path = output / f'result-{key}.json'
        evidence = {'task':{k:by_id[key].get(k) or '' for k in ('question_type','question','table_hint','answer_format')},
                    'source_record':str(source.resolve()), 'source_record_sha256':digest(source),
                    'source_evidence':str(old_evidence.resolve()), 'source_evidence_sha256':digest(old_evidence),
                    'confirmation':item, 'confirmation_manifest':str(args.manifest.resolve()),
                    'confirmation_manifest_sha256':digest(args.manifest), 'quality_rounds':original['quality_rounds']}
        atomic_json(evidence_path,evidence)
        result = {'id':key, 'file_name':by_id[key]['file_name'], 'question_type':by_id[key]['question_type'],
                  'status':'unanswered', 'answer':'', 'answer_state':'verified_no_value',
                  'quality_status':'passed', 'execution_status':'succeeded', 'format_status':'valid',
                  'excel_status':'written', 'excel_answer':'', 'quality_rounds':original['quality_rounds'],
                  'answer_evidence':str(evidence_path.resolve()), 'source_record':str(source.resolve()),
                  'meaning':'用户确认原始符号表示无值；未改写原始识别，未将其视为零或unknown'}
        atomic_json(result_path,result)
        entries.append({'id':key,'state':'verified_no_value','meaning':result['meaning'],
            'record_path':str(result_path.resolve()),'record_sha256':digest(result_path),
            'evidence_path':str(evidence_path.resolve()),'evidence_sha256':digest(evidence_path),
            'excel':str(args.formal.resolve()),'excel_sha256':digest(args.formal),
            'isolated_excel':str(submission.resolve()),'isolated_excel_sha256':digest(submission),
            'excel_readback_empty':True,'quality':'passed','execution':'succeeded','format':'valid'})
    atomic_json(output/'verified-states.json',entries)
    print(json.dumps({'output':str(output.resolve()),'states':entries},ensure_ascii=False))


if __name__ == '__main__':
    main()
