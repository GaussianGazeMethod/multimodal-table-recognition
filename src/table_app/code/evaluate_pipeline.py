#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构造独立合成数据与标准答案，对比直接答题和结构化流程。"""
import argparse
import io
import json
from pathlib import Path
from datetime import datetime
from copy import deepcopy
from PIL import Image, ImageDraw, ImageFont
import fitz
from openai import OpenAI
import table_solver as base
from table_pipeline import Pipeline, execute, UncertainAnswer


def cell(text,row,col,rowspan=1,colspan=1):
    return dict(text=text,row=row,col=col,rowspan=rowspan,colspan=colspan)


def draw_table(title,cells,rows,cols,width=1100,height=650):
    image=Image.new('RGB',(width,height),'white');draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc',26)
    draw.text((40,20),title,fill='black',font=font)
    x,y,cw,ch=40,90,(width-80)/cols,100
    for c in cells:
        box=(x+c['col']*cw,y+c['row']*ch,x+(c['col']+c['colspan'])*cw,y+(c['row']+c['rowspan'])*ch)
        draw.rectangle(box,outline='black',width=2)
        draw.text((box[0]+10,box[1]+30),c['text'],fill='black',font=font)
    return image


def build(folder):
    folder.mkdir(parents=True,exist_ok=True)
    sales={'row_count':4,'col_count':3,'cells':[
        cell('Item',0,0,2,1),cell('Sales (USD)',0,1,1,2),cell('2025',1,1),cell('2026',1,2),
        cell('Alpha',2,0),cell('1,000',2,1),cell('1,250',2,2),cell('Beta',3,0),cell('',3,1),cell('250',3,2)]}
    image=draw_table('Annual product sales - Unit: USD',sales['cells'],4,3)
    image.save(folder/'sales.png')
    image.resize((660,390)).rotate(90,expand=True).save(folder/'rotated.png')
    two=Image.new('RGB',(1100,1000),'white')
    left=[cell('Category',0,0),cell('Amount',0,1),cell('A',1,0),cell('10',1,1),cell('B',2,0),cell('20',2,1)]
    right=[cell('Movement',0,0),cell('Amount',0,1),cell('Opening',1,0),cell('80',1,1),cell('Closing',2,0),cell('90',2,1)]
    two.paste(draw_table('Allowance by category',left,3,2,height=450),(0,0))
    two.paste(draw_table('Allowance movement',right,3,2,height=450),(0,500));two.save(folder/'multiple.png')
    with fitz.open() as doc:
        for title,name,value in [('Inventory register - Unit: pieces','Alpha','10'),('Inventory register (continued) - Unit: pieces','Beta','20')]:
            im=draw_table(title,[cell('Item',0,0),cell('Qty',0,1),cell(name,1,0),cell(value,1,1)],2,2,height=400)
            data=io.BytesIO();im.save(data,format='PNG')
            page=doc.new_page(width=550,height=200);page.insert_image(page.rect,stream=data.getvalue())
        doc.save(folder/'continued.pdf')
    # 独立构造近似长文本，标准答案来自制图内容，不使用外部文档。
    detail = Image.new('RGB', (1500, 440), 'white')
    draw = ImageDraw.Draw(detail)
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 27)
    draw.text((35, 20), '产品功能对比', fill='black', font=font)
    rows = [('产品', '功能说明'),
            ('产品甲', '全景低代码智能体开发平台，支持拖拽式工作流设计。'),
            ('产品乙', '全场景低代码开发平台，支持拖拽式流程设计。')]
    for row, (name, description) in enumerate(rows):
        top = 90 + row * 100
        draw.rectangle((30, top, 300, top + 100), outline='black', width=2)
        draw.rectangle((300, top, 1470, top + 100), outline='black', width=2)
        draw.text((45, top + 30), name, fill='black', font=font)
        draw.text((315, top + 30), description, fill='black', font=font)
    detail.save(folder/'long_text.png')
    partial=deepcopy(sales);partial['cells']=[deepcopy(c) for c in sales['cells'] if c['row']==0 or c['col']==0]
    def q(id,file,type,text,fmt,gold,hint=''):
        return dict(id=id,file_name=file,question_type=type,question=text,answer_format=fmt,table_hint=hint,gold=gold)
    cases=[
        q('S1','sales.png','structure','恢复整个年度产品销售表。','json',sales),
        q('S2','sales.png','structure','仅恢复第一行和第一列的并集结构，行列总数保持整表。','json',partial),
        q('E1','sales.png','extract','按2025、2026顺序提取Beta销售额，空白保留空字符串。','json_array',['','250']),
        q('T1','sales.png','thinking','计算2026年两种产品销售额合计，单位USD。','number','1500'),
        q('T2','sales.png','thinking','Alpha销售额从2025到2026的增长率是多少？输出整数百分比。','string','25%'),
        q('A1','multiple.png','structure','恢复坏账表的第一行。','json',''),
        q('T3','multiple.png','thinking','计算Allowance by category表中A和B的金额合计。','number','30','Allowance by category'),
        q('T4','continued.pdf','thinking','跨页汇总Inventory register全部库存数量，不能重复计算表头。','number','30','Inventory register'),
        q('E3','sales.png','extract','提取表格上方完整标题，包括Unit说明，保持原文。','string','Annual product sales - Unit: USD'),
        q('E2','rotated.png','extract','提取Alpha在2026年的销售额，去除千分位逗号。','number','1250'),
        q('E4','long_text.png','extract','提取产品甲的完整功能说明，保持原文。','string','全景低代码智能体开发平台，支持拖拽式工作流设计。')]
    (folder/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    return cases


def canonical(value,kind):
    if kind=='structure' and isinstance(value,dict):
        value=deepcopy(value);value['cells']=sorted(value['cells'],key=lambda c:(c['row'],c['col']))
        return value
    if isinstance(value,list):return [canonical(v,'value') for v in value]
    return str(value)


def score(answer,case):
    expected=case['gold']
    if isinstance(expected,(list,dict)):
        try:actual=json.loads(answer)
        except (ValueError,TypeError):return False
    else:actual=answer
    return canonical(actual,case['question_type'])==canonical(expected,case['question_type'])


def offline(cases):
    table=deepcopy(cases[0]['gold'])
    for c in table['cells']:c['state']='value' if c['text'] else 'empty'
    assert execute(table,{'kind':'calculate','expressions':[{'op':'sum','args':[{'op':'cell','ref':[2,2]},{'op':'cell','ref':[3,2]}]}],'as_array':False,'scale':'1','decimals':None,'percent':False},'thinking')=='1500'
    assert execute(table,{'kind':'structure','all':False,'rows':[0],'cols':[0]},'structure')==cases[1]['gold']
    assert execute(table,{'kind':'values','refs':[[3,1],[3,2]],'as_array':True},'extract')==['','250']
    query={'op':'query','rows':[2,3],'column':0,'filters':[{'column':2,'cmp':'gt','value':'200'}],'aggregate':'values','sort':{'column':2,'descending':True},'limit':1}
    plan={'kind':'calculate','expressions':[query],'as_array':False,'scale':'1','decimals':None,'percent':False}
    assert execute(table,plan,'thinking')=='Alpha'
    all_query=deepcopy(query);all_query['limit']=None;all_query['sort']=None
    all_plan=dict(plan,expressions=[all_query],as_array=True)
    assert execute(table,all_plan,'thinking')==['Alpha','Beta']
    try: execute(table,dict(all_plan,as_array=False),'thinking')
    except UncertainAnswer: pass
    else: raise AssertionError('多个查询结果不能写入单值答案')
    tied=deepcopy(table); next(c for c in tied['cells'] if c['row']==3 and c['col']==2)['text']='1250'
    try: execute(tied,plan,'thinking')
    except UncertainAnswer: pass
    else: raise AssertionError('并列最大值不能强行选出单个名称')
    tied_third = deepcopy(table)
    next(c for c in tied_third['cells'] if c['row'] == 3 and c['col'] == 2)['text'] = '1250'
    extra = [deepcopy(c) for c in tied_third['cells'] if c['row'] == 3]
    for c in extra:
        c['row'] = 4
        if c['col'] == 0:
            c['text'] = 'Gamma'
    next(c for c in tied_third['cells'] if c['row'] == 3 and c['col'] == 0)['text'] = 'Alpha'
    tied_third['cells'].extend(extra); tied_third['row_count'] = 5
    third_query = dict(query, rows=[2, 3, 4])
    try: execute(tied_third, dict(plan, expressions=[third_query]), 'thinking')
    except UncertainAnswer: pass
    else: raise AssertionError('必须检查全部并列极值，而不是只检查前两行')
    try: execute(tied_third, dict(plan, expressions=[third_query], as_array=True), 'thinking')
    except UncertainAnswer: pass
    else: raise AssertionError('数组结果也不能截断并列极值')
    try: execute(tied_third, dict(plan, expressions=[dict(third_query, limit=2)], as_array=True), 'thinking')
    except UncertainAnswer: pass
    else: raise AssertionError('排序截断处存在并列时不能丢掉结果')
    filtered_unknown = deepcopy(table)
    next(c for c in filtered_unknown['cells'] if c['row'] == 3 and c['col'] == 0).update(text='', state='unknown')
    filtered = dict(query, filters=[{'column': 2, 'cmp': 'gt', 'value': '1000'}], sort=None)
    assert execute(filtered_unknown, dict(plan, expressions=[filtered]), 'thinking') == 'Alpha'
    two_filters = dict(filtered, filters=[{'column': 0, 'cmp': 'eq', 'value': 'Alpha'},
                                          {'column': 2, 'cmp': 'gt', 'value': '1000'}])
    assert execute(filtered_unknown, dict(plan, expressions=[two_filters]), 'thinking') == 'Alpha'
    ranking_table={'cells':[{'row':r,'col':c,'rowspan':1,'colspan':1,'state':'value','text':v}
                             for r,c,v in [(0,0,'甲'),(0,1,'乙'),(1,0,'10'),(1,1,'30')]]}
    arg={'kind':'calculate','expressions':[{'op':'argmax','values':[[1,0],[1,1]],'labels':[[0,0],[0,1]]}],
         'as_array':False,'scale':'1','decimals':None,'percent':False}
    assert execute(ranking_table,arg,'thinking')=='乙'
    ranking_table['cells'][-1]['text']='10'
    # 并列时须按原表顺序完整输出所有名称，不得只取第一个。
    assert execute(ranking_table,arg,'thinking') == '甲、乙', '不同名称并列时必须保留全部结果'
    growth={'kind':'calculate','expressions':[{'op':'growth','args':[{'op':'cell','ref':[2,2]},{'op':'cell','ref':[2,1]}]}],
            'as_array':False,'scale':'1','decimals':0,'percent':True}
    assert execute(table,growth,'thinking')=='25%'
    converted=dict(growth, expressions=[{'op':'cell','ref':[2,2]}], scale='1000', decimals=2, percent=False)
    assert execute(table,converted,'thinking')=='1250000.00'
    assert execute(table,{'kind':'values','refs':[{'context_id':'C1'}],'as_array':False},'extract',
                   [{'id':'C1','text':'Amounts in USD','state':'value'}])=='Amounts in USD'
    mixed = {'kind':'calculate', 'expressions':[{'op':'cell','ref':[2,2]}, {'op':'text','ref':{'context_id':'C1'}},
             {'op':'count_columns','row':1,'columns':[1,2]}], 'as_array':True, 'scale':'1', 'decimals':None, 'percent':False}
    assert execute(table,mixed,'thinking',[{'id':'C1','text':'USD','state':'value'}]) == ['1250','USD','2']
    table['cells'][-1]['state']='unknown';table['cells'][-1]['text']=''
    try:execute(table,{'kind':'values','refs':[[3,2]],'as_array':False},'extract')
    except UncertainAnswer:pass
    else:raise AssertionError('未知值被当作答案')
    print('离线验收通过：合并单元格局部范围、空值、加总、筛选排序与未知值保护')


def quality_offline(cases, folder):
    """契约、边界路由、缓存和实际Excel消费的确定性验收，不冒充模型识别正确率。"""
    from tempfile import TemporaryDirectory
    from unittest.mock import patch
    from openpyxl import load_workbook
    from pipeline_contracts import validate, SCHEMAS
    from jsonschema import Draft202012Validator
    from quality_routing import dependencies, route, empty_sum_conflicts
    from table_pipeline import atomic_json, verification_view, separate_part_padding, explicit_table_mentions, explicitly_named_continuation
    checks_passed = []

    def rejects(action):
        try:
            action()
        except ValueError:
            return
        raise AssertionError('本应拒绝的结果被接纳')

    for schema in SCHEMAS.values():
        Draft202012Validator.check_schema(schema)
    single = {'row_count': 1, 'col_count': 1, 'cells': [{'text': '甲', 'row': 0, 'col': 0, 'rowspan': 1, 'colspan': 1}]}
    multiple = [single, deepcopy(single)]
    assert json.loads(base.format_answer(json.dumps({'answer': single}), 'structure', 'json')) == single
    assert json.loads(base.format_answer(json.dumps({'answer': multiple}), 'structure', 'structure_array')) == multiple
    rejects(lambda: base.format_answer(json.dumps({'answer': multiple}), 'structure', 'json'))
    broken = deepcopy(multiple); broken[1]['cells'][0]['col'] = 1
    rejects(lambda: base.format_answer(json.dumps({'answer': broken}), 'structure', 'structure_array'))
    checks_passed.append('多表数组逐表校验，单表格式及坐标越界保护不变')
    item = {'text': '10', 'state': 'value', 'confidence': .99}
    check = dict(item, consistent=True, position_consistent=True, span_consistent=True)
    for confidence, rules, expected in [(.91, [], 'accepted'), (.90, [], 'reinspect'),
                                       (.70, [{'state': 'passed'}], 'accepted'),
                                       (.69, [{'state': 'passed'}], 'reinspect'),
                                       (.99, [{'state': 'failed'}], 'reinspect')]:
        check['confidence'] = confidence
        assert route(item, check, rules, .70, .90)[0] == expected
    check.update(confidence=.99, consistent=False)
    assert route(item, check, [], .70, .90)[0] == 'reinspect'
    note = {'text': '其他变动(注1)', 'state': 'value', 'confidence': .99}
    note_check = {'text': '其他变动(注 1)', 'state': 'value', 'confidence': .95,
                  'consistent': False, 'position_consistent': True, 'span_consistent': True}
    assert route(note, note_check, [], .70, .90)[0] == 'accepted'
    assert route(note, dict(note_check, position_consistent=False), [], .70, .90)[0] == 'reinspect'
    assert route(note, dict(note_check, span_consistent=False), [], .70, .90)[0] == 'reinspect'
    assert route(note, dict(note_check, confidence=.80), [], .70, .90)[0] == 'reinspect'
    assert route(note, dict(note_check, text='其他变动(注 2)'), [], .70, .90)[0] == 'reinspect'
    name = dict(note, text='New York')
    assert route(name, dict(note_check, text='NewYork'), [], .70, .90)[0] == 'reinspect'
    checks_passed.append('置信度边界0.70/0.90、规则失败和高分冲突')
    plan = dict(kind='values', refs=[[2, 2]], as_array=False, evidence_refs=[[0, 1], [1, 2], [2, 0]], context_ids=[], rules=[])
    validate('plan', plan)
    rejects(lambda: validate('plan', dict(plan, guessed_answer='1250')))
    rejects(lambda: validate('plan', dict(plan, refs=[[True, 2]])))
    rejects(lambda: validate('plan', dict(plan, as_array='false')))
    rejects(lambda: Pipeline.checks_by_id([], {'cell:2:2': item}))
    checks_passed.append('非法字段、布尔坐标、错误类型和复核遗漏被拒绝')
    table = deepcopy(cases[0]['gold'])
    table['structure_confidence'] = .99
    for c in table['cells']:
        c.update(state='value' if c['text'] else 'empty', confidence=.99, source={'part': 0, 'bbox': [0, 0, 1000, 1000]})
    projected = verification_view(table)
    assert projected['row_count'] == table['row_count'] and projected['col_count'] == table['col_count']
    assert len(projected['cells']) == len(table['cells'])
    for original, visible in zip(table['cells'], projected['cells']):
        assert all(visible[key] == original[key] for key in ('text', 'row', 'col', 'rowspan', 'colspan', 'state'))
        assert 'source' not in visible and 'confidence' not in visible
    checks_passed.append('验证输入保留全表文字与结构并移除重复来源元数据')
    blocks = [{'page': 1, 'boundary_bbox': [0, 90, 658, 816], 'bbox': [0, 60, 688, 846]},
              {'page': 1, 'boundary_bbox': [0, 820, 658, 988], 'bbox': [0, 790, 688, 1000]}]
    separate_part_padding(blocks)
    assert blocks[0]['bbox'][3] == blocks[1]['bbox'][1] == 818
    continuation = [{'page': 1, 'boundary_bbox': [0, 0, 1000, 980], 'bbox': [0, 0, 1000, 1000]},
                    {'page': 2, 'boundary_bbox': [0, 20, 1000, 980], 'bbox': [0, 0, 1000, 1000]}]
    before = deepcopy(continuation)
    separate_part_padding(continuation)
    assert continuation == before
    checks_passed.append('同页表块扩边不重叠且跨页续表边界保持不变')
    named_tables = [{'table_id': 'T1', 'title': '表1（续）'}, {'table_id': 'T2', 'title': '表2 测试表'}]
    assert explicit_table_mentions('恢复表1（续）和表2的表头', named_tables) == ['T1', 'T2']
    assert not explicit_table_mentions('恢复表1或者表2的表头', named_tables)
    assert not explicit_table_mentions('恢复表1的第一行和第一列', named_tables)
    assert not explicit_table_mentions('恢复前两行和第一列', named_tables)
    assert explicitly_named_continuation('恢复表1（续）和表2的表头', '表1（续）')
    assert not explicitly_named_continuation('恢复表1和表2的表头', '表1（续）')
    checks_passed.append('明确同时指定两表才触发多表意图，选择关系及行列并集不触发')
    table['cells'][-1].update(state='unknown', text='', confidence=.2)
    targets = dependencies(table, [], plan, 'extract')
    assert 'cell:3:2' not in targets and 'cell:2:2' in targets
    unrelated = [{'id': 'C1', 'kind': 'unit', 'text': '', 'state': 'unknown', 'confidence': .1}]
    assert 'C1' not in dependencies(table, unrelated, plan, 'extract')
    assert 'C1' in dependencies(table, unrelated, dict(plan, context_ids=['C1']), 'extract')
    unit_plan = dict(kind='calculate', expressions=[{'op': 'cell', 'ref': [2, 2]}], as_array=False,
                     scale='1', decimals=None, percent=False, evidence_refs=[], context_ids=[], rules=[])
    assert 'C1' not in dependencies(table, unrelated, unit_plan, 'thinking')
    assert 'C1' in dependencies(table, unrelated, dict(unit_plan, context_ids=['C1']), 'thinking')
    mixed_plan = dict(unit_plan, expressions=[{'op':'text','ref':{'context_id':'C1'}},
                      {'op':'count_columns','row':1,'columns':[1,2]}], as_array=True)
    assert {'C1', 'cell:1:1', 'cell:1:2'} <= set(dependencies(table, unrelated, mixed_plan, 'thinking'))
    rejects(lambda: dependencies(table, [], dict(plan, refs=[[90, 0]]), 'extract'))
    sum_plan = dict(unit_plan, expressions=[{'op':'sum','args':[{'op':'cell','ref':[2,1]}]}],
                    evidence_refs=[[3,1]])
    assert empty_sum_conflicts(table, sum_plan, 'Alpha+Beta合计') == [[3,1]]
    assert empty_sum_conflicts(table, dict(sum_plan, evidence_refs=[]), 'Alpha+Beta合计') == [[3,1]]
    assert not empty_sum_conflicts(table, sum_plan, 'Alpha合计')
    checks_passed.append('关键字段按实际引用确定、非关键未知不阻塞、越界引用拒绝')
    from table_pipeline import execute
    zero_table = {'row_count': 2, 'col_count': 1, 'cells': [
        {'row': 0, 'col': 0, 'rowspan': 1, 'colspan': 1, 'text': '12', 'state': 'value'},
        {'row': 1, 'col': 0, 'rowspan': 1, 'colspan': 1, 'text': '', 'state': 'empty'}]}
    zero_plan = dict(unit_plan, expressions=[{'op': 'sum', 'args': [
        {'op': 'cell', 'ref': [0, 0]}, {'op': 'cell', 'ref': [1, 0]}]}])
    assert execute(zero_table, zero_plan, 'thinking', confirmed_empty_sum_refs=[[1, 0]]) == '12'
    rejects(lambda: execute(zero_table, zero_plan, 'thinking'))
    unknown_table = deepcopy(zero_table)
    unknown_table['cells'][1]['state'] = 'unknown'
    rejects(lambda: execute(unknown_table, zero_plan, 'thinking', confirmed_empty_sum_refs=[[1, 0]]))
    nonsum_plan = dict(unit_plan, expressions=[{'op': 'cell', 'ref': [1, 0]}])
    rejects(lambda: execute(zero_table, nonsum_plan, 'thinking', confirmed_empty_sum_refs=[[1, 0]]))
    checks_passed.append('仅明确确认的空白加总项按零，unknown和非加总场景仍拒绝')
    from human_confirmation import plan_conflicts, apply_confirmation
    scoped_question = {'question_type': 'structure', 'question': '恢复前2行和第一列'}
    confirmed = {'kind': 'table_selection', 'payload': {'table_id': 'T1'}}
    scoped_plan = {'kind': 'structure', 'all': False, 'rows': [0, 1], 'cols': [0],
                   'evidence_refs': [], 'context_ids': [], 'rules': []}
    assert not plan_conflicts(confirmed, scoped_question, table, [], scoped_plan)
    assert plan_conflicts(confirmed, scoped_question, table, [], dict(scoped_plan, cols=[1]))
    confirmed_intent = {'kind': 'question_intent', 'payload': {'table_id': 'T1', 'constraints': {'rows': [0]}}}
    assert plan_conflicts(confirmed_intent, scoped_question, table, [], scoped_plan)
    checks_passed.append('人工确认及原题范围未落实的计划被拒绝')
    block_confirmation = {'kind': 'block_relation', 'payload': {'table_id': 'T1', 'parts': [1], 'relation': 'independent'}}
    block_region = {'table_id': 'T1', 'parts': [{'page': 1}, {'page': 2}]}
    _, _, revised_region, _, needs_recognition = apply_confirmation(block_confirmation, table, [], block_region, None)
    assert needs_recognition and len(revised_region['parts']) == 1 and revised_region['parts'][0]['page'] == 2
    assert len(block_region['parts']) == 2
    checks_passed.append('确认独立表块后只重新识别变化范围且不覆盖原区域')
    cell = next(c for c in table['cells'] if (c['row'], c['col']) == (2, 2))
    corrected = {'kind': 'field', 'payload': {'table_id': 'T1', 'ref': [2, 2],
                 'original': {'text': cell['text'], 'state': cell['state']},
                 'replacement': {'text': '2500', 'state': 'value'}}}
    updated, _, _, _, changed_scope = apply_confirmation(corrected, table, [], {'table_id': 'T1', 'parts': []}, None)
    assert not changed_scope and next(c for c in updated['cells'] if (c['row'], c['col']) == (2, 2))['text'] == '2500'
    assert next(c for c in table['cells'] if (c['row'], c['col']) == (2, 2))['text'] == cell['text']
    assert plan_conflicts(corrected, {'question_type': 'extract', 'question': '提取数据'}, updated, [], dict(plan, refs=[[2, 1]], evidence_refs=[]))
    empty_confirmation = {'kind': 'empty_semantics', 'payload': {'table_id': 'T1', 'refs': [[1, 0]], 'meaning': 'zero_in_sum'}}
    _, _, _, zero_refs, _ = apply_confirmation(empty_confirmation, zero_table, [], {'table_id': 'T1', 'parts': []}, None)
    assert execute(zero_table, zero_plan, 'thinking', confirmed_empty_sum_refs=zero_refs) == '12'
    rejects(lambda: apply_confirmation(empty_confirmation, unknown_table, [], {'table_id': 'T1', 'parts': []}, None))
    checks_passed.append('人工字段修订保留原值且必须进入计划；确认空白不允许 unknown')
    query = {'op': 'query', 'rows': [2, 3], 'column': 0, 'filters': [{'column': 2, 'cmp': 'gt', 'value': '200'}],
             'sort': {'column': 1, 'descending': True}, 'aggregate': 'values', 'limit': 1}
    query_plan = dict(kind='calculate', expressions=[query], as_array=False, scale='1', decimals=None,
                      percent=False, evidence_refs=[], context_ids=[], rules=[])
    query_targets = dependencies(table, [], query_plan, 'thinking')
    assert {'cell:2:0', 'cell:3:0', 'cell:2:1', 'cell:3:1', 'cell:2:2', 'cell:3:2'} <= set(query_targets)
    checks_passed.append('筛选、排序和候选行全部纳入关键依赖')
    from table_pipeline import number, normalize
    filtered_table = deepcopy(table)
    next(c for c in filtered_table['cells'] if (c['row'], c['col']) == (3, 2)).update(text='50', state='value')
    narrowed = dependencies(filtered_table, [], query_plan, 'thinking', number, normalize)
    assert 'cell:3:2' in narrowed and 'cell:3:0' not in narrowed and 'cell:3:1' not in narrowed
    next(c for c in filtered_table['cells'] if (c['row'], c['col']) == (3, 2)).update(text='', state='unknown')
    assert 'cell:3:0' in dependencies(filtered_table, [], query_plan, 'thinking', number, normalize)
    checks_passed.append('仅排除已知不符合筛选的结果字段，未知条件保留候选')
    calls = []
    low_target = [False]
    medium_target = [False]
    unreadable = [False]
    bad_structure = [False]
    bad_boundary = [False]
    scope_pass = [False]
    reject_plan_once = [False]
    preview_invalid = [False]
    preview_conflict = [False]
    position_conflict = [False]
    invalid_reference = [False]
    invalid_contract = [False]
    header_case = [False]
    header_medium = [False]
    ranking_case = [False]
    ranking_cell = [False]

    def fake_response(client, messages, record, model):
        phase = record['phase']
        calls.append((phase, model))
        payload = json.loads(messages[1]['content'][0]['text'])
        if phase == 'input-quality':
            response = {'pages': [{'page': 1, 'rotation_clockwise': 0, 'readability': 'clear', 'reason': '合成测试'}]}
        elif phase == 'locate':
            response = {'tables': [{'title': 'Sales', 'headers': 'Item Sales', 'unit': '', 'description': '年度销售',
                                    'parts': [{'page': 1, 'bbox': [0, 0, 1000, 1000]}]}]}
        elif phase == 'boundaries':
            response = {'parts': [{'table_id': 'T1', 'part': 0, 'complete': not bad_boundary[0], 'bbox': [0, 0, 1000, 1000],
                                   'confidence': .99, 'reason': '完整网格'}]}
        elif phase == 'context':
            response = {'items': []}
        elif phase == 'select':
            response = {'status': 'selected', 'table_id': 'T1', 'candidates': ['T1'], 'reason': '唯一表格'}
        elif phase in ('recognize', 'structure-reinspect', 'recognize-header'):
            response = deepcopy(table)
            if phase == 'recognize-header':
                response['cells'] = [c for c in response['cells'] if c['row'] < 2]
                if header_medium[0]: response['structure_confidence'] = .8
            if bad_structure[0] and phase == 'recognize':
                response['cells'].pop()
            if low_target[0] and phase == 'recognize':
                next(c for c in response['cells'] if (c['row'], c['col']) == (2, 2)).update(text='', state='unknown', confidence=.2)
            if ranking_case[0]:
                next(c for c in response['cells'] if (c['row'], c['col']) == (3, 2)).update(text='250', state='value', confidence=.99)
            if medium_target[0]:
                next(c for c in response['cells'] if (c['row'], c['col']) == (2, 2))['confidence'] = .8
        elif phase == 'plan':
            assert all(p['type'] == 'text' for p in messages[1]['content'])
            if 'previous_plan' in payload:
                assert isinstance(payload['validation_feedback'], str)
            if header_case[0]:
                response = dict(kind='structure', all=False, rows=[0,1], cols=[],
                                evidence_refs=[], context_ids=[], rules=[])
            elif invalid_contract[0] and 'validation_feedback' not in payload:
                response = {'kind': 'values'}
            elif ranking_case[0]:
                if 'previous_plan' not in payload:
                    if ranking_cell[0]:
                        response = dict(kind='calculate', expressions=[{'op':'cell','ref':[2,0]}], as_array=False,
                                        scale='1', decimals=None, percent=False, evidence_refs=[], context_ids=[], rules=[])
                    else:
                        response = dict(kind='values', refs=[[2,0]], as_array=False, evidence_refs=[], context_ids=[], rules=[])
                else:
                    response = dict(kind='calculate', expressions=[{'op':'query','rows':[2,3],'column':0,'filters':[],
                                    'aggregate':'values','sort':{'column':2,'descending':True},'limit':1}],
                                    as_array=False, scale='1', decimals=None, percent=False,
                                    evidence_refs=[], context_ids=[], rules=[])
            elif invalid_reference[0] and 'previous_plan' not in payload:
                response = dict(plan, refs=[[999, 0]])
            elif preview_invalid[0] and 'previous_plan' not in payload:
                response = dict(kind='calculate', expressions=[{'op':'cell','ref':[2,0]}],
                                as_array=False, scale='1', decimals=None, percent=False,
                                evidence_refs=[[2,2]] if preview_conflict[0] else [], context_ids=[], rules=[])
            else:
                response = deepcopy(plan)
        elif phase == 'verify':
            if 'targets' not in payload and 'region' in payload:
                response = {'plan_valid': scope_pass[0], 'plan_reason': '题目所需范围是否可见',
                            'structure_valid': scope_pass[0], 'structure_confidence': .99, 'checks': []}
                return dict(content=json.dumps(response), finish_reason='stop', usage=None, response_id='deterministic-test')
            plan_valid = not reject_plan_once[0]
            reject_plan_once[0] = False
            response = {'plan_valid': plan_valid, 'plan_reason': '计划范围错误' if not plan_valid else '已核验题意和原图', 'structure_valid': True,
                        'structure_confidence': .99, 'checks': [dict(id=k, text=v['text'], state=v['state'],
                        confidence=.99 if v['state'] != 'unknown' else .2, consistent=v['state'] != 'unknown',
                        position_consistent=v['state'] != 'unknown', span_consistent=v['state'] != 'unknown', reason='测试证据')
                        for k, v in payload['targets'].items()]}
            if header_medium[0] and payload.get('partial_header'):
                response['structure_confidence'] = .85
            if preview_conflict[0] and 'cell:2:2' in payload['targets']:
                next(c for c in response['checks'] if c['id'] == 'cell:2:2')['consistent'] = False
                preview_conflict[0] = False
            if (position_conflict[0] and payload['targets'].get('cell:2:2', {}).get('state') == 'value'):
                next(c for c in response['checks'] if c['id'] == 'cell:2:2')['consistent'] = False
                next(c for c in response['checks'] if c['id'] == 'cell:2:2')['position_consistent'] = False
                position_conflict[0] = False
        elif phase == 'field-reinspect':
            assert set(payload['targets']) == {'cell:2:2'}
            response = {'items': [{'id': 'cell:2:2', 'text': '' if unreadable[0] else '1,250',
                                    'state': 'unknown' if unreadable[0] else 'value', 'confidence': .2 if unreadable[0] else .85 if medium_target[0] else .99}]}
        else:
            raise AssertionError(f'出现意外阶段 {phase}')
        return dict(content=json.dumps(response), finish_reason='stop', usage=None, response_id='deterministic-test')

    with TemporaryDirectory(prefix='table-contract-') as directory:
        root = Path(directory)
        question = dict(id='E', question_type='extract', question='Alpha 2026?', answer_format='number')
        entry = {'tables': [{'table_id': 'T1'}, {'table_id': 'T2'}]}
        candidate_answers = {'T1': '1250', 'T2': '1250'}
        consistent = [True]
        def fake_candidate(self, path, task, record, candidate_id=None):
            record.update(quality_status='passed', format_status='valid', plan={'kind': 'values'},
                          quality_rounds=[{'status': 'accepted'}])
            return candidate_answers[candidate_id]
        def fake_consensus(self, phase, prompt, data, images=()):
            return {'plan_valid': consistent[0], 'plan_reason': '测试语义一致性', 'structure_valid': True,
                    'structure_confidence': .99, 'checks': []}
        with patch.object(base, 'CACHE_DIR', root / 'candidate-cache'):
            candidate_pipeline = Pipeline(None, root / 'candidate-run', base)
        with patch.object(Pipeline, 'solve', fake_candidate), patch.object(Pipeline, 'crops', return_value=[]), \
                patch.object(Pipeline, 'ask', fake_consensus):
            result = {}
            assert candidate_pipeline.solve_candidates('sample.png', question, result, entry, ['T1', 'T2'], question, 0) == '1250'
            assert len(result['candidate_runs']) == 2 and result['quality_status'] == 'passed'
            candidate_answers['T2'] = '2000'
            result = {}
            assert candidate_pipeline.solve_candidates('sample.png', question, result, entry, ['T1', 'T2'], question, 0) == ''
            assert '冲突' in result['unanswered_reason'] and len(result['candidate_runs']) == 2
            candidate_answers['T2'] = '1250'; consistent[0] = False
            result = {}
            assert candidate_pipeline.solve_candidates('sample.png', question, result, entry, ['T1', 'T2'], question, 0) == ''
            assert '交叉核验未通过' in result['unanswered_reason']
            checks_passed.append('多候选必须逐表通过、答案一致且语义复核通过；冲突或语义不一致留空')
        from table_pipeline import SEMANTIC_RANK
        selection_entry = {'key': 'case', 'folder': root, 'context': [],
                           'tables': [{'table_id': 'T1', 'parts': []}, {'table_id': 'T2', 'parts': []}]}
        phases = []
        def selection_ask(self, phase, prompt, data, images=()):
            phases.append((phase, prompt == SEMANTIC_RANK))
            if phase == 'plan':
                return {'kind':'structure','all':True,'rows':[],'cols':[],'evidence_refs':[],'context_ids':[],'rules':[]}
            if phase == 'candidate-set':
                return {'candidates': ['T1', 'T2'], 'reason': '两表相关'}
            return {'status': 'selected' if data['task']['question_type'] == 'structure' else 'ambiguous',
                    'table_id': 'T2' if data['task']['question_type'] == 'structure' else None,
                    'candidates': ['T2'] if data['task']['question_type'] == 'structure' else ['T1', 'T2'],
                    'reason': '复核结果'}
        def reject_selected_quality(self, entry, region, task, table, context, plan, record, **kwargs):
            record['quality_rounds'].append({'status':'unresolved_fields','plan_valid':False,'plan_reason':'模拟原图证据不足','fields':[]})
            record['unanswered_reason']='模拟原图证据不足'
            return False,False
        with patch.object(Pipeline, 'locate', return_value=selection_entry), \
                patch.object(Pipeline, 'crops', return_value=[]), patch.object(Pipeline, 'ask', selection_ask), \
                patch.object(Pipeline, 'solve_candidates', return_value=''), \
                patch.object(Pipeline, 'recognize', return_value=table), \
                patch.object(Pipeline, 'quality', reject_selected_quality):
            for kind in ('extract', 'thinking', 'structure'):
                phases.clear(); result = {}
                candidate_pipeline.solve('sample.png', dict(question, question_type=kind, question='请处理表格'), result)
                assert sum(phase == 'candidate-set' for phase, _ in phases) == 1
                if kind == 'structure':
                    assert result['selection']['status'] == 'selected'
                    assert result.get('quality_status') != 'passed' and result['unanswered_reason']=='模拟原图证据不足'
                    assert result['visual_selection']['table_id'] == 'T2'
                    assert result['semantic_selection_conflict']['ranking_first'] == 'T1'
            checks_passed.append('各题型均保留排序；原图选择可纠正排序，但独立质检拒绝时仍留空')
        multi_entry = {'key': 'case', 'folder': root, 'context': [],
                       'tables': [dict(table, parts=[]) for table in named_tables]}
        def multi_intent_ask(self, phase, prompt, data, images=()):
            if phase == 'candidate-set':
                return {'candidates': ['T1', 'T2'], 'reason': '两表相关'}
            return {'status': 'selected', 'table_id': None, 'candidates': ['T1', 'T2'], 'reason': '题目同时指定两表'}
        with patch.object(Pipeline, 'locate', return_value=multi_entry), patch.object(Pipeline, 'crops', return_value=[]), \
                patch.object(Pipeline, 'ask', multi_intent_ask), patch.object(Pipeline, 'solve_multi_structure', return_value='') as multi:
            record = {}
            candidate_pipeline.solve('sample.png', dict(question, question_type='structure', question='恢复表1和表2的表头'), record)
            assert multi.called and record['multi_table_intent']['candidates'] == ['T1', 'T2']
            checks_passed.append('明确多表意图与视觉核验一致后进入多表处理')
        header_fixture = deepcopy(table)
        header_fixture['cells'] = [cell for cell in header_fixture['cells'] if cell['row'] < 2]
        def multi_quality(self, entry, region, task, tab, context, plan, record, **kwargs):
            record['quality_rounds'].append({'status': 'accepted', 'structure_valid': True, 'structure_confidence': .99})
            return True, False
        with patch.object(Pipeline, 'recognize_header', return_value=deepcopy(header_fixture)), \
                patch.object(Pipeline, 'quality', multi_quality):
            record = {'multi_table_intent': {'reason': '明确两表'}, 'multi_table_intent_confirmation': {'status': 'selected'}}
            candidate_pipeline.solve_multi_structure('sample.png', question, record, multi_entry, ['T1', 'T2'],
                                                     dict(question, question_type='structure', question='恢复表1和表2表头'), 0)
            assert len(record['multi_table_candidates']) == 2
            assert record['multi_table_candidates'][0]['dimension_status'] == 'unconfirmed'
            assert record['multi_table_candidates'][1]['status'] == 'validated'
            assert '多表结构数组未启用' in record['unanswered_reason']
            checks_passed.append('多表分别留候选，续段不猜全表行数，多表提交格式未确认时留空')
            complete_entry = deepcopy(multi_entry)
            complete_entry['tables'][0]['title'] = '表1'
            with patch.object(base, 'MULTI_STRUCTURE_ARRAY', True):
                complete = {'multi_table_intent': {'reason': '明确两表'}, 'multi_table_intent_confirmation': {'status': 'selected'}}
                value = candidate_pipeline.solve_multi_structure('sample.png', question, complete, complete_entry, ['T1', 'T2'],
                                                                  dict(question, question_type='structure', question='恢复表1和表2表头'), 0)
                assert len(json.loads(value)) == 2 and complete['multi_structure_format'] == 'structure_array'
                assert complete['quality_status'] == 'passed'
                incomplete = {'multi_table_intent': {'reason': '明确两表'}, 'multi_table_intent_confirmation': {'status': 'selected'}}
                sample_source = root/'sample-cross.png'; sample_source.write_bytes(b'synthetic')
                assert candidate_pipeline.solve_multi_structure(sample_source, question, incomplete, multi_entry, ['T1', 'T2'],
                                                                  dict(question, question_type='structure', question='恢复表1和表2表头'), 0) == ''
                assert '行列数' in incomplete['unanswered_reason']
                assert incomplete['local_reinspection_stop']['kind'] == 'no_executable_target'
                checks_passed.append('明确启用数组时仅完整核验的多表可输出，续段未确认仍留空')
        def cross_cell(row, col, value):
            return {'row': row, 'col': col, 'rowspan': 1, 'colspan': 1, 'text': value,
                    'state': 'value', 'source': {'part': 0, 'bbox': [0, 0, 1, 1]}, 'confidence': .99}
        cross_tables = {'T1': {'row_count': 2, 'col_count': 1, 'cells': [cross_cell(0, 0, '甲'), cross_cell(1, 0, '乙')]},
                        'T2': {'row_count': 1, 'col_count': 2, 'cells': [cross_cell(0, 0, '一班'), cross_cell(0, 1, '10')]},
                        'T3': {'row_count': 1, 'col_count': 2, 'cells': [cross_cell(0, 0, '二班'), cross_cell(0, 1, '20')]}}
        cross_entry = {'key': 'test', 'folder': root, 'context': [],
                       'tables': [{'table_id': k, 'parts': [{}]} for k in cross_tables]}
        cross_plan = {'kind': 'cross_rank', 'direction': 'max', 'separator': '，',
                      'groups': [{'parent_table': 'T1', 'parent_ref': [i, 0], 'child_table': f'T{i+2}',
                                  'rows': [0], 'label_column': 0, 'value_column': 1,
                                  'parent_evidence': [], 'child_evidence': []} for i in range(2)]}
        def cross_ask(self, phase, prompt, data, images=()):
            if phase == 'plan': return cross_plan
            return {'plan_valid': True, 'plan_reason': '位置和归属一致', 'structure_valid': True,
                    'structure_confidence': .99, 'checks': []}
        cross_repairs = [False]
        cross_quality_calls = {}
        def cross_quality(self, entry, region, task, table, context, plan, record, allow_reinspect):
            if cross_repairs[0]:
                key = region['table_id']
                cross_quality_calls[key] = cross_quality_calls.get(key, 0) + 1
            if cross_repairs[0] and region['table_id'] in ('T2', 'T3') and not record['quality_rounds']:
                assert allow_reinspect
                record['quality_rounds'].append({'status': 'replan_required', 'plan_reason': '该表关键字段待复核',
                                                 'reinspection': {'verification': {}}})
                return False, True
            if cross_repairs[0] and region['table_id'] in ('T2', 'T3'):
                assert allow_reinspect and record['quality_rounds'] and not record['local_reinspection_rounds']
            record['quality_rounds'].append({'status': 'accepted'}); return True, False
        with patch.object(Pipeline, 'recognize', lambda self, entry, region: cross_tables[region['table_id']]), \
                patch.object(Pipeline, 'crops', return_value=[]), patch.object(Pipeline, 'ask', cross_ask), \
                patch.object(Pipeline, 'quality', cross_quality):
            cross_question = dict(question, question_type='thinking', answer_format='string', table_hint='')
            result = {}
            assert candidate_pipeline.solve_cross_rank('sample.png', cross_question, result, cross_entry,
                        ['T1', 'T2', 'T3'], cross_question, 0) == '乙，二班'
            assert result['quality_status'] == 'passed' and Path(result['answer_evidence']).is_file()
            cross_tables['T3']['cells'][1]['text'] = '10'
            result = {}
            sample_source = root/'sample-cross.png'; sample_source.write_bytes(b'synthetic')
            assert candidate_pipeline.solve_cross_rank(sample_source, cross_question, result, cross_entry,
                        ['T1', 'T2', 'T3'], cross_question, 0) == '' and '并列' in result['unanswered_reason']
            assert result['local_reinspection_stop']['kind'] == 'no_executable_target'
            checks_passed.append('跨表父子关联、字段质检、程序极值与并列拒绝')
        difference_tables = {'T1': {'row_count': 2, 'col_count': 1, 'cells': [cross_cell(0, 0, '100'), cross_cell(1, 0, '50')]},
                             'T2': {'row_count': 1, 'col_count': 1, 'cells': [cross_cell(0, 0, '100')]},
                             'T3': {'row_count': 1, 'col_count': 1, 'cells': [cross_cell(0, 0, '50')]}}
        difference_plan = {'kind': 'cross_difference', 'alternatives': [
            {'left': {'table_id': 'T1', 'ref': [0, 0]}, 'right': {'table_id': 'T1', 'ref': [1, 0]}},
            {'left': {'table_id': 'T2', 'ref': [0, 0]}, 'right': {'table_id': 'T3', 'ref': [0, 0]}}],
            'evidence': [{'table_id': key, 'refs': [[0, 0]], 'context_ids': []} for key in difference_tables], 'excluded': []}
        bad_exclusion = [False]
        def difference_ask(self, phase, prompt, data, images=()):
            if phase == 'plan':
                if bad_exclusion[0] and not data['previous_feedback']:
                    return dict(difference_plan, excluded=[{'table_id': 'T1', 'reason': '误把未用列作为排除表'}])
                return difference_plan
            return {'plan_valid': True, 'plan_reason': '期间、单位与字段一致', 'structure_valid': True,
                    'structure_confidence': .99, 'checks': []}
        with patch.object(Pipeline, 'recognize', lambda self, entry, region: difference_tables[region['table_id']]), \
                patch.object(Pipeline, 'crops', return_value=[]), patch.object(Pipeline, 'ask', difference_ask), \
                patch.object(Pipeline, 'quality', cross_quality):
            difference_question = dict(question, question_type='thinking', answer_format='number', table_hint='')
            result = {}
            assert candidate_pipeline.solve_cross_difference('sample.png', difference_question, result, cross_entry,
                        ['T1', 'T2', 'T3'], difference_question, 0) == '50'
            assert len(result['cross_comparisons']) == 2 and result['quality_status'] == 'passed'
            cross_repairs[0] = True
            result = {}
            assert candidate_pipeline.solve_cross_difference('sample.png', difference_question, result, cross_entry,
                        ['T1', 'T2', 'T3'], difference_question, 0) == '50'
            assert all(len(next(x for x in result['cross_field_checks'] if x['table_id'] == key)['quality_rounds']) >= 2
                       for key in ('T2', 'T3'))
            assert cross_quality_calls.get('T1') == 1 and cross_quality_calls.get('T2') == 2 and cross_quality_calls.get('T3') == 2
            assert any(x['reused'] for x in result['cross_field_checks'])
            cross_repairs[0] = False
            bad_exclusion[0] = True
            result = {}
            assert candidate_pipeline.solve_cross_difference('sample.png', difference_question, result, cross_entry,
                        ['T1', 'T2', 'T3'], difference_question, 0) == '50'
            bad_exclusion[0] = False
            difference_tables['T3']['cells'][0]['text'] = '40'
            result = {}
            sample_source = root/'sample-cross.png'; sample_source.write_bytes(b'synthetic')
            assert candidate_pipeline.solve_cross_difference(sample_source, difference_question, result, cross_entry,
                        ['T1', 'T2', 'T3'], difference_question, 0) == '' and '不一致' in result['unanswered_reason']
            assert result['local_reinspection_stop']['kind'] == 'no_executable_target'
            checks_passed.append('单表及跨表差值分别计算，结果不一致时拒绝')
        from PIL import ImageDraw
        dense = Image.new('RGB', (2600, 1600), 'white')
        draw = ImageDraw.Draw(dense)
        for y in range(80, 1521, 40): draw.line((0, y, 2599, y), fill='black', width=3)
        dense.save(root / 'T1-part-0-normal.png')
        dense_region = {'table_id': 'T1', 'parts': [{'page': 1, 'bbox': [0, 0, 1000, 1000]}]}
        dense_entry = {'folder': root}
        def fake_block(self, entry, region):
            x1, y1, x2, y2 = region['parts'][0]['bbox']
            return {'row_count': 1, 'col_count': 2, 'structure_confidence': .99,
                    'cells': [{'row': 0, 'col': col, 'rowspan': 1, 'colspan': 1,
                               'text': str(col), 'state': 'value', 'confidence': .99,
                               'source': {'part': 0, 'bbox': [col*500, 0, (col+1)*500, 1000],
                                          'prepared_page_bbox': [x1+(x2-x1)*col/2, y1, x1+(x2-x1)*(col+1)/2, y2]}}
                              for col in range(2)]}
        with patch.object(Pipeline, 'recognize', fake_block), patch.object(Pipeline, 'crops', return_value=[]):
            combined = candidate_pipeline.recognize_dense_blocks(dense_entry, dense_region)
            assert combined and combined['row_count'] >= 3 and combined['col_count'] == 2
            assert all(c['source']['part'] == 0 and c['row'] < combined['row_count'] for c in combined['cells'])
            assert list((root / 'candidate-run').glob('blocks-T1-*.json'))
            checks_passed.append('整行横线分块、来源坐标投影及拼接网格校验')
        def mock_page(self, entry, region):
            page_number = region['parts'][0]['page']
            values = [('名称', '金额'), ('甲' if page_number == 1 else '乙', '1' if page_number == 1 else '2')]
            return {'row_count': 2, 'col_count': 2, 'structure_confidence': .99,
                    'cells': [dict(cross_cell(row, col, value), source={'part': 0, 'bbox': [0, 0, 100, 100]})
                              for row, cells in enumerate(values) for col, value in enumerate(cells)]}
        multi_region = {'table_id': 'T1', 'parts': [{'page': 1, 'bbox': [0, 0, 1000, 1000]},
                                                     {'page': 2, 'bbox': [0, 0, 1000, 1000]}]}
        with patch.object(Pipeline, 'recognize', mock_page), patch.object(Pipeline, 'crops', return_value=[]), \
                patch.object(Pipeline, 'ask', fake_consensus):
            consistent[0] = True
            combined = candidate_pipeline.recognize_multipage({}, multi_region)
            assert combined['row_count'] == 3 and combined['col_count'] == 2
            assert next(c for c in combined['cells'] if c['text'] == '乙')['source']['part'] == 1
            checks_passed.append('跨页接续验证、重复表头去重和来源页保留')
        with patch.object(base, 'CACHE_DIR', root / 'cache'), patch.object(base, 'get_response', fake_response):
            first = Pipeline(None, root / 'run1', base)
            r1 = {}
            answer = first.solve(folder / 'fixtures/sales.png', question, r1)
            assert answer == '1250' and r1['quality_rounds'][0]['noncritical_low_confidence'] == ['cell:3:2']
            previous = len(calls)
            second = Pipeline(None, root / 'run2', base)
            r2 = {}
            assert second.solve(folder / 'fixtures/sales.png', question, r2) == '1250'
            assert r2['document_cache_hit'] and r2['table_cache_hit']
            assert [phase for phase, _ in calls[previous:]] == ['select', 'plan', 'verify']
            assert all(model == base.VERIFY_MODEL for phase, model in calls if phase == 'verify')
            checks_passed.append('实际入口跨实例复用识别结果、文字阶段不带图片、独立验证模型分流')
            with patch.object(base, 'TEXT_MODEL', 'qwen3.8-flash'):
                previous = len(calls)
                changed_model = Pipeline(None, root / 'run-text-model', base)
                assert changed_model.solve(folder / 'fixtures/sales.png', question, {}) == '1250'
                assert [phase for phase, _ in calls[previous:]] == ['select', 'plan', 'verify']
            with patch.object(base, 'VISION_MODEL', 'qwen3.7-plus'):
                previous = len(calls)
                assert Pipeline(None, root / 'run-vision-model', base).solve(folder / 'fixtures/sales.png', question, {}) == '1250'
                assert 'recognize' in [phase for phase, _ in calls[previous:]]
            checks_passed.append('切换文字模型复用识别缓存、切换视觉模型重新识别')
            from table_pipeline import normalize
            with patch('table_pipeline.bbox', normalize):
                assert Pipeline(None, root / 'changed-bbox', base).recognition_revision != second.recognition_revision
            checks_passed.append('边界校验实现变化时识别缓存版本变化')
            prepared = Path(r2['recognition_artifact']) / 'input/page-001-prepared.png'
            prepared.write_bytes(b'corrupted')
            rejects(lambda: Pipeline(None, root / 'run3', base).solve(folder / 'fixtures/sales.png', question, {}))
            checks_passed.append('损坏的持久缓存明确拒绝')
        reject_plan_once[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-plan'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-plan', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            # 当前口径为平均置信度严格超过阈值即通过，同时保留验证模型对计划的异议。
            assert [q['status'] for q in record['quality_rounds']] == ['accepted']
            assert record['quality_rounds'][0]['plan_valid'] is False
            checks_passed.append('平均置信度通过时保留计划异议，不追加旧式拦截')
        header_case[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-header'), patch.object(base, 'get_response', fake_response):
            record = {}
            header_question = dict(question, question_type='structure', question='恢复表头行结构', answer_format='json')
            answer = Pipeline(None, root / 'run-header', base).solve(folder / 'fixtures/sales.png', header_question, record)
            parsed = json.loads(answer)
            assert record['recognition_scope'] == 'header_with_full_dimensions'
            assert parsed['row_count'] == 4 and parsed['col_count'] == 3 and all(c['row'] < 2 for c in parsed['cells'])
            checks_passed.append('表头局部识别保留完整行列数，覆盖表头并独立核验')
        header_case[0] = False
        header_case[0] = True; header_medium[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-header-medium'), patch.object(base, 'get_response', fake_response):
            record = {}
            header_question = dict(question, question_type='structure', question='恢复表头行结构', answer_format='json')
            assert Pipeline(None, root / 'run-header-medium', base).solve(folder / 'fixtures/sales.png', header_question, record)
            result = record['quality_rounds'][-1]
            assert result['status'] == 'accepted' and result['average_confidence']['status'] == 'accepted'
            assert result['structure_confidence'] == .85
            checks_passed.append('结构单项中置信度保留记录，平均值通过时不重复核数')
        header_case[0] = False; header_medium[0] = False
        invalid_reference[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-invalid-ref'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-invalid-ref', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            assert [q['status'] for q in record['quality_rounds']] == ['replan_required', 'accepted']
            assert '程序校验计划失败' in record['quality_rounds'][0]['plan_reason']
            checks_passed.append('计划引用越界时保留程序反馈并重新规划')
        invalid_reference[0] = False
        invalid_contract[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-invalid-contract'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-invalid-contract', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            assert [q['status'] for q in record['quality_rounds']] == ['replan_required', 'accepted']
            assert '不符合契约' in record['quality_rounds'][0]['plan_reason']
            checks_passed.append('计划输出契约错误时携带反馈重规划并复核')
        invalid_contract[0] = False
        ranking_case[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-ranking'), patch.object(base, 'get_response', fake_response):
            record = {}
            ranking_question = dict(question, question_type='thinking', question='2026年销售额最高的是哪个产品？', answer_format='string')
            assert Pipeline(None, root / 'run-ranking', base).solve(folder / 'fixtures/sales.png', ranking_question, record) == 'Alpha'
            assert [q['status'] for q in record['quality_rounds']] == ['replan_required', 'accepted']
            assert record['plan']['expressions'][0]['op'] == 'query'
            checks_passed.append('排名题静态取值被拒绝，改为实际排序后再验证输出')
        ranking_cell[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-ranking-cell'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-ranking-cell', base).solve(folder / 'fixtures/sales.png', ranking_question, record) == 'Alpha'
            assert [q['status'] for q in record['quality_rounds']] == ['replan_required', 'accepted']
            checks_passed.append('排名题伪装为单个计算表达式时也必须重新规划')
        ranking_cell[0] = False
        ranking_case[0] = False
        preview_invalid[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-preview'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-preview', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            assert [q['status'] for q in record['quality_rounds']] == ['replan_required', 'accepted']
            assert '执行计划不能产生有效答案' in record['quality_rounds'][0]['plan_reason']
            checks_passed.append('字段已获确认但计划执行失败时重新规划，不误判所有字段')
        preview_invalid[0] = False
        preview_invalid[0] = True
        preview_conflict[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-preview-conflict'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-preview-conflict', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            first = record['quality_rounds'][0]
            assert first['status'] == 'replan_required' and '执行计划不能产生有效答案' in first['plan_reason']
            verified = record['quality_rounds'][-1]
            assert any(x['id'] == 'cell:2:2' and not x['verification']['consistent'] for x in verified['fields'])
            checks_passed.append('先修正执行错误，再保留关键字段冲突及平均置信度路由记录')
        preview_invalid[0] = False
        low_target[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-low'), patch.object(base, 'get_response', fake_response):
            r3 = {}
            assert Pipeline(None, root / 'run-low', base).solve(folder / 'fixtures/sales.png', question, r3) == '1250'
            assert [r['status'] for r in r3['quality_rounds']] == ['replan_required', 'accepted']
            assert list((root / 'run-low').glob('review-*-cell-2-2.png'))
            checks_passed.append('低置信度关键字段局部放大、双模型复核、重新规划后实际消费修正结果')
        low_target[0] = False
        low_target[0] = True
        position_conflict[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-position'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-position', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            assert [q['status'] for q in record['quality_rounds']] == ['structure_reinspected', 'accepted']
            checks_passed.append('局部识别与单元格状态冲突时对照整表重新定位再验证')
        low_target[0] = False
        medium_target[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-medium'), patch.object(base, 'get_response', fake_response):
            record = {}
            medium_pipeline = Pipeline(None, root / 'run-medium', base)
            assert medium_pipeline.solve(folder / 'fixtures/sales.png', question, record) == '1250'
            assert record['quality_rounds'][-1]['status'] == 'accepted'
            field = next(f for f in record['quality_rounds'][-1]['fields'] if f['id'] == 'cell:2:2')
            assert field['verification']['confidence'] == .99
            evidence = json.loads(Path(record['answer_evidence']).read_text())
            entry = next(iter(medium_pipeline.catalogs.values()))
            task = {k: question.get(k) or '' for k in ('question_type', 'question', 'table_hint', 'answer_format')}
            accepted, changed = medium_pipeline.quality(entry, entry['tables'][0], task, evidence['table'],
                                                        evidence['context'], evidence['plan'], record, False)
            assert accepted and not changed
            checks_passed.append('验证平均值通过后保留识别与验证分数')
        medium_target[0] = False
        low_target[0] = True
        unreadable[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-unreadable'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-unreadable', base).solve(folder / 'fixtures/sales.png', question, record) == ''
            assert record['quality_rounds'][-1]['status'] == 'unresolved_fields'
            checks_passed.append('关键字段复核后仍未知时保留空答案且保存原因')
        low_target[0] = False
        unreadable[0] = False
        bad_structure[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-structure'), patch.object(base, 'get_response', fake_response):
            previous = len(calls)
            assert Pipeline(None, root / 'run-structure', base).solve(folder / 'fixtures/sales.png', question, {}) == '1250'
            assert 'structure-reinspect' in [phase for phase, _ in calls[previous:]]
            checks_passed.append('高置信度也不能通过缺格结构，先重识别再下传')

        # 同一文档中的其他表边界不确定，不应阻止目标表。
        with patch.object(base, 'CACHE_DIR', root / 'cache-boundary-isolation'):
            check = Pipeline(None, root / 'run-boundary-isolation', base)
            check.crops = lambda *args: []
            check.ask = lambda *args: {'parts': [
                {'table_id': 'T1', 'part': 0, 'complete': True, 'bbox': [0, 0, 500, 500], 'confidence': .99, 'reason': '片段'},
                {'table_id': 'T1', 'part': 0, 'complete': True, 'bbox': [0, 0, 500, 500], 'confidence': .99, 'reason': '重复'},
                {'table_id': 'T2', 'part': 0, 'complete': True, 'bbox': [500, 0, 1000, 500], 'confidence': .99, 'reason': '完整'}]}
            entry = {'tables': [{'table_id': 'T1', 'parts': [{'page': 1, 'bbox': [0,0,500,500]}]},
                                {'table_id': 'T2', 'parts': [{'page': 1, 'bbox': [500,0,1000,500]}]}]}
            check.repair_boundaries(entry, [])
            assert '重复输出' in entry['tables'][0]['parts'][0]['boundary_error']
            assert 'boundary_error' not in entry['tables'][1]['parts'][0]
            checks_passed.append('边界重复或不确定时仅阻断该目标表，不阻断同文档其他表')
        bad_boundary[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-boundary'), patch.object(base, 'get_response', fake_response):
            previous = len(calls)
            record = {}
            assert Pipeline(None, root / 'run-boundary', base).solve(folder / 'fixtures/sales.png', question, record) == ''
            assert 'recognize' not in [phase for phase, _ in calls[previous:]]
            assert '目标表边界无法确认' in record['unanswered_reason']
            checks_passed.append('裁剪边界无法确认时先阻止识别，不把残图送给后续解题')
        scope_pass[0] = True
        with patch.object(base, 'CACHE_DIR', root / 'cache-boundary-scope'), patch.object(base, 'get_response', fake_response):
            record = {}
            assert Pipeline(None, root / 'run-boundary-scope', base).solve(folder / 'fixtures/sales.png', question, record) == '1250'
            assert record['boundary_scope']['plan_valid'] and record['quality_rounds'][-1]['status'] == 'accepted'
            checks_passed.append('非结构题仅在所需可见范围独立核验通过后处理边界疑点')
        questions = base.read_questions(base.TESTS_PATH)
        submission = root / 'submission.xlsx'
        base.write_submission(questions, {str(questions[0]['id']): '=literal'}, submission)
        workbook = load_workbook(submission, data_only=False)
        rows = list(workbook.active.iter_rows(values_only=True))
        assert rows[0] == ('id', 'answer') and len(rows) == len(questions) + 1
        assert [r[0] for r in rows[1:]] == [q['id'] for q in questions]
        assert workbook.active['B2'].data_type == 's' and rows[1][1] == '=literal'
        workbook.close()
        checks_passed.append('题目 ID 完整保留、Excel回读及公式注入保护')
    report = {'validation_level': 'deterministic', 'passed': checks_passed, 'execution_checks': ['合并单元格局部恢复', '真实空白', '确定性加总', '筛选排序', '增长率', '单位换算与舍入', '表外引用', '未知值拒绝'],
              'model_accuracy_claim': False, 'thresholds_calibrated': False}
    atomic_json(folder / 'contract_validation.json', report)
    print(f'契约与路由验收通过：{len(checks_passed)}组，证据：{folder / "contract_validation.json"}')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--online',action='store_true')
    parser.add_argument('--cases',nargs='+')
    parser.add_argument('--engine',choices=['both','direct','structured'],default='both')
    args=parser.parse_args()
    folder=base.OUTPUT_DIR/'synthetic_evaluation'
    run=folder/datetime.now().strftime('%Y%m%d-%H%M%S-%f');run.mkdir(parents=True)
    fixtures=run/'fixtures'
    cases=build(fixtures);offline(cases);quality_offline(cases, run)
    if not args.online:return
    if not base.API_KEY:raise ValueError('请设置DASHSCOPE_API_KEY')
    if args.cases:cases=[q for q in cases if q['id'] in args.cases]
    (run/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'评测目录：{run}',flush=True)
    reports=[]
    with OpenAI(api_key=base.API_KEY,base_url=base.BASE_URL,timeout=base.TIMEOUT_SECONDS,max_retries=0) as client:
        pipeline=Pipeline(client,run/'pipeline',base)
        engines=['direct','structured'] if args.engine=='both' else [args.engine]
        for engine in engines:
            for case in cases:
                # 标准答案绝不进入模型请求或结构化流程。
                question={k:v for k,v in case.items() if k!='gold'}
                record={'id':case['id'],'engine':engine}
                try:
                    path=fixtures/case['file_name']
                    if engine=='structured':answer=pipeline.solve(path,question,record)
                    else:
                        task={k:question[k] for k in ('question_type','question','table_hint','answer_format')}
                        response=base.get_response(client,[{'role':'system','content':base.SYSTEM_PROMPT},{'role':'user','content':[{'type':'text','text':json.dumps(task,ensure_ascii=False)},*base.document_content(path)]}],record)
                        record.update(raw_output=response['content'],usage=response['usage'])
                        if response['finish_reason']!='stop':raise ValueError('输出未完整结束')
                        answer=base.format_answer(response['content'],case['question_type'],case['answer_format'])
                    record.update(answer=answer,correct=score(answer,case))
                except Exception as error:
                    record.update(correct=False,error_type=type(error).__name__,error=str(error).replace(base.API_KEY,'[REDACTED]'))
                reports.append(record)
                base.write_submission(cases, {r['id']: r.get('answer', '') for r in reports if r['engine'] == engine}, run / f'{engine}-submission.xlsx')
                (run/'results.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2).replace(base.API_KEY,'[REDACTED]'))
                print(engine,case['id'],'通过' if record['correct'] else '未通过',flush=True)
    summary={e:{'correct':sum(r['correct'] for r in reports if r['engine']==e),'total':sum(r['engine']==e for r in reports)} for e in engines}
    (run/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(summary)


if __name__=='__main__':main()
