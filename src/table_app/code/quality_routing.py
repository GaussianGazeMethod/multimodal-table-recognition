# -*- coding: utf-8 -*-
"""按题目依赖挑选关键字段，置信度与确定性规则共同决定复核范围。"""
from decimal import Decimal, InvalidOperation
from collections import Counter
import json
import unicodedata
import re


def average_confidence_route(checks, structure_confidence, threshold='0.90'):
    """按当前题验证字段及结构分数计算平均值，缺失或非法时不通过。"""
    scores = [{'field': key, 'score': check.get('confidence')} for key, check in sorted(checks.items())]
    if not scores:
        return {'status': 'missing', 'average': None, 'scores': [], 'threshold': str(threshold)}
    scores.append({'field': 'structure', 'score': structure_confidence})
    try:
        values = [Decimal(str(item['score'])) for item in scores]
        if any(not value.is_finite() or value < 0 or value > 1 for value in values):
            raise ValueError('置信度超出范围')
    except (InvalidOperation, ValueError, TypeError):
        return {'status': 'missing', 'average': None, 'scores': scores, 'threshold': str(threshold)}
    average = sum(values) / Decimal(len(values))
    return {'status': 'accepted' if average > Decimal(str(threshold)) else 'reinspect',
            'average': str(average), 'scores': scores, 'threshold': str(threshold)}


def fallback_answer_key(answer):
    """只归一化无关空白及 JSON 对象键序，不推断数值或改写文本含义。"""
    if isinstance(answer, str):
        return unicodedata.normalize('NFC', re.sub(r'\s+', ' ', answer).strip())
    if isinstance(answer, list):
        return [fallback_answer_key(value) for value in answer]
    if isinstance(answer, dict):
        return {key: fallback_answer_key(answer[key]) for key in sorted(answer)}
    return answer


def choose_fallback_sample(samples, threshold='0.90'):
    """三次独立候选先按答案多数，再在选定范围中取最高置信度。"""
    if len(samples) != 3:
        raise ValueError('兜底必须有三次独立采样结果')
    keys=[]
    for item in samples:
        if not isinstance(item, dict) or set(item) != {'answer','confidence','reason'}:
            raise ValueError('兜底采样不符合输出契约')
        try:
            confidence = Decimal(str(item['confidence']))
            if not confidence.is_finite() or not 0 <= confidence <= 1:
                raise ValueError('兜底置信度不在合法范围')
        except (InvalidOperation, TypeError):
            raise ValueError('兜底置信度无效') from None
        if not isinstance(item['reason'], str) or not item['reason'].strip():
            raise ValueError('兜底必须记录依据')
        keys.append(json.dumps(fallback_answer_key(item['answer']), ensure_ascii=False,
                               sort_keys=True, separators=(',', ':'), allow_nan=False))
    counts=Counter(keys)
    majority=next((key for key,count in counts.items() if count>=2),None)
    eligible=[i for i,key in enumerate(keys) if majority is None or key==majority]
    selected=max(eligible,key=lambda i:Decimal(str(samples[i]['confidence'])))
    confidence=Decimal(str(samples[selected]['confidence']))
    return {'selected_index':selected, 'method':'majority' if majority is not None else 'highest_confidence',
            'normalized_keys':keys, 'eligible_indexes':eligible, 'confidence':str(confidence),
            'threshold':str(threshold), 'status':'accepted' if confidence>Decimal(str(threshold)) else 'unresolved'}


def numeric_bounds(value, parse_number):
    """返回可确认的闭区间；破折号、余量或无法解析的值不代表零。"""
    text = value.strip().replace('−','-').replace('\\sim','~').replace('\\leq','≤').replace('\\le','≤').replace('\\geq','≥').replace('\\ge','≥')
    for mark in ('\\(', '\\)', '$'):
        text = text.replace(mark,'')
    scalar = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)'
    pair = re.fullmatch(r'\s*('+scalar+r')\s*(?:~|～|至|–|-)\s*('+scalar+r')\s*',text)
    bound = re.fullmatch(r'\s*(≤|≥)\s*('+scalar+r')\s*',text)
    if pair:
        low,high = [parse_number(x) for x in pair.groups()]
        if low>high: raise ValueError('数值区间上下限倒置')
    elif bound:
        v=parse_number(bound[2]); low,high=(Decimal('-Infinity'),v) if bound[1]=='≤' else (v,Decimal('Infinity'))
    else:
        try: low=high=parse_number(text)
        except (ValueError,ArithmeticError):
            raise ValueError('数值或区间不可确认，破折号和余量不能直接当作数值：'+value) from None
    return low,high


def numeric_comparison(value, other, op, parse_number):
    """区间所有可能值结论一致才比较；不把范围、未知或横线当单值。"""
    if op not in ('gt','ge','lt','le'): raise ValueError('不支持的数值比较')
    try: low,high=numeric_bounds(value,parse_number)
    except ValueError as error: raise ValueError('筛选字段不是可确认的数值或区间：'+value) from error
    right=parse_number(str(other))
    def compare(left): return {'gt':left>right,'ge':left>=right,'lt':left<right,'le':left<=right}[op]
    a,b=compare(low),compare(high)
    if a!=b: raise ValueError('数值区间跨越筛选阈值，不能唯一判定：'+value+' '+op+' '+str(other))
    return a


def cell_at(table, ref):
    if not isinstance(ref, list) or len(ref) != 2 or any(type(x) is not int for x in ref):
        raise ValueError('单元格引用必须是两个整数')
    row, col = ref
    for cell in table['cells']:
        if cell['row'] <= row < cell['row'] + cell['rowspan'] and cell['col'] <= col < cell['col'] + cell['colspan']:
            return cell
    raise ValueError('单元格引用超出已识别网格')


def cell_id(cell):
    return f"cell:{cell['row']}:{cell['col']}"


def multi_header_evidence(table, rows):
    """存在明确的父表头与两个子表头时，只返回证明多级的单元格。"""
    if not rows or rows != list(range(rows[0], rows[-1] + 1)):
        raise ValueError('表头行必须为非空且连续的逻辑行')
    if any(row >= table['row_count'] for row in rows):
        raise ValueError('表头行越界')
    if len(rows)==1:return []
    witnesses = []
    for parent in table['cells']:
        if parent['row'] not in rows[:-1] or parent['colspan'] < 2 or parent['state'] != 'value':
            continue
        child_row = parent['row'] + parent['rowspan']
        if child_row not in rows:
            continue
        children = [c for c in table['cells'] if c['row'] <= child_row < c['row'] + c['rowspan']
                    and c['col'] < parent['col'] + parent['colspan'] and c['col'] + c['colspan'] > parent['col']
                    and c is not parent and c['state'] == 'value']
        if len(children) >= 2:
            witnesses.append([parent, *children])
    if not witnesses:
        return []
    return max(witnesses, key=lambda cells: min(c['confidence'] for c in cells))


def structure_cells(table, plan):
    rows, cols = plan['rows'], plan['cols']
    if type(plan['all']) is not bool:
        raise ValueError('all必须为布尔值')
    for indices, size in ((rows, table['row_count']), (cols, table['col_count'])):
        if not isinstance(indices, list) or any(type(n) is not int or not 0 <= n < size for n in indices):
            raise ValueError('局部结构范围越界')
    if plan['all'] and (rows or cols):
        raise ValueError('整表恢复不能同时指定局部范围')
    cells = [c for c in table['cells'] if plan['all'] or
             any(c['row'] <= r < c['row'] + c['rowspan'] for r in rows) or
             any(c['col'] <= col < c['col'] + c['colspan'] for col in cols)]
    if not cells:
        raise ValueError('所需结构范围为空')
    return cells


def empty_sum_conflicts(table, plan, question):
    """显式多项相加时，不能把同一数值列中作为依据的空项悄悄省略。"""
    if plan.get('kind') != 'calculate' or not any(mark in question for mark in ('+', '＋')):
        return []
    refs = []
    def collect(expression, in_sum=False):
        in_sum = in_sum or expression.get('op') == 'sum'
        if in_sum and expression.get('op') == 'cell' and isinstance(expression.get('ref'), list):
            refs.append(expression['ref'])
        for arg in expression.get('args', []):
            collect(arg, in_sum)
    for expression in plan.get('expressions', []):
        collect(expression)
    columns = {ref[1] for ref in refs}
    if not columns:
        return []
    conflicts = []
    for ref in plan.get('evidence_refs', []):
        cell = cell_at(table, ref)
        if ref[1] in columns and cell['state'] in ('empty', 'unknown') and ref not in refs:
            conflicts.append(ref)
    terms = [re.sub(r'\s+', '', term) for term in re.split(r'[+＋]', question)]
    for label in table['cells']:
        text = re.sub(r'\s+', '', label['text'])
        if label['col'] >= min(columns) or label['state'] != 'value' or len(text) < 2:
            continue
        if not any(text in term for term in terms):
            continue
        for col in columns:
            ref = [label['row'], col]
            cell = cell_at(table, ref)
            if cell['state'] in ('empty', 'unknown') and ref not in refs and ref not in conflicts:
                conflicts.append(ref)
    return conflicts


def dependencies(table, context, plan, question_type, parse_number=None, normalize_value=None):
    """依赖从可执行计划推导，再加入模型声明的语义证据，不能只信关键字段名单。"""
    targets = {}
    context_by_id = {c['id']: c for c in context}

    def add(ref):
        if isinstance(ref, dict):
            key = ref['context_id']
            if key not in context_by_id:
                raise ValueError('计划引用不存在的表外内容')
            targets[key] = context_by_id[key]
        else:
            cell = cell_at(table, ref)
            targets[cell_id(cell)] = cell

    def expression(e, depth=0):
        if depth > 12:
            raise ValueError('表达式嵌套过深')
        if e['op'] in ('cell', 'text', 'substring', 'count_character', 'labeled_number', 'labeled_sequence_number', 'range_contains', 'text_equals'):
            add(e['ref'])
        elif e['op'] == 'time_diff':
            add(e['start']['ref'])
            add(e['end']['ref'])
        elif e['op'] == 'has_multi_header':
            witness = multi_header_evidence(table, e['rows'])
            if witness:
                for cell in witness:
                    add([cell['row'], cell['col']])
            else:
                # 无法找到正面结构证据时，完整核验表头才能判断是否为否。
                for row in e['rows']:
                    for col in range(table['col_count']):
                        add([row, col])
        elif e['op'] in ('language', 'text_contains', 'numeric_trend'):
            for ref in e['refs']:
                add(ref)
        elif e['op'] == 'join':
            for arg in e['args']:
                expression(arg, depth + 1)
        elif e['op'] == 'count_columns':
            for col in e['columns']:
                add([e['row'], col])
        elif e['op'] == 'count_equal':
            for ref in e['refs']: add(ref)
        elif e['op'] == 'argmax_counts':
            for group in e['groups']:
                add(group['label'])
                for ref in group['refs']: add(ref)
        elif e['op'] == 'select_column_labels':
            for col in e['columns']:
                add([e['row'],col]);add([e['header_row'],col])
        elif e['op'] in ('argmax', 'argmin'):
            if len(e['values']) != len(e['labels']):
                raise ValueError('比较数值与标签必须一一对应')
            for ref in e['values']:
                add(ref)
            possible_labels = e['labels']
            values = [cell_at(table, ref) for ref in e['values']]
            if parse_number is not None and all(cell['state'] == 'value' for cell in values):
                try:
                    numbers = [parse_number(cell['text']) for cell in values]
                    extreme = max(numbers) if e['op'] == 'argmax' else min(numbers)
                    possible_labels = [ref for ref, value in zip(e['labels'], numbers) if value == extreme]
                except (ValueError, ArithmeticError):
                    pass
            for ref in possible_labels:
                add(ref)
        elif e['op'] == 'query':
            candidates = []
            for row in e['rows']:
                possible = True
                filter_refs, excluding_ref = [], None
                for condition in e['filters']:
                    ref = [row, condition['column']]
                    filter_refs.append(ref)
                    cell = cell_at(table, ref)
                    if cell['state'] == 'unknown':
                        continue
                    value, other, cmp = cell['text'].strip(), condition['value'], condition['cmp']
                    if normalize_value is not None:
                        value = normalize_value(value)
                    try:
                        if cmp in ('eq', 'ne'):
                            matched = (value == str(other)) if cmp == 'eq' else (value != str(other))
                        elif parse_number is not None:
                            matched = numeric_comparison(value, other, cmp, parse_number)
                        else:
                            continue
                    except (ValueError, ArithmeticError):
                        continue
                    if not matched:
                        possible = False
                        excluding_ref = excluding_ref or ref
                if possible:
                    for ref in filter_refs:
                        add(ref)
                    candidates.append(row)
                else:
                    add(excluding_ref)
            # 计数仍依赖被计数的目标行，必须逐行核实其内容。
            columns = {e['column']}
            if e['sort'] is not None:
                columns.add(e['sort']['column'])
            for col in columns:
                if col >= table['col_count']:
                    raise ValueError('查询列越界')
                for row in candidates:
                    add([row, col])
        else:
            if e['op'] in ('difference', 'ratio', 'growth') and len(e['args']) != 2:
                raise ValueError('该运算必须有两个操作数')
            for arg in e['args']:
                expression(arg, depth + 1)

    kind = plan['kind']
    if kind == 'unknown':
        return targets
    if (kind == 'structure') != (question_type == 'structure'):
        raise ValueError('题型与计划不一致')
    if kind == 'structure':
        for cell in structure_cells(table, plan):
            add([cell['row'], cell['col']])
    elif kind == 'values':
        if not plan['as_array'] and len(plan['refs']) != 1:
            raise ValueError('单值计划只能有一个引用')
        for ref in plan['refs']:
            if isinstance(ref, dict) and 'missing_field' in ref:
                if not plan['as_array'] or not ref['missing_field'].strip():
                    raise ValueError('缺失字段只能用于多值数组且必须说明字段')
                continue
            add(ref)
    else:
        if not plan['as_array'] and len(plan['expressions']) != 1:
            raise ValueError('单值计划只能有一个表达式')
        for e in plan['expressions']:
            expression(e)
        try:
            if not Decimal(plan['scale']).is_finite():
                raise ValueError('单位换算倍数必须有限')
        except InvalidOperation:
            raise ValueError('单位换算倍数不是有效数字') from None
    for ref in plan['evidence_refs']:
        add(ref)
    for key in plan['context_ids']:
        add({'context_id': key})
    for rule in plan['rules']:
        for ref in [*rule['refs'], rule['total']]:
            add(ref)
    if not targets and not (kind == 'values' and plan['as_array'] and
                            all(isinstance(ref, dict) and ref.get('missing_field') for ref in plan['refs'])):
        raise ValueError('计划没有可执行的目标字段')
    return targets


def evaluate_rules(table, rules, number):
    results = []
    for rule in rules:
        cells = [cell_at(table, ref) for ref in rule['refs']]
        total = cell_at(table, rule['total'])
        ids = [cell_id(c) for c in [*cells, total]]
        if len(set(ids)) != len(ids) or not rule['reason'].strip():
            raise ValueError('合计校验必须有适用依据且不得重复计入同一合并单元格')
        state = 'not_applicable'
        if all(c['state'] == 'value' for c in [*cells, total]):
            try:
                state = 'passed' if sum((number(c['text']) for c in cells), Decimal(0)) == number(total['text']) else 'failed'
            except (ValueError, ArithmeticError):
                state = 'failed'
        results.append({'ids': ids, 'state': state, 'reason': rule['reason']})
    return results


def plain_math_text(text):
    """只还原已知数学排版包装和符号；不处理未知公式、不删下标或运算。"""
    symbols = {'alpha':'α','beta':'β','gamma':'γ','delta':'δ','sim':'~','le':'≤','leq':'≤','ge':'≥','geq':'≥'}
    def render(match):
        inner=match.group(1)
        result=re.sub(r'\\([A-Za-z]+)', lambda m:symbols.get(m[1], m[0]), inner)
        return match[0] if '\\' in result else result
    return re.sub(r'\\\((.*?)\\\)',render,text)

def plain_numeric_markup(text):
    """只去除包裹纯数值的成对装饰标签；上标、未知标签和原文保持不变。"""
    candidate=text.strip()
    while True:
        value=re.sub(r'<(b|strong|i|em)>\s*([^<>]*)\s*</\1>',lambda m:m.group(2),candidate,flags=re.IGNORECASE)
        if value==candidate:break
        candidate=value
    if candidate==text.strip():return text
    if re.fullmatch(r'\s*[\$¥￥€£]?\s*[+\-−]?\(?[\d,]+(?:\.\d+)?\)?%?\s*',candidate):return candidate
    return text


def comparison_text(text):
    """仅归一化多余空白与注释编号排版，保留有意义的分词空格。"""
    normalized = re.sub(r'\s+', ' ', plain_numeric_markup(plain_math_text(text)).strip())
    return re.sub(r'(?<=注)\s+(?=\d)', '', normalized)


def verified_match(item, check):
    """文字归一化不能代替对位置、状态和合并关系的独立确认。"""
    if (item['state'] == 'unknown' or check['state'] != item['state']
            or not check['position_consistent'] or not check['span_consistent']):
        return False
    original, observed = item['text'].strip(), check['text'].strip()
    if original == observed:
        return check['consistent']
    if comparison_text(original) == comparison_text(observed):
        return True
    # 仅表外单位说明的装饰性整句括号等价；数值括号、局部括号和位置冲突不放行。
    if item.get('kind') == 'unit':
        def unit_text(text):
            text = comparison_text(text)
            if (len(text) > 2 and (text[0], text[-1]) in (('(', ')'), ('（', '）'))
                    and not any(c in text[1:-1] for c in '()（）')
                    and re.search(r'[A-Za-z\u4e00-\u9fff]', text[1:-1])):
                return text[1:-1].strip()
            return text
        return unit_text(original) == unit_text(observed)
    return False


def route(item, check, rules, low, high):
    """阈值是待校准参数；高分不能覆盖冲突、未知和规则失败。"""
    if not verified_match(item, check):
        return 'reinspect', '原文无法确认或独立复核存在冲突'
    if any(r['state'] == 'failed' for r in rules):
        return 'reinspect', '适用规则校验失败'
    confidence = min(item['confidence'], check['confidence'])
    if confidence > high:
        return 'accepted', '识别与复核一致且均高于高阈值'
    if confidence >= low and any(r['state'] == 'passed' for r in rules):
        return 'accepted', '中置信度且有适用规则通过'
    return 'reinspect', '低置信度或中置信度缺少通过的适用规则'
