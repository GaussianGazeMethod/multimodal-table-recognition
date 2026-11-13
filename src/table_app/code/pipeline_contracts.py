# -*- coding: utf-8 -*-
"""各阶段共用同一份输出契约：提示词定义格式，宿主拒绝不合约结果。"""
import json
from jsonschema import Draft202012Validator

VERSION = '2.9-efficiency'
RECOGNITION_VERSION = '2.0'
TEXT = {'type': 'string'}
BOOL = {'type': 'boolean'}
INDEX = {'type': 'integer', 'minimum': 0}
POSITIVE = {'type': 'integer', 'minimum': 1}
SCORE = {'type': 'number', 'minimum': 0, 'maximum': 1}
STATE = {'enum': ['value', 'empty', 'unknown']}


def obj(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def array(items, **limits):
    return {'type': 'array', 'items': items, **limits}


BOX = array({'type': 'number', 'minimum': 0, 'maximum': 1000}, minItems=4, maxItems=4)
CELL_REF = array(INDEX, minItems=2, maxItems=2)
REF = {'oneOf': [CELL_REF, obj(context_id=TEXT)]}
VALUE_REF = {'oneOf': [CELL_REF, obj(context_id=TEXT), obj(missing_field=TEXT)]}
PART = obj(page=POSITIVE, bbox=BOX)
CELL = obj(text=TEXT, row=INDEX, col=INDEX, rowspan=POSITIVE, colspan=POSITIVE,
           state=STATE, confidence=SCORE, source=obj(part=INDEX, bbox=BOX))
TABLE = obj(row_count=POSITIVE, col_count=POSITIVE, structure_confidence=SCORE, cells=array(CELL, minItems=1))
CONTEXT = obj(text=TEXT, state=STATE, confidence=SCORE, kind={'enum': ['title', 'unit', 'note', 'paragraph', 'header', 'footer']},
              table_ids=array(TEXT, uniqueItems=True), source=PART)
RULE = obj(op={'const': 'sum'}, refs=array(CELL_REF, minItems=1, uniqueItems=True), total=CELL_REF, reason=TEXT)
COMMON = dict(evidence_refs=array(CELL_REF, uniqueItems=True), context_ids=array(TEXT, uniqueItems=True), rules=array(RULE))
EXPRESSION = {'oneOf': [
    obj(op={'const': 'cell'}, ref=REF),
    obj(op={'const': 'text'}, ref=REF),
    obj(op={'const': 'substring'}, ref=REF, start=INDEX, end=INDEX),
    obj(op={'const':'count_character'}, ref=REF, character=TEXT, ignore_case=BOOL),
    obj(op={'const':'labeled_number'}, ref=REF, label=TEXT),
    obj(op={'const':'labeled_sequence_number'}, ref=REF, label=TEXT, index=INDEX),
    obj(op={'const': 'time_diff'}, start=obj(ref=REF, index=INDEX), end=obj(ref=REF, index=INDEX),
        unit={'enum': ['minutes', 'hours']}, overnight=BOOL),
    obj(op={'const': 'join'}, args=array({'$ref': '#/$defs/expression'}, minItems=1), separator=TEXT),
    obj(op={'const': 'language'}, refs=array(REF, minItems=1, uniqueItems=True), languages=array(TEXT, minItems=1, uniqueItems=True), reason=TEXT),
    obj(op={'const': 'text_equals'}, ref=REF, expected=TEXT, yes=TEXT, no=TEXT),
    obj(op={'const': 'all_of'}, args=array({'$ref':'#/$defs/expression'}, minItems=1), yes=TEXT, no=TEXT),
    obj(op={'const': 'numeric_trend'}, refs=array(REF, minItems=2, uniqueItems=True), direction={'enum':['increasing','decreasing','nondecreasing','nonincreasing']}, yes=TEXT, no=TEXT),
    obj(op={'const': 'text_contains'}, refs=array(REF, minItems=1), expected=TEXT, yes=TEXT, no=TEXT),
    obj(op={'const': 'range_contains'}, ref=REF, value=TEXT, yes=TEXT, no=TEXT),
    obj(op={'const': 'has_multi_header'}, rows=array(INDEX, minItems=1, uniqueItems=True), yes=TEXT, no=TEXT),
    obj(op={'const': 'count_columns'}, row=INDEX, columns=array(INDEX, minItems=1, uniqueItems=True)),
    obj(op={'const':'count_equal'}, refs=array(CELL_REF,minItems=1,uniqueItems=True), expected=TEXT),
    obj(op={'const':'argmax_counts'}, groups=array(obj(label=CELL_REF, refs=array(CELL_REF,minItems=1,uniqueItems=True)),minItems=1), expected=TEXT),
    obj(op={'const':'select_column_labels'}, row=INDEX, header_row=INDEX, columns=array(INDEX,minItems=1,uniqueItems=True), expected=TEXT, separator=TEXT),
    obj(op={'enum': ['argmax', 'argmin']}, values=array(CELL_REF, minItems=1), labels=array(CELL_REF, minItems=1)),
    obj(op={'enum': ['sum', 'product', 'min', 'max', 'mean']}, args=array({'$ref': '#/$defs/expression'}, minItems=1)),
    obj(op={'enum': ['difference', 'ratio', 'growth']}, args=array({'$ref': '#/$defs/expression'}, minItems=2, maxItems=2)),
    obj(op={'const': 'query'}, rows=array(INDEX, uniqueItems=True), column=INDEX,
        filters=array(obj(column=INDEX, cmp={'enum': ['eq', 'ne', 'gt', 'ge', 'lt', 'le']}, value=TEXT)),
        aggregate={'enum': ['values', 'count', 'distinct_count', 'sum', 'min', 'max']},
        sort={'oneOf': [{'type': 'null'}, obj(column=INDEX, descending=BOOL)]},
        limit={'type': ['integer', 'null'], 'minimum': 1})]}
PLAN = {'oneOf': [
    obj(kind={'const': 'structure'}, all=BOOL, rows=array(INDEX, uniqueItems=True), cols=array(INDEX, uniqueItems=True), **COMMON),
    obj(kind={'const': 'values'}, refs=array(VALUE_REF, minItems=1), as_array=BOOL, **COMMON),
    obj(kind={'const': 'calculate'}, expressions=array({'$ref': '#/$defs/expression'}, minItems=1),
        as_array=BOOL, scale=TEXT, decimals={'type': ['integer', 'null'], 'minimum': 0, 'maximum': 12}, percent=BOOL, **COMMON),
    obj(kind={'const': 'cross_collect'}, items=array(obj(table_id=TEXT, question=TEXT,
        plan={'oneOf': [obj(kind={'const': 'values'}, refs=array(VALUE_REF, minItems=1), as_array=BOOL, **COMMON),
                        obj(kind={'const': 'calculate'}, expressions=array({'$ref': '#/$defs/expression'}, minItems=1),
                            as_array=BOOL, scale=TEXT, decimals={'type': ['integer', 'null'], 'minimum': 0, 'maximum': 12},
                            percent=BOOL, **COMMON)]}), minItems=2)),
    obj(kind={'const': 'cross_rank'}, direction={'enum': ['max', 'min']}, separator=TEXT,
        groups=array(obj(parent_table=TEXT, parent_ref=CELL_REF, child_table=TEXT,
                         rows=array(INDEX, minItems=1, uniqueItems=True), label_column=INDEX, value_column=INDEX,
                         parent_evidence=array(CELL_REF, uniqueItems=True), child_evidence=array(CELL_REF, uniqueItems=True)), minItems=1)),
    obj(kind={'const': 'cross_difference'},
        alternatives=array(obj(left=obj(table_id=TEXT, ref=CELL_REF), right=obj(table_id=TEXT, ref=CELL_REF)), minItems=2),
        evidence=array(obj(table_id=TEXT, refs=array(CELL_REF, uniqueItems=True), context_ids=array(TEXT, uniqueItems=True))),
        excluded=array(obj(table_id=TEXT, reason=TEXT))),
    obj(kind={'const': 'unknown'}, reason=TEXT)], '$defs': {'expression': EXPRESSION}}
CHECK = obj(id=TEXT, text=TEXT, state=STATE, confidence=SCORE, consistent=BOOL,
            position_consistent=BOOL, span_consistent=BOOL, reason=TEXT)
SCHEMAS = {
    'input-quality': obj(pages=array(obj(page=POSITIVE, rotation_clockwise={'enum': [0, 90, 180, 270, None]},
                                        readability={'enum': ['clear', 'small', 'blurred', 'uncertain']}, reason=TEXT), minItems=1)),
    'locate': obj(tables=array(obj(title=TEXT, headers=TEXT, unit=TEXT, description=TEXT, parts=array(PART, minItems=1)))),
    'boundaries': obj(parts=array(obj(table_id=TEXT, part=INDEX, complete=BOOL, bbox=BOX, confidence=SCORE, reason=TEXT))),
    'context': obj(items=array(CONTEXT)),
    'candidate-set': obj(candidates=array(TEXT, uniqueItems=True), reason=TEXT),
    'select': obj(status={'enum': ['selected', 'ambiguous', 'not_found']}, table_id={'type': ['string', 'null']},
                  candidates=array(TEXT, uniqueItems=True), reason=TEXT),
    'recognize': TABLE,
    'recognize-header': TABLE,
    'structure-reinspect': TABLE,
    'plan': PLAN,
    'verify': obj(plan_valid=BOOL, plan_reason=TEXT, structure_valid=BOOL, structure_confidence=SCORE, checks=array(CHECK)),
    'field-reinspect': obj(items=array(obj(id=TEXT, text=TEXT, state=STATE, confidence=SCORE))),
}
SCHEMAS['icon-target'] = obj(status={'enum':['needed','not_applicable','uncertain']}, refs=array(CELL_REF,uniqueItems=True), reason=TEXT)
SCHEMAS['visual-observe'] = obj(status={'enum':['observed','uncertain']},
    scope=TEXT, predicate=TEXT, complete=BOOL, confidence=SCORE, reason=TEXT,
    matches=array(obj(page=POSITIVE, bbox=BOX, description=TEXT)))
SCHEMAS['visual-handwriting'] = obj(status={'enum':['observed','uncertain']}, complete=BOOL, reason=TEXT, items=array(obj(page=POSITIVE,bbox=BOX,text=TEXT,confidence=SCORE)))
SCHEMAS['answer-fallback'] = obj(answer={'type':['string','number','array','object']}, confidence=SCORE, reason=TEXT)
# 结构题作为候选先参与既定投票；选中之后再据此拒绝缺项，不能跳过高票候选。
STRUCTURE_FALLBACK_ANSWER = obj(row_count=POSITIVE, col_count=POSITIVE,
    cells=array(obj(text=TEXT, row=INDEX, col=INDEX, rowspan=POSITIVE, colspan=POSITIVE)))


def structure_fallback_errors(answer, multiple=False):
    """返回结构候选的字段及类型错误；不补造缺失值。"""
    if answer == '':
        return ['模型明确表示无法确认']
    schema = {'type':'array','minItems':2,'items':STRUCTURE_FALLBACK_ANSWER} if multiple else STRUCTURE_FALLBACK_ANSWER
    errors = sorted(Draft202012Validator(schema).iter_errors(answer),
                    key=lambda item: (tuple(str(part) for part in item.absolute_path), item.message))
    return [f"{'.'.join(map(str, item.absolute_path)) or '$'}: {item.message}" for item in errors]
SCHEMAS['visual-title'] = obj(status={'enum':['observed','not_found','uncertain']}, complete=BOOL, reason=TEXT,
    items=array(obj(page=POSITIVE,bbox=BOX,text=TEXT,confidence=SCORE)))
SCHEMAS['column-structure-verify'] = obj(plan_valid=BOOL, structure_valid=BOOL, confidence=SCORE, column_count=INDEX, reason=TEXT)
SCHEMAS['semantic-link-verify'] = obj(relation_valid=BOOL, confidence=SCORE, target_id=TEXT, observed_text=TEXT, row_label=TEXT, column_label=TEXT, reason=TEXT)
SCHEMAS['footnote-verify'] = obj(valid=BOOL, confidence=SCORE, items=array(obj(target_id=TEXT, numeric_text=TEXT, marker=TEXT),minItems=1), reason=TEXT)
SCHEMAS['semantic-multi-link-verify'] = obj(relation_valid=BOOL, confidence=SCORE, items=array(obj(target_id=TEXT,observed_text=TEXT,row_label=TEXT,column_label=TEXT),minItems=2), reason=TEXT)
SCHEMAS['visual-scope-review'] = obj(status={'enum':['clear','ambiguous']}, included=array(INDEX, uniqueItems=True), confidence=SCORE, reason=TEXT)
SCHEMAS['select-visual'] = SCHEMAS['select']
SCHEMAS['parser-anchor'] = obj(status={'enum':['located','not_found','uncertain']}, table_id=TEXT, row=INDEX, original_text=TEXT, observed_text=TEXT, confidence=SCORE, reason=TEXT)

SCHEMAS['language-verify']=obj(scope_valid=BOOL,languages=array(TEXT,minItems=1,uniqueItems=True),confidence=SCORE,reason=TEXT,evidence=array(obj(id=TEXT,text=TEXT,position_consistent=BOOL),minItems=1))


def validate(phase, value):
    error = next(Draft202012Validator(SCHEMAS[phase]).iter_errors(value), None)
    if error:
        path = '.'.join(map(str, error.absolute_path)) or '$'
        raise ValueError(f'{phase} 输出契约错误，位置 {path}，违反 {error.validator} 约束')
    return value


def instructions(phase):
    return '\n必须严格符合以下JSON Schema，不能添加字段：\n' + json.dumps(SCHEMAS[phase], ensure_ascii=False)
