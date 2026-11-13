# -*- coding: utf-8 -*-
"""人工确认的来源绑定、可追溯修订及解题计划约束。"""
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

KINDS = {'table_selection', 'block_relation', 'field', 'dimensions', 'unit', 'empty_semantics', 'question_intent'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def question_digest(question):
    value = json.dumps(question, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_table_evidence(path, table_id):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('contract') != 'docparser-facts-v2':
        if data.get('table_id') != table_id:raise ValueError('候选表格与识别证据不一致')
        return data
    folder = Path(path).resolve().parent
    manifest = json.loads((folder/'parser-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('artifacts',{}).get('facts.json') != digest(path):
        raise ValueError('解析事实与缓存指纹不匹配')
    if table_id == 'D0':
        from table_pipeline import context_fragments
        context = context_fragments([item for item in data['context'] if not item['table_ids']])
        return {'table_id':'D0','table':{'row_count':0,'col_count':0,'cells':[],'structure_confidence':None},
                'context':context,'parser_facts':True,'document_sha256':data['document_sha256']}
    tables = [item for item in data['tables'] if item.get('table_id') == table_id]
    if len(tables)!=1:raise ValueError('解析事实中找不到唯一目标表')
    from table_pipeline import context_fragments
    context = context_fragments([item for item in data['context'] if not item['table_ids'] or table_id in item['table_ids']])
    return {'table_id':table_id,'table':tables[0],'context':context,'parser_facts':True}


def issue_from_result(result_path, question, files_dir, recognition_result=None):
    path = Path(result_path).resolve()
    result = json.loads(path.read_text(encoding='utf-8'))
    if str(result.get('id')) != str(question['id']) or result.get('file_name') != question['file_name']:
        raise ValueError('异常记录与题目不一致')
    document = (Path(files_dir) / question['file_name']).resolve()
    if not document.is_relative_to(Path(files_dir).resolve()):
        raise ValueError('原文件路径越界')
    candidates = []
    folder = Path(result['recognition_artifact']) if result.get('recognition_artifact') else None
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8')) if folder and (folder / 'manifest.json').exists() else {}
    tables = {table['table_id']: table for table in manifest.get('tables', [])}
    for item in result.get('candidate_runs', []):
        child = item.get('record', {})
        region = tables.get(item['table_id'], {})
        crops = [str(folder / f"{item['table_id']}-part-{index}-normal.png") for index, _ in enumerate(region.get('parts', []))
                 if (folder / f"{item['table_id']}-part-{index}-normal.png").exists()] if folder else []
        candidates.append({'table_id': item['table_id'], 'answer': item.get('answer', ''),
                           'evidence': child.get('answer_evidence'), 'status': child.get('quality_status'),
                           'title': region.get('title', ''), 'source_parts': region.get('parts', []), 'crops': crops,
                           'unresolved_fields': [field for round_ in child.get('quality_rounds', [])
                                                 for field in round_.get('fields', [])
                                                 if field.get('route') not in ('accepted','accepted_by_average')]})
    if not candidates and result.get('answer_evidence'):
        source = Path(result['answer_evidence'])
        if source.is_file():
            data = json.loads(source.read_text(encoding='utf-8'))
            table_id = data.get('table_id')
            if table_id and data.get('table'):
                region = tables.get(table_id, {})
                crops = [str(folder / f'{table_id}-part-{index}-normal.png') for index, _ in enumerate(region.get('parts', []))
                         if (folder / f'{table_id}-part-{index}-normal.png').exists()] if folder else []
                candidates.append({'table_id': table_id, 'answer': result.get('answer', ''), 'evidence': str(source.resolve()),
                                   'status': result.get('quality_status'), 'title': region.get('title', ''),
                                   'source_parts': region.get('parts', []), 'crops': crops})
    if folder and (folder/'parser-manifest.json').is_file() and (folder/'facts.json').is_file():
        facts = json.loads((folder/'facts.json').read_text(encoding='utf-8'))
        ids = result.get('selection',{}).get('candidates') or [t['table_id'] for t in facts['tables']]
        if not facts['tables'] or 'D0' in ids:
            read_table_evidence(folder/'facts.json','D0')
            existing = next((item for item in candidates if item['table_id']=='D0'),None)
            if existing:
                existing.update(evidence=str((folder/'facts.json').resolve()),title='文档表外内容',
                                source_parts=[x['source'] for x in facts['context']])
            else:
                candidates.append({'table_id':'D0','answer':'','evidence':str((folder/'facts.json').resolve()),
                    'status':'unconfirmed','title':'文档表外内容','source_parts':[x['source'] for x in facts['context']], 'crops':[]})
        for table in facts['tables']:
            if table['table_id'] not in ids:continue
            read_table_evidence(folder/'facts.json',table['table_id'])
            existing = next((item for item in candidates if item['table_id']==table['table_id']),None)
            if existing and not existing.get('evidence'):
                existing.update(evidence=str((folder/'facts.json').resolve()),title=table.get('title',''),source_parts=[table['source']])
            elif not existing:
                candidates.append({'table_id':table['table_id'],'answer':'','evidence':str((folder/'facts.json').resolve()),
                    'status':'unconfirmed','title':table.get('title',''),'source_parts':[table['source']],'crops':[]})
    fields = [field for round_ in result.get('quality_rounds', []) for field in round_.get('fields', [])
              if field.get('route') not in ('accepted','accepted_by_average')]
    for sub in result.get('cross_fields',[]) + result.get('cross_field_checks',[]):
        fields.extend(dict(field,table_id=sub.get('table_id')) for round_ in sub.get('quality_rounds',[])
                      for field in round_.get('fields',[]) if field.get('route') not in ('accepted','accepted_by_average'))
    dependency_error = None
    if result.get('execution_status') == 'unresolved' and result.get('answer_evidence'):
        evidence_path = Path(result['answer_evidence'])
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
            if all(key in evidence for key in ('table','context','plan')):
                from quality_routing import dependencies
                from table_pipeline import number, normalize
                try:
                    targets = dependencies(evidence['table'],evidence['context'],evidence['plan'],question['question_type'],number,normalize)
                    fields.extend({'id':key,'state':'unknown','source':item.get('source'),
                                   'reason':'质检后程序执行仍依赖无法辨认的内容'}
                                  for key,item in targets.items() if item.get('state') == 'unknown')
                except (ValueError,KeyError,TypeError) as error:
                    dependency_error = str(error)
    issue = {'version': 1, 'scope': 'evaluation_only', 'question': question,
            'question_sha256': question_digest(question), 'document': str(document), 'document_sha256': digest(document),
            'original_result': str(path), 'original_result_sha256': digest(path), 'original_revision': result.get('revision'),
            'recognition_revision': result.get('recognition_revision'), 'recognition_artifact': result.get('recognition_artifact'),
            'issue_type': result.get('failure_category') or 'unresolved', 'reason': result.get('unanswered_reason') or result.get('error') or '',
            'original_judgment': result.get('selection'), 'candidates': candidates, 'fields': fields,
            'layout_blocks': result.get('layout_blocks', []), 'current_answer': result.get('answer', ''),
            'quality_status':result.get('quality_status'),'execution_status':result.get('execution_status'),
            'format_status':result.get('format_status'),'local_reinspection_rounds':result.get('local_reinspection_rounds',[]),
            'local_reinspection_stop':result.get('local_reinspection_stop'),'fallback':result.get('fallback'),
            'dependency_error':dependency_error}
    if recognition_result:
        if candidates:
            raise ValueError('原结果已有识别证据，无需另指定识别结果')
        supplemental = issue_from_result(recognition_result, question, files_dir)
        if not supplemental['candidates'] or not supplemental['recognition_artifact'] or not supplemental['recognition_revision']:
            raise ValueError('补充结果缺少可核验的识别证据')
        expected = hashlib.sha256(document.read_bytes() + supplemental['recognition_revision'].encode()).hexdigest()
        if Path(supplemental['recognition_artifact']).name != expected:
            raise ValueError('补充识别结果与原文件或识别版本不匹配')
        issue.update(candidates=supplemental['candidates'], recognition_artifact=supplemental['recognition_artifact'],
                     recognition_revision=supplemental['recognition_revision'],
                     recognition_result=supplemental['original_result'],
                     recognition_result_sha256=supplemental['original_result_sha256'])
    return issue


def confirmation_from_issue(issue_path, kind, payload, basis):
    issue_path = Path(issue_path).resolve()
    issue = json.loads(issue_path.read_text(encoding='utf-8'))
    if kind not in KINDS or not isinstance(payload, dict) or not basis.strip():
        raise ValueError('确认类型、修订内容和依据不能为空')
    if not isinstance(payload.get('table_id'), str) or not payload['table_id']:
        raise ValueError('确认必须指定受影响的表格')
    candidate = next((x for x in issue['candidates'] if x['table_id'] == payload['table_id']), None)
    if not candidate or not candidate.get('evidence'):
        raise ValueError('确认表格不在已有候选或缺少识别证据')
    evidence_path = Path(candidate['evidence']).resolve()
    read_table_evidence(evidence_path,payload['table_id'])
    result = {'version': 1, 'scope': 'evaluation_only', 'confirmed': True, 'source_type': 'authorized',
            'kind': kind, 'payload': payload, 'basis': basis, 'issue': str(issue_path), 'issue_sha256': digest(issue_path),
            'question': issue['question'], 'question_sha256': issue['question_sha256'], 'document': issue['document'],
            'document_sha256': issue['document_sha256'], 'original_result': issue['original_result'],
            'original_result_sha256': issue['original_result_sha256'], 'original_revision': issue['original_revision'],
            'recognition_revision': issue['recognition_revision'], 'recognition_artifact': issue['recognition_artifact'],
            'original_judgment': issue['original_judgment'], 'original_answer': issue['current_answer'],
            'table_evidence': str(evidence_path), 'table_evidence_sha256': digest(evidence_path)}
    if issue.get('recognition_result'):
        result.update(recognition_result=issue['recognition_result'], recognition_result_sha256=issue['recognition_result_sha256'])
    return result


def validate_confirmation(confirmation, question, document):
    if confirmation.get('scope') != 'evaluation_only' or confirmation.get('source_type') != 'authorized' or confirmation.get('confirmed') is not True:
        raise ValueError('人工确认只能用于明确授权的评测场景')
    if confirmation.get('kind') not in KINDS or confirmation.get('question_sha256') != question_digest(question):
        raise ValueError('人工确认与当前题目不一致')
    if Path(confirmation['document']).resolve() != Path(document).resolve() or digest(document) != confirmation.get('document_sha256'):
        raise ValueError('人工确认与当前原文件不一致')
    for field, hash_field in (('issue', 'issue_sha256'), ('original_result', 'original_result_sha256'), ('table_evidence', 'table_evidence_sha256')):
        if digest(confirmation[field]) != confirmation[hash_field]:
            raise ValueError(f'人工确认的来源记录已变化：{field}')
    source_path = confirmation.get('recognition_result', confirmation['original_result'])
    if confirmation.get('recognition_result') and digest(source_path) != confirmation.get('recognition_result_sha256'):
        raise ValueError('补充识别结果已变化')
    original = json.loads(Path(source_path).read_text(encoding='utf-8'))
    if str(original.get('id')) != str(question['id']) or original.get('file_name') != question['file_name']:
        raise ValueError('识别结果与当前题目不一致')
    expected = hashlib.sha256(Path(document).read_bytes() + confirmation['recognition_revision'].encode()).hexdigest()
    if Path(confirmation['recognition_artifact']).name != expected or original['recognition_artifact'] != confirmation['recognition_artifact']:
        raise ValueError('人工确认与历史识别来源不一致')
    evidence = read_table_evidence(confirmation['table_evidence'],confirmation['payload']['table_id'])
    if evidence.get('parser_facts'):
        source_hash = evidence.get('document_sha256') if evidence.get('table_id') == 'D0' else evidence['table']['source'].get('document_sha256')
        if source_hash != digest(document):raise ValueError('解析事实与确认的原文件不一致')
    return evidence


def apply_confirmation(confirmation, table, context, region, pipeline):
    from quality_routing import cell_at
    table, context, region = deepcopy(table), deepcopy(context), deepcopy(region)
    kind, payload = confirmation['kind'], confirmation['payload']
    if region['table_id'] != payload['table_id']:
        raise ValueError('确认的表格与当前表格不一致')
    zero_refs, needs_recognition = [], False
    if kind == 'field':
        cell = cell_at(table, payload['ref'])
        if {'text': cell['text'], 'state': cell['state']} != payload['original']:
            raise ValueError('字段原判断已变化')
        value = payload['replacement']
        if value['state'] not in ('value', 'empty', 'unknown') or (value['state'] == 'value') != bool(value['text']):
            raise ValueError('字段修订状态与文字不一致')
        cell.update(text=value['text'], state=value['state'])
    elif kind == 'dimensions':
        if [table['row_count'], table['col_count']] != payload['original']:
            raise ValueError('原行列数已变化')
        table['row_count'], table['col_count'] = payload['replacement']
        pipeline.validate_table(table, region)
    elif kind == 'unit':
        items = [x for x in context if x['id'] == payload['context_id'] and x['kind'] == 'unit']
        if len(items) != 1 or items[0]['text'] != payload['original'] or not payload['replacement']:
            raise ValueError('单位原判断不匹配或修订为空')
        items[0].update(text=payload['replacement'], state='value')
    elif kind == 'empty_semantics':
        zero_refs = payload['refs']
        if payload.get('meaning') != 'zero_in_sum' or not zero_refs or any(cell_at(table, ref)['state'] != 'empty' for ref in zero_refs):
            raise ValueError('只能确认真实空白在求和中按零，unknown不得按零')
    elif kind == 'block_relation':
        indices = payload['parts']
        if (payload.get('relation') not in ('same_table', 'independent') or not indices
                or any(type(index) is not int or not 0 <= index < len(region['parts']) for index in indices)
                or indices != sorted(set(indices))):
            raise ValueError('确认的表块范围不合法')
        if payload['relation'] == 'independent' and len(indices) != 1:
            raise ValueError('独立表块不能直接复用多个片段的识别结果')
        if indices != list(range(len(region['parts']))):
            region['parts'] = [region['parts'][index] for index in indices]
            region['table_id'] += '-confirmed-' + '-'.join(map(str, indices))
            needs_recognition = True
    elif kind == 'question_intent':
        if not payload.get('constraints'):
            raise ValueError('题意确认需要可检查的计划约束')
    return table, context, region, zero_refs, needs_recognition


def _count(text):
    if text.isdigit(): return int(text)
    return {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}[text]


def plan_conflicts(confirmation, question, table, context, plan):
    from quality_routing import dependencies, cell_at, cell_id
    errors = []
    if plan.get('kind') == 'unknown': return ['新计划未能理解题目']
    payload, kind = confirmation['payload'], confirmation['kind']
    if question['question_type'] == 'structure' and plan.get('kind') == 'structure':
        for label, key in (('行', 'rows'), ('列', 'cols')):
            match = re.search(r'(?:前\s*([0-9一二三四五六七八九十]+)|第\s*([一1]))\s*'+label, question['question'])
            if match:
                expected = list(range(_count(match.group(1) or match.group(2))))
                if plan.get(key) != expected or plan.get('all'): errors.append(f'{label}范围未落实题目要求：{expected}')
    constraints = payload.get('constraints', {})
    for key, value in constraints.items():
        if plan.get(key) != value: errors.append(f'计划字段 {key} 未落实人工确认')
    try:
        targets = dependencies(table, context, plan, question['question_type'])
        if kind == 'field' and cell_id(cell_at(table, payload['ref'])) not in targets:
            errors.append('计划未引用人工修订的关键字段')
        if kind == 'unit' and payload['context_id'] not in plan.get('context_ids', []):
            errors.append('计划未引用人工确认的单位')
        if kind == 'empty_semantics':
            def sum_refs(expr):
                if expr.get('op') == 'sum':
                    return [arg.get('ref') for arg in expr.get('args', []) if arg.get('op') == 'cell']
                return sum((sum_refs(arg) for arg in expr.get('args', [])), [])
            refs = sum((sum_refs(e) for e in plan.get('expressions', [])), [])
            if any(ref not in refs for ref in payload['refs']): errors.append('求和计划遗漏人工确认的空白字段')
    except (ValueError, KeyError, TypeError) as error:
        errors.append('计划依赖校验失败：' + str(error))
    return errors
