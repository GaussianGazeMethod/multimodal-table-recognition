#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅管理可人工标注的评测案例；解题程序不读取这些参考答案。"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from evaluate_pipeline import score
import table_solver as base
from human_confirmation import issue_from_result, confirmation_from_issue, KINDS


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def record(args):
    original_path = args.result.resolve()
    original = read(original_path)
    if str(original['id']) != args.id:
        raise ValueError('题目 ID 与原始运行记录不一致')
    if not args.evidence.strip() or not args.confirm:
        raise ValueError('必须提供核对依据并明确使用 --confirm 确认参考答案')
    questions = base.read_questions(args.tests)
    if args.tests.resolve() == base.TESTS_PATH.resolve():
        questions, _ = base.apply_corrections(questions)
    found = [q for q in questions if str(q['id']) == args.id]
    if len(found) != 1 or found[0]['file_name'] != original['file_name']:
        raise ValueError('题目清单与原始运行记录不一致')
    document = (args.files_dir / found[0]['file_name']).resolve()
    if not document.is_relative_to(args.files_dir.resolve()):
        raise ValueError('来源文件路径超出表格目录')
    reference = json.loads(args.answer_json)
    if not isinstance(reference, (str, int, float, list, dict)) or isinstance(reference, bool):
        raise ValueError('参考答案必须为字符串、数字、数组或结构对象')
    base.format_answer(json.dumps({'answer': reference}, ensure_ascii=False, allow_nan=False),
                       found[0]['question_type'], found[0].get('answer_format'))
    entry = {'id': args.id, 'question_type': original['question_type'], 'reference': reference,
             'source_type': args.source_type, 'evidence': args.evidence, 'confirmed_at': datetime.now(timezone.utc).isoformat(),
             'original_result_path': str(original_path), 'original_result_sha256': digest(original_path),
             'original_result': original, 'question': found[0], 'document_sha256': digest(document),
             'tests_sha256': digest(args.tests), 'issue_type': args.issue_type, 'reference_status': 'verified_synthetic' if args.source_type == 'synthetic' else 'human_confirmed'}
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    with args.ledger.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
    print('已追加确认记录；原始结果和修订依据均已保留')


def review_issue(args):
    result = read(args.result)
    questions = base.read_questions(args.tests)
    if args.tests.resolve() == base.TESTS_PATH.resolve():
        questions, _ = base.apply_corrections(questions)
    found = [q for q in questions if str(q['id']) == str(result['id'])]
    if len(found) != 1:
        raise ValueError('异常结果对应题目不唯一')
    write_new(args.output, issue_from_result(args.result, found[0], args.files_dir, args.recognition_result))
    print(f'已保存待确认疑点：{args.output}')


def confirm_issue(args):
    if not args.confirm or args.source_type != 'authorized':
        raise ValueError('必须明确确认且数据须允许人工标注')
    entry = confirmation_from_issue(args.issue, args.kind, json.loads(args.payload_json), args.evidence)
    entry['confirmed_at'] = datetime.now(timezone.utc).isoformat()
    write_new(args.output, entry)
    print(f'已保存人工确认：{args.output}')


def freeze_confirmations(args):
    cases = []
    for path in args.confirmations:
        path = path.resolve()
        item = read(path)
        if item.get('scope') != 'evaluation_only' or item.get('source_type') != 'authorized' or not item.get('confirmed'):
            raise ValueError('只能冻结允许人工标注且已确认的案例')
        cases.append({'id': str(item['question']['id']), 'confirmation': str(path), 'confirmation_sha256': digest(path),
                      'question_sha256': item['question_sha256'], 'document_sha256': item['document_sha256'],
                      'kind': item['kind'], 'payload': item['payload']})
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('同一评测版本不能包含重复题目')
    write_new(args.output, {'version': args.version, 'scope': 'confirmation_workflow',
                            'created_at': datetime.now(timezone.utc).isoformat(), 'cases': cases,
                            'note': '仅验证确认是否落实及流程状态；不含人工核实的最终答案，不计算正确率。'})
    print(f'已冻结确认流程评测版本 {args.version}：{args.output}')


def report_confirmations(args):
    dataset = read(args.dataset)
    if dataset.get('scope') != 'confirmation_workflow':
        raise ValueError('评测版本不是人工确认流程集')
    records = {}
    for run in args.runs:
        for path in run.glob('result-*.json'):
            item = read(path)
            if str(item['id']) in records:
                raise ValueError('评测运行存在重复题目')
            records[str(item['id'])] = (item, path)
    cases = []
    for case in dataset['cases']:
        pair = records.get(case['id'])
        if pair is None:
            cases.append({'id': case['id'], 'status': 'not_run'})
            continue
        item, path = pair
        if not item.get('evaluation_only') or item.get('confirmation_sha256') != case['confirmation_sha256']:
            raise ValueError('评测结果与冻结确认不一致')
        cases.append({'id': case['id'], 'status': item['status'], 'selected_table': item.get('selection', {}).get('table_id'),
                      'expected_table': case['payload']['table_id'], 'confirmation_check': item.get('confirmation_check'),
                      'recognition_reused': item.get('recognition_reused'), 'quality': item.get('quality_status'),
                      'excel': item.get('excel_status'), 'model_calls': item.get('pipeline_calls'), 'record': str(path.resolve())})
    write_new(args.output, {'dataset_version': dataset['version'], 'dataset_sha256': digest(args.dataset), 'cases': cases,
                            'note': '仅报告确认落实、质检和写入状态；不代表答案正确率。'})
    print(f'已生成确认流程回归报告：{args.output}')


def run_confirmations(args):
    dataset = read(args.dataset)
    if dataset.get('scope') != 'confirmation_workflow' or not dataset.get('cases'):
        raise ValueError('需要非空的人工确认流程评测集')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    for case in dataset['cases']:
        confirmation = Path(case['confirmation'])
        if digest(confirmation) != case['confirmation_sha256']:
            raise ValueError('冻结后的人工确认文件发生变化')
        before = {p for p in args.output_dir.iterdir() if p.is_dir()}
        command = [sys.executable, str(Path(__file__).resolve().parent.parent / 'run.py'), '--tests', str(args.tests),
                   '--files-dir', str(args.files_dir), '--output-dir', str(args.output_dir), '--ids', case['id'],
                   '--confirmed-evaluation', str(confirmation)]
        if args.cache_dir:
            command += ['--cache-dir', str(args.cache_dir)]
        completed = subprocess.run(command, check=False)
        created = [p for p in args.output_dir.iterdir() if p.is_dir() and p not in before]
        if len(created) != 1:
            raise ValueError('无法唯一确定确认回归运行目录')
        write_new(created[0] / 'confirmation-dataset.json', {'dataset_sha256': digest(args.dataset),
                  'confirmation_sha256': case['confirmation_sha256'], 'returncode': completed.returncode})
        if completed.returncode:
            failures.append(case['id'])
    if failures:
        raise ValueError('部分确认回归失败，已保留记录：' + '、'.join(failures))


def freeze(args):
    entries = [json.loads(line) for line in args.ledger.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not entries:
        raise ValueError('评测记录为空')
    latest = {entry['id']: entry for entry in entries}
    data = {'version': args.version, 'ledger_path': str(args.ledger.resolve()), 'ledger_sha256': digest(args.ledger),
            'created_at': datetime.now(timezone.utc).isoformat(), 'history': entries, 'cases': list(latest.values())}
    write_new(args.output, data)
    print(f"已冻结版本 {args.version}，案例 {len(latest)} 个：{args.output}")


def report(args):
    dataset = read(args.dataset)
    records = {}
    for path in sorted(args.run.glob('result-*.json')):
        item = read(path)
        if str(item['id']) in records:
            raise ValueError('运行记录存在重复题目 ID')
        records[str(item['id'])] = (item, path)
    cases = []
    for case in dataset['cases']:
        pair = records.get(case['id'])
        item = pair[0] if pair else None
        if pair:
            manifest_path = args.run / 'evaluation-input.json'
            same_original = digest(pair[1]) == case['original_result_sha256']
            verified_run = manifest_path.exists() and read(manifest_path).get('dataset_sha256') == digest(args.dataset)
            if not same_original and not verified_run:
                raise ValueError('该运行未验证评测集输入一致性，不能计算参考答案匹配率')
        status = item.get('status', 'unknown') if item else 'not_run'
        answer = item.get('answer', '') if item else ''
        comparable = item is not None and status not in ('error', 'not_run')
        matches = score(answer, {'gold': case['reference'], 'question_type': case['question_type']}) if comparable else None
        rounds = item.get('quality_rounds', []) if item else []
        average = rounds[-1].get('average_confidence', {}) if rounds else {}
        fallback = item.get('fallback', {}).get('decision', {}) if item else {}
        raw_confidence = fallback.get('confidence') if fallback else average.get('average')
        try:
            confidence = float(raw_confidence)
            if not 0 <= confidence <= 1: confidence = None
        except (TypeError, ValueError):
            confidence = None
        confidence_basis = ('fallback_selected' if fallback else 'question_average') if confidence is not None else 'unknown'
        cases.append({'id': case['id'], 'question_type': case['question_type'], 'status': status, 'answer': answer,
                      'reference': case['reference'], 'matches': matches, 'confidence': confidence, 'confidence_basis': confidence_basis,
                      'error_type': item.get('error_type') if item else None, 'issue_type': case.get('issue_type', 'unclassified'),
                      'reason': (item.get('unanswered_reason') or item.get('error')) if item else '未运行',
                      'revision': item.get('revision') if item else None})
    def metrics(items):
        compared = [x for x in items if x['matches'] is not None]
        correct = sum(x['matches'] is True for x in compared)
        return {'total': len(items), 'compared': len(compared), 'correct': correct,
                'accuracy': correct / len(compared) if compared else None,
                'correct_over_total': correct / len(items) if items else None,
                'coverage': len(compared) / len(items) if items else None,
                'unanswered': sum(x['status'] == 'unanswered' for x in items),
                'errors': sum(x['status'] == 'error' for x in items),
                'not_run': sum(x['status'] == 'not_run' for x in items)}
    bands = {'below_0.70': [], '0.70_to_0.90': [], 'above_0.90': [], 'missing': []}
    for case in cases:
        value = case['confidence']
        key = 'missing' if value is None else 'below_0.70' if value < .7 else '0.70_to_0.90' if value <= .9 else 'above_0.90'
        bands[key].append(case)
    result = {'dataset_version': dataset['version'], 'dataset_sha256': digest(args.dataset),
              'run': str(args.run.resolve()), 'overall': metrics(cases),
              'by_type': {kind: metrics([c for c in cases if c['question_type'] == kind]) for kind in ('structure','extract','thinking')},
              'confidence_bands_question_level': {k: metrics(v) for k,v in bands.items()},
              'confidence_bands_by_basis': {basis:{k:metrics([c for c in v if c['confidence_basis']==basis]) for k,v in bands.items()}
                                            for basis in ('question_average','fallback_selected','unknown')},
              'failure_types': {kind: sum(c['error_type'] == kind for c in cases) for kind in sorted({c['error_type'] for c in cases if c['error_type']})},
              'mismatched': sum(c['matches'] is False for c in cases),
              'mismatch_by_issue': {k: sum(c['matches'] is False and c['issue_type'] == k for c in cases)
                                    for k in sorted({c['issue_type'] for c in cases})}, 'cases': cases,
              'confidence_note': '按最终字段与结构平均置信度或兜底选中置信度分组；未记录则为未知。两种依据须分别解读，题级匹配率不等于字段级校准，不自动调整阈值。',
              'metric_note': 'accuracy以可比较题数为分母；correct_over_total以评测集全部题数为分母，未运行和接口错误单独列出，不能视为语义答错。'}
    if args.compare:
        previous = read(args.compare)
        if previous['dataset_sha256'] != result['dataset_sha256']:
            raise ValueError('前后对比必须使用同一个冻结评测集')
        result['comparison'] = {'previous_report': str(args.compare.resolve()), 'previous': previous['overall'], 'current': result['overall']}
    write_new(args.output, result)
    print(json.dumps(result['overall'], ensure_ascii=False))


def run(args):
    dataset = read(args.dataset)
    ids = [c['id'] for c in dataset['cases']]
    if not ids or any(c['source_type'] not in ('synthetic', 'authorized') for c in dataset['cases']):
        raise ValueError('评测集必须包含明确许可的案例')
    questions = base.read_questions(args.tests)
    if args.tests.resolve() == base.TESTS_PATH.resolve():
        questions, _ = base.apply_corrections(questions)
    current = {str(q['id']): q for q in questions}
    for case in dataset['cases']:
        if current.get(case['id']) != case['question']:
            raise ValueError('题目已变化，不能沿用旧参考答案')
        document = (args.files_dir / case['question']['file_name']).resolve()
        if not document.is_relative_to(args.files_dir.resolve()) or digest(document) != case['document_sha256']:
            raise ValueError('来源文档已变化，不能沿用旧参考答案')
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / 'run.py'), '--tests', str(args.tests),
               '--files-dir', str(args.files_dir), '--output-dir', str(args.output_dir), '--ids', *ids]
    if args.cache_dir:
        command += ['--cache-dir', str(args.cache_dir)]
    if args.fresh:
        command.append('--fresh')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    before = {p for p in args.output_dir.iterdir() if p.is_dir()}
    completed = subprocess.run(command, check=False)
    new = [p for p in args.output_dir.iterdir() if p.is_dir() and p not in before]
    if len(new) != 1:
        raise ValueError('不能唯一确定评测运行目录')
    write_new(new[0] / 'evaluation-input.json', {'dataset_sha256': digest(args.dataset),
              'tests_sha256': digest(args.tests), 'documents': {c['id']: c['document_sha256'] for c in dataset['cases']},
              'returncode': completed.returncode})
    print(f'评测运行目录：{new[0]}')
    if completed.returncode:
        raise ValueError('部分题目运行失败，已保留记录，可继续生成诊断报告')


def main():
    parser = argparse.ArgumentParser(description='人工确认、冻结评测集、运行和比较；不参与自动答案生成')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('review-issue', help='从已有运行结果生成定向人工核查事项')
    p.add_argument('--result', type=Path, required=True); p.add_argument('--tests', type=Path, required=True)
    p.add_argument('--recognition-result', type=Path, help='早期失败无识别事实时，关联同题同文件的后续识别结果')
    p.add_argument('--files-dir', type=Path, required=True); p.add_argument('--output', type=Path, required=True); p.set_defaults(func=review_issue)
    p = sub.add_parser('confirm-issue', help='对允许标注的数据保存带来源的人工确认')
    p.add_argument('--issue', type=Path, required=True); p.add_argument('--kind', choices=sorted(KINDS), required=True)
    p.add_argument('--payload-json', required=True); p.add_argument('--evidence', required=True)
    p.add_argument('--source-type', choices=['authorized'], required=True); p.add_argument('--confirm', action='store_true')
    p.add_argument('--output', type=Path, required=True); p.set_defaults(func=confirm_issue)
    p = sub.add_parser('freeze-confirmations', help='冻结确认流程回归案例，不创建参考答案')
    p.add_argument('--confirmations', type=Path, nargs='+', required=True); p.add_argument('--version', required=True)
    p.add_argument('--output', type=Path, required=True); p.set_defaults(func=freeze_confirmations)
    p = sub.add_parser('report-confirmations', help='比较冻结确认与评测运行状态')
    p.add_argument('--dataset', type=Path, required=True); p.add_argument('--runs', type=Path, nargs='+', required=True)
    p.add_argument('--output', type=Path, required=True); p.set_defaults(func=report_confirmations)
    p = sub.add_parser('run-confirmations', help='重复运行冻结的人工确认流程集')
    p.add_argument('--dataset', type=Path, required=True); p.add_argument('--tests', type=Path, required=True)
    p.add_argument('--files-dir', type=Path, required=True); p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--cache-dir', type=Path); p.set_defaults(func=run_confirmations)
    p = sub.add_parser('record', help='追加人工确认的参考答案与原始结果')
    p.add_argument('--ledger', type=Path, required=True); p.add_argument('--result', type=Path, required=True)
    p.add_argument('--id', required=True); p.add_argument('--answer-json', required=True); p.add_argument('--evidence', required=True)
    p.add_argument('--tests', type=Path, required=True); p.add_argument('--files-dir', type=Path, required=True)
    p.add_argument('--issue-type', choices=['unclassified','image_quality','table_location','text_recognition','structure','field_mapping','unit','calculation','answer_format'], default='unclassified')
    p.add_argument('--source-type', choices=['synthetic','authorized'], required=True); p.add_argument('--confirm', action='store_true'); p.set_defaults(func=record)
    p = sub.add_parser('freeze', help='冻结不可覆盖的评测集版本')
    p.add_argument('--ledger', type=Path, required=True); p.add_argument('--version', required=True); p.add_argument('--output', type=Path, required=True); p.set_defaults(func=freeze)
    p = sub.add_parser('run', help='用指定题目清单重新执行冻结案例，参考答案不会传给解题程序')
    p.add_argument('--dataset', type=Path, required=True); p.add_argument('--tests', type=Path, required=True)
    p.add_argument('--files-dir', type=Path, required=True); p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--cache-dir', type=Path); p.add_argument('--fresh', action='store_true'); p.set_defaults(func=run)
    p = sub.add_parser('report', help='对已保存结果与冻结参考答案做比较')
    p.add_argument('--dataset', type=Path, required=True); p.add_argument('--run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True); p.add_argument('--compare', type=Path); p.set_defaults(func=report)
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f'错误：{error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
