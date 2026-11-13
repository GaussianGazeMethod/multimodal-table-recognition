# -*- coding: utf-8 -*-
"""导入文档解析原始结果，复用原解题入口；缺失源分数不冒充低分或高分。"""
import hashlib
import inspect
import io
from collections import Counter
from PIL import Image, ImageDraw
import json
import os
import shutil
import re
import tempfile
from parser_html import Grid
from pipeline_contracts import VERSION
from copy import deepcopy
from pathlib import Path
from input_quality import prepare
from quality_routing import dependencies, evaluate_rules, verified_match, structure_cells, cell_at, cell_id, plain_math_text, average_confidence_route
from table_pipeline import Pipeline, PLAN, VERIFY, REINSPECT, atomic_json, execute, number, normalize, verification_view, bbox, UncertainAnswer, preparation_dependencies

CONTRACT = 'docparser-facts-v2'

def validate_prepared_import(spec, source_hash, base):
    if not spec.get('prepared_manifest'):
        return None
    prepared_path = Path(spec['prepared_manifest']).resolve()
    if digest(prepared_path) != spec['configuration'].get('preparation_sha256'):
        raise ValueError('预处理记录哈希不匹配')
    prepared = json.loads(prepared_path.read_text())
    if (prepared.get('source_sha256') != source_hash or
            prepared.get('dependencies') != preparation_dependencies(base) or
            prepared.get('parser_input_sha256') != spec['configuration'].get('parser_input_sha256')):
        raise ValueError('预处理来源或依赖不匹配')
    if digest(Path(prepared['parser_input'])) != prepared['parser_input_sha256']:
        raise ValueError('文档解析输入哈希不匹配')
    source_dir = Path(prepared['quality_dir']).resolve()
    if source_dir != prepared_path.parent:
        raise ValueError('预处理记录目录不匹配')
    for name, expected in prepared['artifacts'].items():
        item = (source_dir/name).resolve()
        if item.parent != source_dir or digest(item) != expected:
            raise ValueError('预处理页面损坏或路径越界')
    return prepared

def raw_numeric_footnotes(targets, imports, source_hash):
    """只从原始解析中的明确上标标记构造待核验候选，不改写单元格原文。"""
    found = []
    for key, item in targets.items():
        source = item.get('source', {})
        if 'row' not in item or item.get('state') != 'value' or source.get('document_sha256') != source_hash:
            continue
        specs = [spec for spec in imports if spec.get('document_sha256') == source_hash
                 and spec.get('raw_sha256') == source.get('raw_sha256')]
        if len(specs) != 1:
            continue
        path = Path(specs[0]['raw_path'])
        if not path.is_file() or digest(path) != specs[0]['raw_sha256']:
            raise ValueError('上标候选的原始解析来源不匹配')
        blocks = [block for block in json.loads(path.read_text())['Data']['layouts']
                  if block.get('uniqueId') == source.get('layout_id')]
        if len(blocks) != 1:
            continue
        cells = [cell for cell in blocks[0].get('cells', []) if cell.get('cellId') == source.get('cell_id')]
        if len(cells) != 1:
            continue
        raw = ''.join(part.get('text', '') for part in cells[0].get('layouts', [])).strip()
        match = re.fullmatch(r'([+\-−]?\d[\d,]*(?:\.\d+)?%?)\s*<sup>\s*([A-Za-z])\s*</sup>', raw, re.I)
        if not match:
            continue
        numeric, marker = match.groups()
        if item['text'].strip() not in (raw, numeric + marker):
            continue
        found.append({'target_id':key, 'ref':[item['row'],item['col']], 'numeric_text':numeric,
                      'marker':marker, 'raw_text':raw, 'source':deepcopy(source)})
    return found

def numeric_expression_targets(targets, plan):
    """上标复核只覆盖实际数值运算引用，不扩展到无关表头或补充证据。"""
    refs = set()
    def visit(value):
        if isinstance(value, dict):
            if value.get('op') == 'cell' and isinstance(value.get('ref'), list) and len(value['ref']) == 2:
                refs.add(tuple(value['ref']))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(plan.get('expressions', []))
    return {key:item for key,item in targets.items() if 'row' in item and (item['row'],item['col']) in refs}

def quality_failure_reason(verdict, fields, rules, high):
    """按实际未通过的环节记录原因，避免把计划通过说明误当阻断原因。"""
    reasons = []
    if not verdict['plan_valid']:
        reasons.append('计划未通过：' + (verdict['plan_reason'] or '未提供具体原因'))
    if not verdict['structure_valid']:
        reasons.append('题目依赖的结构或位置未通过原图核验')
    elif verdict['structure_confidence'] <= high:
        reasons.append(f"结构置信度 {verdict['structure_confidence']} 未超过阈值 {high}")
    for field in fields:
        if field['route'] != 'accepted':
            reasons.append('关键证据 ' + field['id'] + ' 未通过：' + (field['verification'].get('reason') or '文字、位置、跨度或置信度不一致'))
    for rule in rules:
        if rule.get('state') == 'failed':
            reasons.append('适用规则未通过：' + str(rule.get('reason') or rule.get('id') or rule))
    return '；'.join(reasons) or '关键证据未通过原图核验'


def handwriting_ambiguity(task, observation):
    """多个不同手写数值且题干无定位线索时，不能由模型任选一个。"""
    if task.get('answer_format')=='json_array':return None
    numeric=[]
    for item in observation.get('items',[]):
        try:numeric.append(number(item['text']))
        except (ValueError,ArithmeticError):continue
    if len(set(numeric))<=1:return None
    text=task.get('question','')
    residual=re.sub(r'手写批注|手写标注|手写|批注|请提取|请问|提取|对应|数值|数字|是什么|是多少|什么|多少|的|值|[？?：:，,。\s]','',text)
    if not residual:return '原图存在多个不同的手写数值，题目未指定位置或字段，不能任选一个'
    return None


def header_review_required(table, plan, targets, checks, high):
    """仅针对要求的表头位置判断是否需要结构复核。"""
    if plan.get('kind') != 'structure' or plan.get('all') or not plan.get('rows') or plan.get('cols'):
        return False
    rows = plan['rows']
    cells = [cell for cell in table.get('cells', []) if cell.get('row') in rows
             and cell.get('state') == 'value' and cell.get('colspan') == 1]
    repeated = any(left['row'] == right['row'] and right['col'] == left['col'] + 1
                   and normalize(left['text']) == normalize(right['text']) for left in cells for right in cells)
    conflicts = any('row' in targets[key] and targets[key]['row'] in rows
                    and (not check.get('span_consistent', True)
                         or (check['confidence'] > high and check['position_consistent']
                             and check['state'] != 'unknown' and not verified_match(targets[key], check)))
                    for key, check in checks.items())
    return repeated or conflicts

def mixed_visual_count_error(task):
    """多结果任务不得将一个混合观察计数解释为任一属性。"""
    text=task.get('question','')
    return (task.get('answer_format')=='json_array'
            and bool(re.search(r'以下字段|分别|、|[，,].*[，,]',text)))

def ordered_visual_attributes(task):
    """仅识别题目明确按顺序列出的多个是非属性。"""
    if task.get('answer_format')!='json_array':return []
    match=re.search(r'以下字段(?:的答案)?[：:]\s*(.+?)[。？?]?$',task.get('question',''))
    if not match:return []
    attrs=[x.strip() for x in re.split(r'[、，,]',match.group(1)) if x.strip()]
    return attrs if 2<=len(attrs)<=8 and len(set(attrs))==len(attrs) and all(x.startswith('是否') for x in attrs) else []

def is_visual_count_task(text):
    """区分询问对象个数与询问是否存在名为数量的列。"""
    return bool(re.search(r'多少|几个|有几|列数|数量(?:为|是|多少)|统计.{0,4}数量',text))

def multiple_attribute_plan_error(task,plan):
    attrs=ordered_visual_attributes(task)
    if not attrs:return '无法确认多属性顺序'
    expressions=plan.get('expressions',[])
    if plan.get('kind')!='calculate' or not plan.get('as_array') or len(expressions)!=len(attrs):return '多属性必须逐项程序判断并输出数组'
    for index,expr in enumerate(expressions):
        if (expr.get('op')!='text_equals' or expr.get('ref')!={'context_id':f'VISUAL_CANDIDATE_COUNT_{index}'}
            or expr.get('expected')!='0' or expr.get('yes')!='否' or expr.get('no')!='是'):
            return '多属性计划未按题目顺序分别引用对应证据'
    return None

def multiple_fields_join_error(task,plan):
    """明确列出多个字段并要求数组时，不能把各项拼成一个数组元素。"""
    question = task.get('question', '')
    if task.get('answer_format') != 'json_array' or not re.search(r'列出以下字段|以下字段的答案', question):
        return None
    expressions = plan.get('expressions', [])
    if (plan.get('kind') == 'calculate' and plan.get('as_array') and len(expressions) == 1
            and expressions[0].get('op') == 'join' and len(expressions[0].get('args', [])) > 1):
        return '题目要求多个字段的数组，不能将多个字段拼接为一个数组元素；请逐项按题目顺序生成表达式。'
    return None

def one_item_as_count_error(task, plan):
    """逐项数组中要求某类之一时，禁止把该项改成类型个数。"""
    if task.get('answer_format') != 'json_array' or plan.get('kind') != 'calculate' or not plan.get('as_array'):
        return None
    match = re.search(r'以下字段的答案\s*[:：]\s*(.+)', task.get('question', ''))
    if not match:
        return None
    items = [item.strip(' 。？?') for item in re.split(r'[、，,]', match.group(1)) if item.strip(' 。？?')]
    expressions = plan.get('expressions', [])
    if len(items) != len(expressions):
        return None
    for index, (item, expression) in enumerate(zip(items, expressions)):
        if '之一' in item and expression.get('op') == 'query':
            if expression.get('aggregate') in ('count', 'distinct_count'):
                return f'第{index+1}项要求给出一个具体对象，计划却计算数量；请输出符合范围的具体值并保留原图核验。'
            if expression.get('aggregate') == 'values':
                return f'第{index+1}项要求一个具体值，查询返回列表会嵌套数组；请引用可核验的单个对象。'
    return None

def section_cell_mismatch_error(task, plan, table):
    """只在明确识别多个整行分区标题时，检查逐项直接引用是否位于所问分区。"""
    if task.get('answer_format') != 'json_array' or plan.get('kind') != 'calculate' or not plan.get('as_array'):
        return None
    match = re.search(r'以下字段的答案\s*[:：]\s*(.+)', task.get('question', ''))
    if not match:
        return None
    items = [item.strip(' 。？?') for item in re.split(r'[、，,]', match.group(1)) if item.strip(' 。？?')]
    expressions = plan.get('expressions', [])
    if len(items) != len(expressions):
        return None
    counts = {}
    for cell in table.get('cells', []):
        if cell.get('state') != 'value' or not cell.get('text', '').strip():
            continue
        label = normalize(plain_math_text(cell['text'])).casefold()
        if label:
            key = (cell['row'], label)
            counts[key] = counts.get(key, 0) + cell['colspan']
    headings = sorted((row, label) for (row, label), count in counts.items()
                      if count >= 2 and count * 2 >= table['col_count'])
    if len({label for _, label in headings}) < 2:
        return None
    for index, (item, expr) in enumerate(zip(items, expressions)):
        ref = expr.get('ref')
        if expr.get('op') not in ('cell', 'text') or not isinstance(ref, list) or len(ref) != 2:
            continue
        wanted = {label for _, label in headings if label in normalize(item).casefold()}
        preceding = [(row, label) for row, label in headings if row < ref[0]]
        if len(wanted) == 1 and preceding:
            last_row = max(row for row, _ in preceding)
            active = {label for row, label in preceding if row == last_row}
            if len(active) == 1 and active != wanted:
                return f'第{index+1}项引用的单元格位于其他明确分区，请重新定位题目指定分区并核验原图。'
    return None

def header_boolean_as_count_error(task, plan):
    """多级表头的真假不能直接推导分组、类别或区块的个数。"""
    if not re.search(r'(?:分组|类别|区块|表格|指标)(?:的)?数量', task.get('question', '')):
        return None
    for expr in plan.get('expressions', []):
        if (expr.get('op') == 'has_multi_header'
                and str(expr.get('yes', '')).isdigit() and str(expr.get('no', '')).isdigit()):
            return '是否存在多级表头不能直接推导分组或区块数量；请依据完整范围逐项计数。'
    return None

def visual_column_plan_error(plan):
    ref={'context_id':'VISUAL_CANDIDATE_COUNT'}
    if plan.get('evidence_refs') or plan.get('rules'):return '逐列候选已覆盖列结构；不得加入有冲突的解析网格单元格作为依赖'
    if plan.get('kind')=='values' and not plan.get('as_array') and plan.get('refs')==[ref]:return None
    if (plan.get('kind')=='calculate' and not plan.get('as_array') and plan.get('scale')=='1'
        and not plan.get('percent') and len(plan.get('expressions',[]))==1
        and plan['expressions'][0]=={'op':'cell','ref':ref}):return None
    return '列结构冲突后必须由程序使用已独立核验的逐列候选计数，不能再数解析网格的任意子集'

def single_numeric_cell_ref(task,plan):
    """仅对直接提取一个数值单元格的计划允许关系核验。"""
    if task.get('answer_format')!='number' or plan.get('as_array'):return None
    if plan.get('kind')=='values' and len(plan.get('refs',[]))==1:ref=plan['refs'][0]
    elif (plan.get('kind')=='calculate' and len(plan.get('expressions',[]))==1
          and plan['expressions'][0].get('op')=='cell' and plan.get('scale')=='1'
          and plan.get('decimals') is None and not plan.get('percent')):ref=plan['expressions'][0]['ref']
    else:return None
    return ref if isinstance(ref,list) and len(ref)==2 else None

def mean_numeric_cell_refs(task,plan):
    """多值数值提取或均值需逐项核验独立单元格；均值除数按个数计算。"""
    if task.get('answer_format')=='json_array' and plan.get('kind')=='values' and plan.get('as_array') and not plan.get('rules'):
        refs=plan.get('refs',[])
        if len(refs)>=2 and all(isinstance(x,list) and len(x)==2 for x in refs) and len({tuple(x) for x in refs})==len(refs):return refs
        return []
    expressions=plan.get('expressions',[])
    if (task.get('answer_format')!='number' or plan.get('kind')!='calculate' or plan.get('as_array')
        or plan.get('rules') or len(expressions)!=1 or expressions[0].get('op')!='mean'):return []
    args=expressions[0].get('args',[])
    if len(args)<2 or any(x.get('op')!='cell' or not isinstance(x.get('ref'),list) or len(x['ref'])!=2 for x in args):return []
    refs=[x['ref'] for x in args]
    return refs if len({tuple(x) for x in refs})==len(refs) else []

def existence_attribute_route(question, texts):
    """仅在所引完整字段不存在时，把“是否含”识别为待核验属性。"""
    text=question.get('question','')
    match=re.search(r'[“"「](是否(?:含有|包含|含)[^”"」]+)[”"」]',text)
    if not match:
        return False
    label=normalize(match.group(1)).casefold()
    # 如果解析结果已有该字段，优先保留取其原值的任务，不能替换成视觉判断。
    return not any(normalize(str(value or '')).casefold()==label for value in texts)


def conditional_selection_error(task, plan):
    """条件列表必须保留程序执行的筛选操作，不能由模型提前选定结果。"""
    text=task.get('question','')
    if not (re.search(r'(?:不支持|支持).{0,12}(?:工具|产品|对象)|(?:工具|产品|对象).{0,12}(?:不支持|支持)',text)
            and plan.get('kind')=='calculate'):return None
    def operations(value):
        if isinstance(value,dict):
            return ({value['op']} if isinstance(value.get('op'),str) else set()).union(*(operations(v) for v in value.values()))
        if isinstance(value,list):return set().union(*(operations(v) for v in value))
        return set()
    if {'select_column_labels','query'} & operations(plan):return None
    return '条件筛选题必须由程序遍历全部候选列或行并按单元格值筛选；不能由模型预选标题后直接拼接。横向用select_column_labels，纵向用query。'


def ruled_grid(image_path, table):
    """只接受边界完整且数量精确匹配的实线网格；不根据题号猜坐标。"""
    image=Image.open(image_path).convert('RGB');width,height=image.size;px=image.load()
    points=table.get('source',{}).get('parser_position')
    if not isinstance(points,list) or len(points)<4:raise ValueError('缺少可核验的表格原始位置')
    xs=[int(v['x']) for v in points];ys=[int(v['y']) for v in points]
    x0=max(0,min(xs)-8);x1=min(width-1,max(xs)+8);y0=max(0,min(ys)-8);y1=min(height-1,max(ys)+8)
    if x1-x0<40 or y1-y0<40:raise ValueError('表格位置超出原图或尺寸不足')
    background=Counter(px[x,y] for x in range(x0,x1,8) for y in range(y0,y1,8)).most_common(1)[0][0]
    def differs(pixel):return max(abs(pixel[i]-background[i]) for i in range(3))>=25
    sample_x=range(x0,x1+1,3);sample_y=range(y0,y1+1,3)
    horizontal=[y for y in range(y0,y1+1) if sum(differs(px[x,y]) for x in sample_x)/len(sample_x)>.65]
    vertical=[x for x in range(x0,x1+1) if sum(differs(px[x,y]) for y in sample_y)/len(sample_y)>.65]
    def clusters(values):
        out=[]
        for value in values:
            if out and value<=out[-1][-1]+1:out[-1].append(value)
            else:out.append([value])
        return [round(sum(group)/len(group)) for group in out]
    rows,cols=clusters(horizontal),clusters(vertical)
    if len(rows)!=table['row_count']+1 or len(cols)!=table['col_count']+1:
        raise ValueError(f"表格实线边界不足或不唯一：实际{len(rows)}行线/{len(cols)}列线，要求{table['row_count']+1}/{table['col_count']+1}")
    if any(b-a<5 for a,b in zip(rows,rows[1:])) or any(b-a<5 for a,b in zip(cols,cols[1:])):
        raise ValueError('表格网格边界过密，不能可靠定位')
    return {'rows':rows,'cols':cols,'image_size':[width,height], 'method':'pixel_grid_lines_v1'}


def icon_contact_sheet(image_path, grid, cells, output):
    """以已核验网格提取单元格接触图；同时保留完整原图。"""
    original=Image.open(image_path).convert('RGB');tw,th=210,115;columns=5
    count=len(cells);sheet=Image.new('RGB',(tw*columns,th*((count+columns-1)//columns)),(255,255,255));draw=ImageDraw.Draw(sheet)
    for idx,cell in enumerate(cells):
        row,col=cell['row'],cell['col'];box=[grid['cols'][col]+1,grid['rows'][row]+1,grid['cols'][col+1],grid['rows'][row+1]]
        crop=original.crop(tuple(box));crop.thumbnail((tw-8,th-25),Image.Resampling.LANCZOS)
        x=(idx%columns)*tw;y=(idx//columns)*th;sheet.paste(crop,(x+4,y+22));draw.text((x+4,y+3),f"cell:{row}:{col}",fill=(0,0,0))
    sheet.save(output,format='PNG');return output


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def convert(raw, source_hash, raw_hash, configuration):
    """转换网格索引并保留原始区域；不把未覆盖网格或无文本强行解释为空白。"""
    tables, context, title = [], [], ''
    blocks=[]
    for original in raw['Data']['layouts']:
        html=original.get('text','')
        fragments=re.findall(r'<table\b[^>]*>.*?</table>',html,re.I|re.S) if original['type']!='table' else []
        if not fragments:
            blocks.append(original);continue
        for part,fragment in enumerate(fragments):
            grid=Grid();grid.feed(fragment);grid.close()
            if not grid.cells:raise ValueError('HTML表格没有可转换单元格')
            block=deepcopy(original);block.update(type='table',uniqueId=str(original.get('uniqueId'))+'-html-'+str(part),numRow=max(r for r,c in grid.occupied)+1,numCol=max(c for r,c in grid.occupied)+1,cells=[{'ysc':c['row'],'xsc':c['col'],'yec':c['row']+c['rowspan']-1,'xec':c['col']+c['colspan']-1,'cellId':i,'layouts':[{'text':c['text']}]} for i,c in enumerate(grid.cells)])
            blocks.append(block)
        remainder=re.sub(r'<table\b[^>]*>.*?</table>','',html,flags=re.I|re.S).strip()
        if remainder:
            rest=deepcopy(original);rest['text']=remainder;blocks.append(rest)
    for index, block in enumerate(blocks):
        page = block.get('pageNum')
        if type(page) is not int or page < 0:
            raise ValueError('解析来源页码无效')
        source = {'page': page + 1, 'layout_id': block.get('uniqueId'),
                  'parser_position': block.get('pos'), 'scope': 'page',
                  'document_sha256': source_hash, 'raw_sha256': raw_hash}
        if block['type'] != 'table':
            text = block.get('text', '').strip()
            if block['type'] == 'table_name':
                title = text
            if text:
                context.append({'id': f'C{index}', 'text': text, 'state': 'value', 'confidence': None,
                                'kind': block['type'], 'table_ids': [], 'source': source})
            continue
        table = {'table_id': f'T{len(tables)+1}', 'title': title, 'row_count': block['numRow'],
                 'col_count': block['numCol'], 'structure_confidence': None, 'cells': [],
                 'source': source, 'issues': [], 'uncovered_positions': []}
        grid = {}
        for cell in block.get('cells', []):
            row, col, end_row, end_col = (cell[k] for k in ('ysc','xsc','yec','xec'))
            if (any(type(v) is not int for v in (row,col,end_row,end_col)) or
                    not 0 <= row <= end_row < table['row_count'] or
                    not 0 <= col <= end_col < table['col_count']):
                table['issues'].append({'kind': 'out_of_bounds', 'cell_id': cell.get('cellId')})
                continue
            text = ''.join(p.get('text','') for p in cell.get('layouts',[])).strip()
            item = {'text': text, 'row': row, 'col': col, 'rowspan': end_row-row+1,
                    'colspan': end_col-col+1, 'state': 'value' if text else 'unknown', 'confidence': None,
                    'source': dict(source, cell_id=cell.get('cellId'), parser_position=cell.get('pos'))}
            for r in range(row,end_row+1):
                for c in range(col,end_col+1):
                    if (r,c) in grid:
                        table['issues'].append({'kind':'overlap','position':[r,c]})
                    grid[r,c] = True
            table['cells'].append(item)
        table['uncovered_positions'] = [[r,c] for r in range(table['row_count'])
                                        for c in range(table['col_count']) if (r,c) not in grid]
        tables.append(table)
    return {'contract': CONTRACT, 'document_sha256': source_hash, 'raw_sha256': raw_hash,
            'parser_configuration': configuration, 'tables': tables, 'context': context, 'table_relations': [],
            'source_policy': 'parser_position保留原服务坐标；未确认变换时只用页面来源核验，不伪造单元格框'}


class DocumentParserPipeline(Pipeline):
    """共享选表、计划、执行、格式和CLI保存；解析事实使用原图依赖核验。"""
    def __init__(self, client, output, base, manifest):
        super().__init__(client, output, base)
        self.imports = json.loads(Path(manifest).read_text(encoding='utf-8'))['documents']
        self.icon_candidates = {}
        self.icon_verification_active = False
        self.icon_review_sheet = None
        self.revision = hashlib.sha256((self.revision + digest(__file__)).encode()).hexdigest()

    def resume_contract_review(self, path, question, record, source_record):
        """从真实验证契约失败恢复，先核查原调用，再复核有明确状态冲突的字段。"""
        started = self.call_count
        source_record = Path(source_record).resolve()
        prior = json.loads(source_record.read_text())
        if (str(prior.get('id')) != str(question['id']) or prior.get('file_name') != question['file_name']
                or not any(item.get('status') == 'unresolved_verification_contract' for item in prior.get('quality_rounds', []))):
            raise ValueError('来源不是当前题目的验证契约失败记录')
        artifact = Path(prior['answer_evidence'])
        evidence = json.loads(artifact.read_text())
        task = {key:question.get(key) or '' for key in ('question_type','question','table_hint','answer_format')}
        entry = self.locate(path)
        regions = [region for region in entry['tables'] if region['table_id'] == evidence.get('table_id')]
        if len(regions) != 1 or self.recognize(entry, regions[0]) != evidence['table']:
            raise ValueError('历史失败表格与当前解析缓存不一致')
        candidates = []
        for call_path in sorted((source_record.parent / 'pipeline').glob('call-*-verify.json')):
            call = json.loads(call_path.read_text()); data = call.get('input', {})
            if (call.get('validation_error') != '复核文本与状态不一致' or data.get('task') != task
                    or data.get('plan') != evidence['plan'] or data.get('table') != verification_view(evidence['table'])
                    or data.get('context') != evidence['context']):
                continue
            try:
                response = json.loads(call['content'])
                invalid = [check for check in response['checks'] if check.get('state') in ('empty','unknown') and check.get('text')]
            except (ValueError, KeyError, TypeError):
                continue
            if len(invalid) == 1 and invalid[0]['id'] in data.get('targets', {}):
                candidates.append((call_path, data['targets'][invalid[0]['id']], invalid[0]))
        if not candidates:
            raise ValueError('未找到可追溯的唯一字段状态冲突')
        call_path, old_target, check = candidates[0]
        key = check['id']
        entry_table = self.tables[entry['key'], regions[0]['table_id']]
        table = deepcopy(evidence['table'])
        target = cell_at(table, [old_target['row'], old_target['col']])
        if target != old_target:
            raise ValueError('历史冲突字段与表格证据不一致')
        record['prior_contract_failure'] = {'record':str(source_record), 'record_sha256':digest(source_record),
            'evidence':str(artifact), 'evidence_sha256':digest(artifact), 'call':str(call_path),
            'call_sha256':digest(call_path), 'target':key}
        self.active_question = question['question']
        if not self.reinspect_parser_field(entry, task, {key:target}, {key:check}, record):
            return ''
        if record['local_reinspection_rounds'][-1].get('status') == 'invalid':
            return ''
        self.tables[entry['key'], regions[0]['table_id']] = table
        try:
            answer = super().solve(path, question, record, candidate_id=regions[0]['table_id'])
        finally:
            self.tables[entry['key'], regions[0]['table_id']] = entry_table
        record['parser_import_manifest'] = self.imports
        record['pipeline_calls'] = self.call_count - started
        if record.get('answer_evidence'):
            saved_path = Path(record['answer_evidence']); saved = json.loads(saved_path.read_text())
            saved['parser_validation_policy'] = record.get('parser_validation_policy')
            atomic_json(saved_path, saved)
        return answer

    def resume_targeted_review(self, path, question, record, source_record):
        """复用已独立核验的执行失败记录，只重读阻断输出的字段。"""
        source_record = Path(source_record).resolve()
        checkpoint_path = source_record
        prior = json.loads(source_record.read_text())
        checkpoint = None
        if (prior.get('prior_evidence') and prior.get('quality_status') == 'not_passed'
                and prior.get('execution_status') == 'unresolved'):
            checkpoint = prior
            provenance = checkpoint['prior_evidence']
            original = json.loads(Path(provenance['artifact']).read_text())
            reconstructed = deepcopy(original)
            reconstructed.update(plan=deepcopy(checkpoint['plan']), targeted_review_provenance=deepcopy(provenance))
            for revision in checkpoint.get('local_reinspection_rounds', []):
                cell = cell_at(reconstructed['table'], [revision['before']['row'], revision['before']['col']])
                cell.update(text=revision['after']['text'], state=revision['after']['state'])
            restored = validate_targeted_review_result(checkpoint, reconstructed, question, self.base,
                                                       source_record.parent / 'pipeline', allow_unresolved=True)
            source_record = Path(provenance['record'])
            prior = json.loads(source_record.read_text())
        if (str(prior.get('id')) != str(question['id']) or prior.get('quality_status') != 'passed'
                or prior.get('execution_status') != 'unresolved'):
            raise ValueError('恢复入口只接受本题质检通过但执行未解决的记录')
        evidence_path = Path(prior['answer_evidence'])
        evidence = json.loads(evidence_path.read_text())
        validate_saved_result(prior, evidence, question, self.base, source_record.parent / 'pipeline', evidence_only=True)
        task = {key: question.get(key) or '' for key in ('question_type','question','table_hint','answer_format')}
        document_count = self.document_count
        entry = self.locate(path)
        regions = [region for region in entry['tables'] if region['table_id'] == evidence['table_id']]
        if len(regions) != 1:
            raise ValueError('原始证据表与当前解析目录不唯一匹配')
        region = regions[0]
        table, context, plan = deepcopy(evidence['table']), deepcopy(evidence['context']), deepcopy(evidence['plan'])
        record.update(recognition_artifact=str(entry['folder']), parser_import_manifest=self.imports,
                      contract_version=VERSION, revision=self.revision,
                      recognition_revision=entry.get('revision', self.recognition_revision),
                      document_cache_hit=self.document_count == document_count,
                      table_cache_hit=False, table_evidence_reused=True,
                      selection=deepcopy(prior['selection']), plan=deepcopy(plan),
                      prior_evidence={'record':str(source_record), 'record_sha256':digest(source_record),
                                      'artifact':str(evidence_path), 'artifact_sha256':digest(evidence_path)},
                      prior_quality=deepcopy(prior['quality_rounds'][-1]), quality_status='passed',
                      execution_status='unresolved', quality_rounds=[])
        started = self.call_count
        previous_checks = {x['id']:deepcopy(x['verification']) for x in prior['quality_rounds'][-1].get('fields', [])}
        prior_plan = deepcopy(plan)
        prior_structure = prior['quality_rounds'][-1].get('structure_confidence')
        if checkpoint:
            table, context, plan = restored['table'], restored['context'], restored['plan']
            previous_checks, prior_structure = restored['checks'], restored['structure_confidence']
            record.update(plan=deepcopy(plan), quality_status='not_passed',
                          quality_rounds=deepcopy(checkpoint['quality_rounds']),
                          local_reinspection_rounds=deepcopy(checkpoint['local_reinspection_rounds']),
                          targeted_verification=deepcopy(checkpoint.get('targeted_verification', [])),
                          checkpoint={'record':str(checkpoint_path), 'sha256':digest(checkpoint_path),
                                      'continued_from':str(Path(checkpoint['local_reinspection_rounds'][-1]['call']).resolve())})
        while len(record.get('local_reinspection_rounds', [])) < 3:
            targets = dependencies(table, context, plan, task['question_type'], number, normalize)
            unknown = {key:item for key,item in targets.items() if item.get('state') == 'unknown' and 'row' in item}
            if not unknown and record.get('execution_status') == 'unresolved':
                # 程序明确指出某个原文值无法参与计算时，定位该值而不是重识别整表。
                reason = record.get('unanswered_reason') or prior.get('unanswered_reason') or ''
                named = {key:item for key,item in targets.items() if 'row' in item and item.get('state') == 'value'
                         and item.get('text') and item['text'] in reason}
                if len(named) == 1:
                    unknown = named
            if not unknown and record.get('quality_status') == 'not_passed':
                # 文字已读出但位置或低置信度仍存疑时，继续复核真实疑点。
                unknown = {key:item for key,item in targets.items() if 'row' in item and key in previous_checks
                           and (previous_checks[key].get('confidence', 0) <= self.base.CONFIDENCE_HIGH
                                or not verified_match(item, previous_checks[key]))}
            if not unknown:
                reason = record.get('unanswered_reason') or '当前计划没有可定位的未知单元格，执行仍未解决'
                # 结构或计划仍存疑时不能宣称不存在复核目标并直接转入兜底。
                unresolved_structure = (record.get('quality_status') == 'not_passed' and
                    any(not item['verdict'].get('structure_valid', False)
                        for item in record.get('targeted_verification', [])))
                record['local_reinspection_stop'] = {'kind':'structure_review_required' if unresolved_structure else 'no_executable_target',
                    'reason':reason, 'source_sha256':digest(path)}
                break
            checks = {key:previous_checks.get(key, {'id':key,'text':'','state':'unknown','confidence':0,
                      'consistent':False,'position_consistent':False,'span_consistent':False,
                      'reason':'程序执行所需字段未识别'}) for key in unknown}
            if not self.reinspect_parser_field(entry, task, unknown, checks, record):
                break
            if record['local_reinspection_rounds'][-1].get('status') == 'invalid':
                record['quality_status'] = 'not_passed'
                continue
            record['stage'] = 'plan'
            plan = self.ask('plan', PLAN, {'task':task, 'table':verification_view(table), 'context':context,
                'previous_plan':plan, 'validation_feedback':'执行所需未知字段已定向重读；重新生成依赖，不得猜测或将unknown视为空白。'})
            record['plan'] = plan
            if plan.get('kind') == 'unknown':
                record['local_reinspection_stop'] = {'kind':'no_executable_target',
                    'reason':plan.get('reason') or '重新规划后没有可执行目标', 'source_sha256':digest(path)}
                break
            record['stage'] = 'quality'
            # 旧核验仅对相同输出操作和未变化字段有效；新增依赖须独立核验。
            new_targets = dependencies(table, context, plan, task['question_type'], number, normalize)
            # 补充证据引用可调整顺序或选用已覆盖字段；输出操作与实际依赖仍须完全一致。
            prior_operation = {key:value for key,value in prior_plan.items() if key != 'evidence_refs'}
            new_operation = {key:value for key,value in plan.items() if key != 'evidence_refs'}
            added_targets = set(new_targets) - set(previous_checks)
            if new_operation != prior_operation or len(added_targets) > 12:
                record.update(quality_status='not_passed', execution_status='unresolved',
                              unanswered_reason='重规划后的计划或依赖与已核验记录不同，不能复用原质检')
                break
            revision = record['local_reinspection_rounds'][-1]
            key = revision['scope'][0]
            if revision:
                focused_keys = {key} | added_targets
                focused = {name:new_targets[name] for name in new_targets if name in focused_keys}
                view = verification_view(table)
                positions = [(item['row'],item['col']) for item in focused.values() if 'row' in item]
                view['cells'] = [cell for cell in view['cells'] if any(
                    (cell['row'] <= row and cell['col'] <= col < cell['col'] + cell['colspan']) or
                    (cell['col'] <= col and cell['row'] <= row < cell['row'] + cell['rowspan'])
                    for row,col in positions)]
                data = {'task':task, 'plan':plan, 'table':view, 'context':context, 'targets':focused,
                        'scope':'仅核验变化字段及相邻行列标题，保留完整表格原图'}
                verdict = self.ask_contract('verify', VERIFY+'\n只核验targets中的单元格及其实际位置、表头和合并关系。',
                                            data, self.crops(entry, region, self.base.PDF_DPI))
                checks = self.checks_by_id(verdict['checks'], focused)
                previous_checks = {name:check for name,check in previous_checks.items() if name in new_targets}
                previous_checks.update(checks)
                record.setdefault('targeted_verification', []).append({'target':key, 'targets':list(focused), 'verdict':verdict,
                    'call':str((self.output / f'call-{self.call_count:04d}-verify.json').resolve()),
                    'sha256':digest(self.output / f'call-{self.call_count:04d}-verify.json')})
                # 保留验证模型的结构和计划异议，按约定由平均置信度统一路由。
                prior_structure = verdict['structure_confidence']
            average = average_confidence_route(previous_checks, prior_structure, self.base.CONFIDENCE_HIGH)
            record['quality_rounds'].append({'status':'accepted' if average['status']=='accepted' else 'unresolved_fields',
                'policy':'question_average_confidence_v1', 'average_confidence':average,
                'source':'复用原质检，仅重新核验变化字段',
                'fields':[{'id':key,'verification':value} for key,value in previous_checks.items()]})
            if average['status'] != 'accepted':
                record['quality_status'] = 'not_passed'
                record['unanswered_reason'] = '定向核验平均置信度未通过：' + str(average['average']) + '；' + verdict['plan_reason']
                continue
            record['quality_status'] = 'passed'
            try:
                answer = execute(table, plan, task['question_type'], context,
                                 confirmed_missing_fields=record.get('confirmed_missing_fields', []))
                formatted = self.base.format_answer(json.dumps({'answer':answer}, ensure_ascii=False),
                    task['question_type'], task['answer_format'], [table,context])
            except (UncertainAnswer, ValueError, ArithmeticError) as error:
                record.update(execution_status='unresolved', unanswered_reason=str(error))
                continue
            artifact = self.output / f'answer-evidence-{self.call_count:04d}.json'
            atomic_json(artifact, {'contract_version':VERSION, 'revision':self.revision, 'source_document':str(path),
                                   'table_id':region['table_id'], 'table':table, 'context':context,
                                   'plan':plan, 'quality_rounds':record['quality_rounds'],
                                   'targeted_review_provenance':deepcopy(record['prior_evidence'])})
            record.update(answer_evidence=str(artifact), execution_status='succeeded', format_status='valid',
                          answer_state='verified_empty' if not formatted else 'value', pipeline_calls=self.call_count-started)
            record.pop('unanswered_reason', None)
            return formatted
        record['pipeline_calls'] = self.call_count-started
        if len(record.get('local_reinspection_rounds', [])) == 3:
            answer = self.fallback_answer(path, question, record)
            record['pipeline_calls'] = self.call_count-started
            if answer:
                record.update(quality_status='passed', execution_status='succeeded', format_status='valid',
                              answer_state='value', execution_method='fallback_selection_and_format')
                record.pop('unanswered_reason', None)
                return answer
            record['unanswered_reason'] = '复核后仍无法确认：' + record['fallback'].get('reason', '兜底未通过')
            if record['fallback'].get('reason', '').startswith('选中答案未通过格式或合法性校验'):
                record['format_status'] = 'invalid'
        return ''

    def locate(self, path):
        path = Path(path).resolve()
        source_hash = digest(path)
        matches = [d for d in self.imports if d['document_sha256'] == source_hash]
        if len(matches) != 1:
            raise ValueError('解析导入必须与文件内容唯一匹配')
        spec = matches[0]; raw_path = Path(spec['raw_path'])
        if digest(raw_path) != spec['raw_sha256']:
            raise ValueError('原始解析结果哈希不匹配')
        prepared_import = validate_prepared_import(spec,source_hash,self.base)
        config = {'parser':spec['parser'], 'configuration':spec['configuration'], 'raw':spec['raw_sha256'],
                  'file':source_hash, 'adapter':hashlib.sha256((inspect.getsource(convert)+digest(Path(__file__).with_name('parser_html.py'))).encode()).hexdigest(), 'contract':CONTRACT,
                  'preprocessing':digest(Path(__file__).with_name('input_quality.py')),
                  'dpi':self.base.PDF_DPI, 'vision_model':self.base.VISION_MODEL,
                  'thinking':self.base.thinking_configuration('input-quality',self.base.VISION_MODEL)}
        revision = hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
        key = hashlib.sha256(path.read_bytes() + revision.encode()).hexdigest()
        if key in self.catalogs:
            return self.catalogs[key]
        folder = self.cache / key; folder.mkdir(parents=True,exist_ok=True)
        saved_path = folder/'parser-manifest.json'
        if saved_path.exists():
            saved = json.loads(saved_path.read_text())
            if saved['key'] != key:
                raise ValueError('解析缓存指纹不匹配')
            for name, expected in saved['artifacts'].items():
                target = (folder/name).resolve()
                if folder.resolve() not in target.parents or digest(target) != expected:
                    raise ValueError('解析缓存损坏或路径越界')
            facts = json.loads((folder/'facts.json').read_text())
            prepared = saved['prepared']
            prepared['page_paths'] = [folder/name for name in prepared['page_paths']]
        else:
            self.document_count += 1
            shutil.copy2(raw_path,folder/'raw.json')
            facts = convert(json.loads(raw_path.read_text()),source_hash,spec['raw_sha256'],spec['configuration'])
            if prepared_import:
                source_dir=Path(prepared_import['quality_dir']).resolve()
                input_dir=folder/'input';input_dir.mkdir(exist_ok=True)
                for name in prepared_import['artifacts']:
                    shutil.copy2(source_dir/name,input_dir/name)
                quality=json.loads((input_dir/'quality.json').read_text())
                pages=sorted(input_dir.glob('page-*-prepared.png'))
                if len(pages)!=len(quality['pages']):raise ValueError('预处理页面数与记录不一致')
                prepared={'page_paths':pages,'quality':quality}
            else:
                prepared = prepare(path,folder/'input',self.base,self.ask)
            facts['document_page_count'] = len(prepared['page_paths'])
            if any(t['source']['page'] > facts['document_page_count'] for t in facts['tables']):
                raise ValueError('解析表格页码超出文件')
            atomic_json(folder/'facts.json',facts)
            saved_prepared = {k:v for k,v in prepared.items() if k!='content'}
            saved_prepared['page_paths'] = [str(p.relative_to(folder)) for p in prepared['page_paths']]
            atomic_json(saved_path,{'key':key,'dependencies':config,'prepared':saved_prepared,
                'artifacts':{str(p.relative_to(folder)):digest(p) for p in folder.rglob('*') if p.is_file()}})
        # 文件属性由程序读取，作为独立文档事实提供，不混入任意子表。
        facts = deepcopy(facts)
        if path.suffix.lower() == '.pdf':
            import fitz
            with fitz.open(path) as pdf:
                count = pdf.page_count
            if count != facts['document_page_count']:
                raise ValueError('PDF实际页数与预处理页面数量不一致')
            facts['context'].append({'id':'DOCUMENT_PAGE_COUNT','text':str(count),'state':'value',
                'confidence':None,'kind':'document_page_count','table_ids':[],
                'source':{'page':1,'scope':'document','document_sha256':source_hash,
                          'method':'PyMuPDF.page_count','value':count}})
        for page in range(1, facts['document_page_count']+1):
            blocks = [t for t in facts['tables'] if t['source']['page']==page]
            facts['context'].append({'id':f'PAGE_{page}_PARSED_TABLE_COUNT','text':str(len(blocks)),
                'state':'value','confidence':None,'kind':'parsed_page_table_count','table_ids':[],
                'source':{'page':page,'scope':'page','document_sha256':source_hash,
                          'raw_sha256':spec['raw_sha256'],'method':'count_parsed_blocks',
                          'layout_ids':[t['source']['layout_id'] for t in blocks]},
                'meaning':'解析识别到的该页表块数，是否等于题目所问主表数必须对照原图核验，不能直接认定逻辑表数量'})
        catalog = []
        for table in facts['tables']:
            headers = ' | '.join(c['text'] for c in table['cells'] if c['row']<4)
            catalog.append({'table_id':table['table_id'],'title':table['title'],'headers':headers,'unit':'',
                            'description':'独立解析表块，未证明与其他表块接续；headers仅为开头预览，不能据此判定数据不存在。',
                            'row_labels':[c['text'] for c in table['cells'] if c['col']<2 and c['text']],
                            'parts':[{'page':table['source']['page'],'bbox':[0,0,1000,1000]}]})
            self.tables[key,table['table_id']] = table
        # 文档级事实不是任意子表的属性，为其提供独立选取对象。
        catalog.append({'table_id':'D0','title':'文档表外内容及文件属性','headers':'PDF页数：'+str(facts['document_page_count']) if path.suffix.lower()=='.pdf' else '', 'unit':'',
                        'description':'用于PDF页数、跨页判断、页面表块数量、标题和标准号等文档整体信息。跨页展示多个表格或页数乘表数不应把各子表分别当作整份文档作答。解析表块数量须对照原图核验是否等于可见主表数量，不用于表格网格问题',
                        'parts':[{'page':p,'bbox':[0,0,1000,1000]} for p in sorted({c['source']['page'] for c in facts['context']})]})
        self.tables[key,'D0'] = {'row_count':0,'col_count':0,'cells':[],'structure_confidence':None}
        entry = {'path':path,'key':key,'revision':revision,'folder':folder,'tables':catalog,'context':facts['context'],
                 'prepared':prepared,'warnings':[],'parser_facts':facts}
        self.catalogs[key] = entry
        return entry

    def crops(self, entry, table, dpi):
        images = super().crops(entry, table, dpi)
        if self.icon_verification_active or re.search(r'左侧|右侧|竖排|横向拍摄|二维码|图标|明细行|合计区|手写批注|列数|是否(?:含有|包含|含)|(?:红|橙|黄|绿|青|蓝|紫|黑|白|灰)色(?:标签|标记)', getattr(self, 'active_question', '')):
            images = []
            for page in sorted({p['page'] for p in table['parts']}):
                original = entry['folder'] / 'input' / f'page-{page:03d}-original.png'
                if not original.is_file():raise ValueError('位置题缺少原始方向页面证据')
                images.extend([{'type':'text','text':f'第{page}页未旋转原图：题干左右及竖排均按原始方向判断；本次只使用未旋转原图确定空间关系。'}, self.base.image_part(original.read_bytes())])
        if self.icon_verification_active and self.icon_review_sheet:
            sheet=Path(self.icon_review_sheet)
            images.extend([{'type':'text','text':'程序依据实线网格制作的单元格定位接触图；标签row和col从0开始，须与原始整图共同核验。'},self.base.image_part(sheet.read_bytes())])
        return images

    def recognize(self, entry, region):
        return self.tables[entry['key'],region['table_id']]

    def recognize_dense_blocks(self, entry, region):
        return self.recognize(entry,region)

    def reuse_multipage(self, entry, region):
        return self.recognize(entry,region)

    def narrow_existence_witness(self, table, plan, record):
        """存在性阳性候选只需一处证据；仍须独立原图核验，不生成否定结论。"""
        changes = []
        def visit(expr):
            if expr.get('op') == 'text_contains':
                expected = normalize(plain_math_text(expr['expected'])).casefold()
                if expected:
                    for ref in expr['refs']:
                        if not isinstance(ref, list):
                            continue
                        cell = cell_at(table, ref)
                        if cell.get('state') == 'value' and expected in normalize(plain_math_text(cell['text'])).casefold():
                            if len(expr['refs']) > 1:
                                changes.append({'before':deepcopy(expr['refs']), 'after':[deepcopy(ref)],
                                                'reason':'存在性阳性候选；未核验前不视为事实，阴性及unknown不缩范围'})
                                expr['refs'] = [deepcopy(ref)]
                            break
            for child in expr.get('args', []):
                if isinstance(child, dict): visit(child)
        for expr in plan.get('expressions', []): visit(expr)
        # 仅当模型把搜索范围原样重复为全部补充证据时，消除重复；其他表头等依据保留。
        if len(plan.get('expressions', [])) == 1 and len(changes) == 1:
            old = changes[0]['before']
            if plan.get('evidence_refs') == old:
                plan['evidence_refs'] = deepcopy(changes[0]['after'])
                changes[0]['duplicate_evidence_removed'] = True
        if changes: record.setdefault('existence_scope_changes', []).extend(changes)

    def reinspect_parser_field(self, entry, task, targets, checks, record):
        """定位存疑字段的原图区域并放大；结果只更新当前题候选。"""
        def cannot_locate(reason, observation_call=None):
            record['unanswered_reason'] = reason
            record['local_reinspection_stop'] = {'kind': 'no_executable_target', 'reason': reason,
                                                 'source_sha256': digest(entry['path'])}
            if observation_call and observation_call.is_file():
                record['local_reinspection_stop'].update(observation_call=str(observation_call.resolve()),
                                                          observation_sha256=digest(observation_call))
            return False
        candidates = [(key,item) for key,item in targets.items() if 'row' in item and
                      (checks[key].get('confidence',0) <= self.base.CONFIDENCE_HIGH
                       or not verified_match(item,checks[key]))]
        if not candidates:
            candidates = [(key,item) for key,item in targets.items() if 'row' in item]
        if not candidates:
            return cannot_locate('没有可定位的字段疑点，不能伪造局部复核')
        key,item = candidates[0]; source = item.get('source',{}); page = source.get('page')
        if type(page) is not int or page < 1:
            return cannot_locate('存疑字段缺少可靠来源页，无法裁剪')
        image_path = entry['folder']/'input'/f'page-{page:03d}-original.png'
        if not image_path.is_file():
            return cannot_locate('缺少存疑字段的原始页面')
        raw=image_path.read_bytes(); image_hash=hashlib.sha256(raw).hexdigest()
        payload={'task':task,'target':{key:item},'source_sha256':digest(entry['path']),
                 'page':page,'image_sha256':image_hash,'purpose':'仅定位存疑字段和必要表头上下文'}
        try:
            observed=self.ask('visual-observe',
                '在唯一提供的原始页面上定位target中指定的单元格，而不是题目最终答案所在单元格。'
                '结合target的原文、逻辑行列及来源判断，保留相邻表头，输出包含目标和上下文的bbox；'
                'page必须使用输入page，不是印刷页码。无法定位时uncertain，不能猜坐标。'
                '只输出契约字段，reason简短说明定位依据，不输出推理过程。',
                payload,[self.base.image_part(raw)])
        except ValueError as error:
            call_path = self.output / f'call-{self.call_count:04d}-visual-observe.json'
            stopped = cannot_locate('局部定位响应不符合契约，无法据此裁剪：' + str(error))
            if call_path.is_file():
                record['local_reinspection_stop'].update(observation_call=str(call_path.resolve()),
                                                          observation_sha256=digest(call_path))
            return stopped
        record.setdefault('local_region_observations',[]).append(observed)
        if observed['status']!='observed' or not observed['complete'] or not observed['matches']:
            return cannot_locate('字段局部位置未能定位：'+observed['reason'], self.output / f'call-{self.call_count:04d}-visual-observe.json')
        if any(match['page']!=page for match in observed['matches']):
            return cannot_locate('局部观察混淆了来源页与印刷页码', self.output / f'call-{self.call_count:04d}-visual-observe.json')
        # 位置不确定时保留所有候选和相邻表头，合并裁剪范围而不是任选一个候选。
        boxes=[bbox(match['bbox']) for match in observed['matches']]
        x1=min(box[0] for box in boxes);y1=min(box[1] for box in boxes)
        x2=max(box[2] for box in boxes);y2=max(box[3] for box in boxes)
        with Image.open(io.BytesIO(raw)) as original:
            original=original.convert('RGB'); w,h=original.size
            box=(max(0,int((x1-30)*w/1000)),max(0,int((y1-30)*h/1000)),
                 min(w,int((x2+30)*w/1000)+1),min(h,int((y2+30)*h/1000)+1))
            crop=original.crop(box);crop=crop.resize((crop.width*2,crop.height*2),Image.Resampling.LANCZOS)
            buffer=io.BytesIO();crop.save(buffer,format='PNG');crop_bytes=buffer.getvalue()
        crop_path=self.output/f'local-review-{self.call_count:04d}.png';crop_path.write_bytes(crop_bytes)
        review={'purpose':'定向重读存疑字段','scope':[key],'issues':{key:checks[key]},
                'source_sha256':digest(entry['path']),'page':page,'bbox':[x1,y1,x2,y2],
                'candidate_regions':observed['matches'],
                'original_sha256':image_hash,'crop_sha256':digest(crop_path),
                'previous_rounds':deepcopy(record.get('local_reinspection_rounds',[])),
                'stop_condition':'重新规划并验证，平均置信度通过则停止，最多三次'}
        before=deepcopy(item)
        try:
            result=self.ask('field-reinspect',REINSPECT+'\n只重读targets中指定的原始单元格，按其原文、逻辑位置及相邻表头辨认；不得将题目最终答案写入其他单元格。不确定时返回unknown。',{'task':task,'targets':{key:item},'review':review},
                            [self.base.image_part(raw),self.base.image_part(crop_bytes)])
            call_path=self.output/f'call-{self.call_count:04d}-field-reinspect.json'
            candidate=self.checks_by_id(result['items'],{key:item})[key]
        except ValueError as error:
            call_path=self.output/f'call-{self.call_count:04d}-field-reinspect.json'
            if not call_path.is_file():
                raise
            saved=json.loads(call_path.read_text()); saved.update(contract_valid=False,validation_error=str(error))
            atomic_json(call_path,saved)
            record.setdefault('local_reinspection_rounds',[]).append({
                'status':'invalid', 'scope':[key], 'call':str(call_path.resolve()), 'sha256':digest(call_path),
                'source_sha256':digest(entry['path']), 'crop':str(crop_path.resolve()),
                'crop_sha256':digest(crop_path), 'before':before, 'after':deepcopy(item),
                'review':review, 'error':str(error)})
            record['unanswered_reason'] = '局部字段重读契约无效：' + str(error)
            return True
        if candidate['state']=='value' and candidate['text'] or candidate['state']!='value' and not candidate['text']:
            item.update(text=candidate['text'],state=candidate['state'])
        record.setdefault('local_reinspection_rounds',[]).append({
            'status':'evidence_updated' if item!=before else 'unresolved','scope':[key],
            'call':str(call_path.resolve()),'sha256':digest(call_path),'source_sha256':digest(entry['path']),
            'crop':str(crop_path.resolve()),'crop_sha256':digest(crop_path),'before':before,'after':deepcopy(item),
            'candidate':candidate,'review':review})
        return True

    def quality(self, entry, region, task, table, context, plan, record, allow_reinspect, **kwargs):
        """没有解析自评分时以独立原图核验路由；不降低既有高阈值，不补造分数。"""
        self.narrow_existence_witness(table, plan, record)
        record['parser_validation_policy'] = '原始解析不提供置信度；以独立原图核验的关键字段和结构平均置信度路由'
        if record.get('visual_column_count_candidate'):
            reason=visual_column_plan_error(plan)
            if reason:
                record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan','plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
                record['unanswered_reason']=reason
                return False,allow_reinspect
        if record.get('multi_visual_attributes'):
            reason=multiple_attribute_plan_error(task,plan)
            if reason:
                record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan','plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
                record['unanswered_reason']=reason
                return False,allow_reinspect
        reason=conditional_selection_error(task,plan)
        if reason:
            record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                 'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
            record['unanswered_reason']=reason
            return False,allow_reinspect
        reason=multiple_fields_join_error(task,plan)
        if reason:
            record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                 'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
            record['unanswered_reason']=reason
            return False,allow_reinspect
        reason=header_boolean_as_count_error(task,plan)
        if reason:
            record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                 'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
            record['unanswered_reason']=reason
            return False,allow_reinspect
        reason=one_item_as_count_error(task,plan)
        if reason:
            record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                 'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
            record['unanswered_reason']=reason
            return False,allow_reinspect
        reason=section_cell_mismatch_error(task,plan,table)
        if reason:
            record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                 'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':[]})
            record['unanswered_reason']=reason
            return False,allow_reinspect
        try:
            targets = dependencies(table,context,plan,task['question_type'],number,normalize)
            if task['question_type']=='structure':
                if table.get('issues') or table.get('uncovered_positions'):
                    raise ValueError('完整逻辑结构存在未解决缺口或冲突')
                self.base.validate_structure(table)
            positions = {(r,c) for v in targets.values() if 'row' in v
                         for r in range(v['row'],v['row']+v['rowspan'])
                         for c in range(v['col'],v['col']+v['colspan'])}
            if any(tuple(i.get('position',[])) in positions for i in table.get('issues',[])):
                raise ValueError('题目依赖与解析结构冲突位置重合')
            rules = evaluate_rules(table,plan['rules'],number)
        except (ValueError,KeyError) as error:
            record['unanswered_reason'] = str(error)
            repairable = str(error) in ('题型与计划不一致','单值计划只能有一个引用','单值计划只能有一个表达式','计划引用不存在的表外内容','该运算必须有两个操作数','表头行必须为非空且连续的逻辑行')
            retry = allow_reinspect and repairable
            record['quality_rounds'].append({'status':'replan_required' if retry else 'unresolved_plan','plan_valid':False,'plan_reason':str(error),'fields':[]})
            return False, retry
        if not self.verify_missing_fields(entry, region, task, table, plan, record, allow_reinspect):
            return False, False
        # 格式是程序契约：先试算再检查；计划错误只重规划，不消耗视觉质检。
        preview = None
        try:
            preview = execute(table, plan, task['question_type'], context, confirmed_missing_fields=record['confirmed_missing_fields'],
                              confirmed_empty_sum_refs=kwargs.get('confirmed_empty_sum_refs',()))
        except UncertainAnswer as error:
            if str(error) == '截取结果为空，无法确认':
                reason = '计划的文本截取结果为空；请基于已引用原文重新规划具体字段和零基左闭右开范围，不得猜测或修改来源。'
                def collect_empty_substrings(expr):
                    details = []
                    if expr.get('op') == 'substring':
                        ref = expr.get('ref')
                        source = ([x for x in context if x.get('id') == ref.get('context_id')]
                                  if isinstance(ref, dict) else [cell_at(table, ref)])
                        if len(source) == 1 and source[0].get('state') == 'value':
                            text = normalize(source[0]['text'])
                            start, end = expr.get('start'), expr.get('end')
                            if type(start) is int and type(end) is int and not text[start:end].strip():
                                details.append({'ref':ref,'text':text,'start':start,'end':end})
                    for child in expr.get('args', []):
                        if isinstance(child, dict): details.extend(collect_empty_substrings(child))
                    return details
                details = sum((collect_empty_substrings(expr) for expr in plan.get('expressions', [])), [])
                if details:
                    reason += ' 原文及无效范围：' + json.dumps(details, ensure_ascii=False)
                record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                    'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':rules})
                record['unanswered_reason'] = reason
                return False, allow_reinspect
            # 识别未知不能当计划错误，保留原图核验机会。
            pass
        except (ValueError, ArithmeticError) as error:
            if str(error)=='文本截取范围无效':
                reason='计划的substring起止位置超出规范化后的原文范围；请根据引用文本重新计算零基、左闭右开的字符位置，禁止修改源数据。'
                record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                    'plan_valid':False,'plan_reason':reason,'structure_valid':None,'fields':[],'rules':rules})
                record['unanswered_reason']=reason
                return False,allow_reinspect
            # 其他数据值疑点仍交给原图核验，不扩大为整表识别。
            pass
        else:
            try:
                self.base.format_answer(json.dumps({'answer':preview}, ensure_ascii=False), task['question_type'], task.get('answer_format'), [table,context])
            except (ValueError, ArithmeticError) as error:
                reason = '计划输出类型与题目格式不符：' + str(error) + '；多个文本要求string时用calculate/join逐项引用；不可仅修改as_array=false。数量问题仍须执行count，不得改成列出原始行。'
                record['quality_rounds'].append({'status':'replan_required' if allow_reinspect else 'unresolved_plan',
                    'plan_valid':False,'plan_reason':reason,'fields':[],'rules':rules})
                record['unanswered_reason']=reason
                return False, allow_reinspect
        expressions = plan.get('expressions', [])
        if (plan.get('kind') == 'calculate' and len(expressions) == 1
                and expressions[0].get('op') == 'language' and not plan['rules']
                and not plan['context_ids'] and all('row' in v for v in targets.values())):
            verdict = self.ask('language-verify',
                '独立对照原图判断目标表格主要使用的语言，不依赖计划给出的语言。'
                '不要将拉丁字母自动判为英文，品牌和缩写不自动构成独立语言；表外标题不计入。'
                '必须查看整个目标表确认没有遗漏主要语言。语言名称按题目要求输出，无另行要求时用中文；返回语言名称、范围是否明确、置信度及简短词汇/语法依据。'
                'evidence只摘录支持语言判断的短词汇，用提供的单元格id定位；必须是对应输入单元格中连续存在的原文片段。禁止省略号、拼接、改写、补全括号或摘录整段规格。两三个代表性词汇即可，逐字检查来源。位置不清或证据不足则scope_valid=false。',
                {'task':task,'table_id':region['table_id'],'title':region.get('title'),
                 'targets':{k:{'text':v['text'],'row':v['row'],'col':v['col']} for k,v in targets.items()}},
                self.crops(entry,region,self.base.PDF_DPI))
            from language_evidence import accept_language
            accepted, reason = accept_language(verdict, plan, targets, self.base.CONFIDENCE_HIGH)
            record['quality_rounds'].append({'status':'accepted' if accepted else 'unresolved_language',
                'plan_valid':accepted,'plan_reason':reason,'structure_valid':verdict['scope_valid'],
                'structure_confidence':verdict['confidence'],'fields':[], 'rules':[],
                'language_verdict':verdict,'target_ids':list(targets),'model':self.base.VERIFY_MODEL})
            if not accepted:record['unanswered_reason']=reason
            if (not accepted and allow_reinspect and reason == '独立原图语言判断与计划冲突'
                    and verdict['scope_valid'] and verdict['confidence'] > self.base.CONFIDENCE_HIGH):
                record['quality_rounds'][-1]['status'] = 'replan_required'
                record['quality_rounds'][-1]['plan_reason'] = (reason + '；原图核验依据：' + verdict['reason']
                    + '。重新判断目标表主要语言，型号、品牌和缩写不自动构成独立语言；新计划仍需独立核验。')
                return False, True
            return accepted,False
        view = verification_view(table)
        data = {'task':task,'plan':plan,'table':view,'context':context,'targets':targets,
                'source_policy':record['parser_validation_policy'],
                'scope':'原图核实本题全部依赖、表头、单位与字段归属；无关区域疑点不阻断'}
        if plan['kind']=='structure':
            data['structure_output'] = {'row_count':table['row_count'],'col_count':table['col_count'],
                                        'cells':structure_cells(table,plan)}
        images = self.crops(entry,region,self.base.PDF_DPI)
        if self.icon_verification_active and self.icon_review_sheet:
            review_path=Path(self.icon_review_sheet)
            data['icon_review_sheet']={'path':str(review_path.resolve()),'sha256':digest(review_path)}
        visual_targets = {k:v for k,v in targets.items() if v.get('kind')=='visual_candidate_count'}
        visual_instructions = ''
        if targets and all('row' not in item for item in targets.values()):
            visual_instructions += ('\n本次依赖均为表外内容，structure_valid判断题目所需的页面区域、'
                '内容归属、范围和完整性是否可从原图确认，不要求存在表格网格或合并单元格；'
                '有任何来源位置、范围或完整性疑点时仍须返回false，逐项独立核验原文和位置。')
        if any(v.get('source',{}).get('method')=='icon_observation' for v in targets.values()):
            visual_instructions += ('\nicon_observation字段是未经最终核验的图标候选；'
                '独立对照原图的对应行列和完整表头，row与col均从0开始；先确认目标所在行标题和列标题的交叉处，'
                '不能将位置数字另加1，也不能凭候选或邻格猜测。明确区分勾、叉和无法辨认。'
                '确实是勾时检查text用✓、叉用✗；部分支持、第三方等文字是否影响题目比较应独立判断。')
        if any(v.get('kind')=='visual_handwriting' for v in targets.values()):
            visual_instructions += ('\nvisual_handwriting是视觉模型提出的手写原文候选，不是已确认答案。'
                '独立对照原图核对手写与印刷的区别、笔迹文字和所在页面区域；不可辨认则unknown且不通过。')
        if visual_targets:
            visual_instructions += ('\nvisual_candidate_count是程序统计原图候选区域的数量，不是文档印刷文字。'
                '对照原始页面独立检查候选对象类型、目标表归属、颜色、selected_indexes对应的候选框及必要范围。'
                '若selection_purpose为存在性阳性证据，独立确认所选的一处真实存在即可，不要求所选数等于全部实例数；'
                '其他计数或否定必须核查完整范围。正确时checks.text填写所核验的数量字符串；'
                '有关联对象、位置、范围的遗漏、重复或不能辨认则consistent=false。'
                '不得仅因统计值没有直接印在图片中就拒绝；不得根据候选或其他模型自评分放行。')
        try:
            prompt=VERIFY+'\n解析数据没有识别置信度和逐格图片框。依据页面原图独立读取，逻辑行列对应不清则拒绝；不要要求接口提供自评分。'+visual_instructions
            if len(targets)>12:
                chunks=[]; keys=list(targets)
                for offset in range(0,len(keys),12):
                    subset={key:targets[key] for key in keys[offset:offset+12]}
                    chunk_data=dict(data,targets=subset,scope='仅核验本分批字段，保留完整原图、表头和结构上下文')
                    result=self.ask_contract('verify',prompt+'\n只返回本批targets中的字段，不得漏项或添加其他字段。',chunk_data,images)
                    self.checks_by_id(result['checks'],subset)
                    call_path=self.output/f'call-{self.call_count:04d}-verify.json'
                    chunks.append({'path':str(call_path.resolve()),'sha256':digest(call_path),'targets':list(subset),'verdict':result})
                verdict={'plan_valid':all(x['verdict']['plan_valid'] for x in chunks),
                         'plan_reason':'；'.join(x['verdict']['plan_reason'] for x in chunks),
                         'structure_valid':all(x['verdict']['structure_valid'] for x in chunks),
                         'structure_confidence':min(x['verdict']['structure_confidence'] for x in chunks),
                         'checks':[item for x in chunks for item in x['verdict']['checks']]}
                record['chunked_verification']={'chunks':chunks,'combined':verdict}
            else:
                verdict = self.ask_contract('verify',prompt,data,images)
        except ValueError as error:
            if str(error) == '复核结果必须完整且仅覆盖请求的字段，不能遗漏、重复或新增':
                from pipeline_contracts import validate
                prior_path=self.output/f'call-{self.call_count:04d}-verify.json'
                prior=json.loads(prior_path.read_text()) if prior_path.is_file() else {}
                partial=json.loads(prior.get('content','{}')); validate('verify',partial)
                keys=[item['id'] for item in partial['checks']]
                if len(keys)==len(set(keys)) and set(keys)<set(targets):
                    subset={key:targets[key] for key in keys}
                    self.checks_by_id(partial['checks'],subset)
                    missing={key:item for key,item in targets.items() if key not in subset}
                    extra_data=dict(data,targets=missing, scope='仅补核本次遗漏的字段，保留完整原图和表头上下文')
                    extra=self.ask_contract('verify',VERIFY+'\n只返回targets中的遗漏字段，原图独立核验；不得重复其他字段。',extra_data,images)
                    self.checks_by_id(extra['checks'],missing)
                    extra_path=self.output/f'call-{self.call_count:04d}-verify.json'
                    verdict={'plan_valid':partial['plan_valid'] and extra['plan_valid'],
                             'plan_reason':partial['plan_reason']+'；遗漏字段补核：'+extra['plan_reason'],
                             'structure_valid':partial['structure_valid'] and extra['structure_valid'],
                             'structure_confidence':min(partial['structure_confidence'],extra['structure_confidence']),
                             'checks':partial['checks']+extra['checks']}
                    record['partial_verification']={'first':str(prior_path.resolve()),'first_sha256':digest(prior_path),
                        'second':str(extra_path.resolve()),'second_sha256':digest(extra_path),
                        'original_targets':list(targets),'missing':list(missing),'combined':verdict}
                else:
                    verdict = None
            else:
                verdict = None
            if verdict is not None:
                error = None
            else:
                reason = '验证输出契约未通过：' + str(error)
                record['unanswered_reason'] = reason
                failed_round = {'status':'unresolved_verification_contract', 'plan_valid':False,
                                'plan_reason':reason, 'structure_valid':None, 'fields':[], 'rules':[]}
                record['quality_rounds'].append(failed_round)
                # 契约无效不能计算均值；只将具体状态冲突用于定位，不把无效响应当作验证结果。
                call_path=self.output/f'call-{self.call_count:04d}-verify.json'
                if allow_reinspect and str(error)=='模型必须输出JSON对象':
                    previous=self.output/f'call-{self.call_count-1:04d}-verify.json'
                    if previous.is_file():
                        prior=json.loads(previous.read_text())
                        if (prior.get('validation_error')=='复核文本与状态不一致'
                                and prior.get('input',{}).get('task')==task and prior.get('input',{}).get('plan')==plan):
                            call_path=previous
                if allow_reinspect and call_path.is_file() and json.loads(call_path.read_text()).get('validation_error')=='复核文本与状态不一致':
                    call=json.loads(call_path.read_text()); response=json.loads(call.get('content','{}'))
                    bad={x.get('id') for x in response.get('checks',[]) if isinstance(x,dict)
                         and x.get('state') in ('empty','unknown') and bool(x.get('text'))}
                    focused={key:item for key,item in targets.items() if key in bad}
                    unknown={key:{'id':key,'text':'','state':'unknown','confidence':0,'consistent':False,
                                  'position_consistent':False,'span_consistent':False,'reason':reason}
                             for key in focused}
                    if focused and self.reinspect_parser_field(entry,task,focused,unknown,record):
                        failed_round.update(status='replan_required', plan_reason=reason+'；局部读取后重新规划并核验。')
                        return False,True
                return False, False
        checks = self.checks_by_id(verdict['checks'],targets)
        average_route = average_confidence_route(checks, verdict.get('structure_confidence'), self.base.CONFIDENCE_HIGH)
        if average_route['status'] == 'accepted':
            record['quality_rounds'].append({
                'status':'accepted', 'policy':'question_average_confidence_v1',
                'average_confidence':average_route, 'plan_valid':verdict['plan_valid'],
                'plan_reason':verdict['plan_reason'], 'structure_valid':verdict['structure_valid'],
                'structure_confidence':verdict['structure_confidence'], 'rules':rules,
                'model':self.base.VERIFY_MODEL,
                'fields':[{'id':key, 'recognition_confidence':None, 'verification':checks[key],
                           'source':item['source'], 'route':'accepted_by_average'}
                          for key,item in targets.items()],
                'review_contract':{'purpose':'记录当前题关键字段与结构验证平均置信度',
                                   'scope':list(targets), 'full_page_context':True,
                                   'stop_condition':'平均值严格高于阈值即停止模型质检'}})
            return True, False
        if allow_reinspect:
            round_record = {'status':'unresolved_fields', 'policy':'question_average_confidence_v1',
                'average_confidence':average_route, 'plan_valid':verdict['plan_valid'],
                'plan_reason':verdict['plan_reason'], 'structure_valid':verdict['structure_valid'],
                'structure_confidence':verdict['structure_confidence'], 'rules':rules, 'model':self.base.VERIFY_MODEL,
                'fields':[{'id':key,'recognition_confidence':None,'verification':checks[key],
                           'source':item['source'],'route':'reinspect'} for key,item in targets.items()]}
            record['quality_rounds'].append(round_record)
            if not verdict['plan_valid'] and record.get('verification_plan_replans', 0) <= len(record.get('local_reinspection_rounds', [])):
                record['verification_plan_replans'] = record.get('verification_plan_replans', 0) + 1
                round_record.update(status='replan_required',
                    plan_reason='原图核验指出计划问题，应先修正计划：'+verdict['plan_reason'])
                return False,True
            if self.reinspect_parser_field(entry,task,targets,checks,record):
                round_record.update(status='replan_required',
                    plan_reason='关键字段平均置信度未通过；局部原图重识别后重新规划并核验。')
                return False,True
            return False,False
        field_reviews=[]
        # 只重核输出自相矛盾的字段，不能根据reason猜测模型实际读到了什么。
        contradictory={key:item for key,item in targets.items()
            if item.get('kind')!='visual_candidate_count' and checks[key]['state']==item['state']
            and checks[key]['text']==item['text'] and not checks[key]['consistent']
            and checks[key]['position_consistent'] and checks[key]['span_consistent']}
        if contradictory and allow_reinspect:
            review_data={'task':task,'plan':plan,'table':view,'context':context,'targets':contradictory,
                         'prior_checks':{key:checks[key] for key in contradictory},
                         'purpose':'只复核输出自相矛盾的字段；保留原图和必要表头上下文'}
            if data.get('icon_review_sheet'):review_data['icon_review_sheet']=data['icon_review_sheet']
            review=self.ask_contract('verify',VERIFY+'\n上一轮对这些字段返回了相同文字却标记不一致。'
                '只独立重读指定字段：text必须填写原图实际文字，若与候选不同不可复制候选；'
                '检查位置和跨度。若认为前一判断有误，reason说明原图依据；无法确定则consistent=false。',review_data,images)
            review_checks=self.checks_by_id(review['checks'],contradictory)
            review_path=self.output/f'call-{self.call_count:04d}-verify.json'
            field_reviews.append({'targets':list(contradictory),'verdict':review,'path':str(review_path.resolve()),'sha256':digest(review_path),
                                  'purpose':'纠正同文却标记不一致的输出契约冲突'})
            if review['plan_valid'] and review['structure_valid'] and review['structure_confidence']>self.base.CONFIDENCE_HIGH:
                checks.update(review_checks)
        uncertain_text={key:item for key,item in targets.items()
            if 'row' in item and item.get('kind')!='visual_candidate_count'
            and checks[key]['state']=='value' and checks[key]['text']!=item['text']
            and checks[key]['position_consistent'] and checks[key]['span_consistent']
            and self.base.CONFIDENCE_LOW <= checks[key]['confidence'] <= self.base.CONFIDENCE_HIGH}
        if uncertain_text and allow_reinspect and verdict['plan_valid'] and verdict['structure_valid']:
            review_data={'task':task,'plan':plan,'table':view,'context':context,'targets':uncertain_text,
                         'prior_checks':{key:checks[key] for key in uncertain_text},
                         'purpose':'针对文字不一致且置信度未超过高阈值的关键字段，独立重读原图'}
            review=self.ask_contract('verify',VERIFY+'\n只独立重读指定关键字段，返回原图完整文字、位置、跨度和置信度；'
                '不要复制前次候选或以多数表决代替原图判断，不清楚时标unknown。',review_data,images)
            review_checks=self.checks_by_id(review['checks'],uncertain_text)
            review_path=self.output/f'call-{self.call_count:04d}-verify.json'
            review_record={'targets':list(uncertain_text),'verdict':review,'path':str(review_path.resolve()),'sha256':digest(review_path),
                           'purpose':'定向重读低置信度且文字不一致的关键字段'}
            record.setdefault('targeted_text_reviews',[]).append(review_record)
            if review['plan_valid'] and review['structure_valid'] and review['structure_confidence']>self.base.CONFIDENCE_HIGH:
                field_reviews.append(review_record);checks.update(review_checks)
        average_route = average_confidence_route(checks, verdict.get('structure_confidence'), self.base.CONFIDENCE_HIGH)
        accepted = average_route['status'] == 'accepted'
        round_record = {'status':'accepted' if accepted else 'unresolved_fields','plan_valid':verdict['plan_valid'],
            'plan_reason':verdict['plan_reason'],'structure_valid':verdict['structure_valid'],
            'structure_confidence':verdict['structure_confidence'],'rules':rules,'model':self.base.VERIFY_MODEL,
            'average_confidence':average_route,
            'thresholds':{'low':self.base.CONFIDENCE_LOW,'high':self.base.CONFIDENCE_HIGH,'calibrated':False},
            'fields':[{'id':k,'recognition_confidence':None,'verification':checks[k],'source':v['source'],
                       'route':'accepted' if verified_match(v,checks[k]) and checks[k]['confidence']>self.base.CONFIDENCE_HIGH else 'reinspect'} for k,v in targets.items()],
            'field_contract_reviews':field_reviews,
            'review_contract':{'purpose':'核验解析结果中当前题的全部依赖','scope':list(targets),'full_page_context':True,
                               'stop_condition':'一次核验；具体字段冲突时仅修订这些字段，再规划一次'}}
        record['quality_rounds'].append(round_record)
        if accepted:
            round_record['policy'] = 'question_average_confidence_v1'
            return True,False
        # 仅对明确要求表头且文字、空白或跨度存疑的题重识别表头；完整表行列数不得改猜。
        if (allow_reinspect and task['question_type'] == 'structure'
                and len(region['parts']) == 1 and region['parts'][0].get('bbox') == [0,0,1000,1000]
                and header_review_required(table, plan, targets, checks, self.base.CONFIDENCE_HIGH)):
            old_table = deepcopy(table)
            previous = self.header_candidates.pop(region['table_id'], None)
            try:
                try:
                    candidate = self.recognize_header(entry, region)
                except ValueError as error:
                    record['header_structure_review'] = {'status':'unresolved', 'reason':'表头候选不满足结构契约：' + str(error)}
                    record['unanswered_reason'] = record['header_structure_review']['reason']
                    round_record['status'] = 'unresolved_structure'
                    return False, False
            finally:
                if previous is not None:
                    self.header_candidates[region['table_id']] = previous
            artifact = self.output / f"header-{region['table_id']}-{self.call_count:04d}.json"
            call = self.output / f'call-{self.call_count:04d}-recognize-header.json'
            coverage = {(row, col) for cell in candidate['cells']
                        for row in range(cell['row'], cell['row'] + cell['rowspan'])
                        for col in range(cell['col'], cell['col'] + cell['colspan'])}
            if (candidate['row_count'] == old_table['row_count'] and candidate['col_count'] == old_table['col_count']
                    and all((row, col) in coverage for row in plan['rows'] for col in range(candidate['col_count']))
                    and artifact.is_file() and call.is_file()):
                record['header_structure_review'] = {'artifact': str(artifact.resolve()), 'sha256': digest(artifact),
                    'call': str(call.resolve()), 'call_sha256': digest(call), 'table_id': region['table_id'],
                    'parser_row_count': old_table['row_count'], 'parser_col_count': old_table['col_count'],
                    'purpose': '仅修订存疑表头，完整行列数与解析结果一致后重新独立核验'}
                table.clear(); table.update(candidate)
                round_record['status'] = 'replan_required'
                round_record['plan_reason'] = '原表头文字、空白或跨度存疑，已生成有来源的表头候选；保持完整行列数，重新规划并独立核验。'
                return False, True
            record['header_structure_review'] = {'status':'unresolved', 'reason':'表头候选行列数或要求范围不完整，未替换原解析结果',
                'artifact':str(artifact.resolve()) if artifact.is_file() else None}
            record['unanswered_reason'] = record['header_structure_review']['reason']
            round_record['status'] = 'unresolved_structure'
            return False, False
        # 单值提取时，表头解析文字或跨度错位不应自动否定已确认的数据值；
        # 只有独立原图核实题意、行列归属及目标值后才能解除该阻断。
        ref=single_numeric_cell_ref(task,plan)
        failed=[field for field in round_record['fields'] if field['route']!='accepted']
        if (not accepted and ref is not None and verdict['plan_valid'] and verdict['structure_valid']
                and verdict['structure_confidence']>self.base.CONFIDENCE_HIGH and failed
                and not any(rule['state']=='failed' for rule in rules)
                and all('row' in targets[f['id']] and targets[f['id']]['row']<ref[0] for f in failed)):
            target=cell_at(table,ref);target_id=cell_id(target)
            if target_id in targets and verified_match(target,checks[target_id]) and checks[target_id]['confidence']>self.base.CONFIDENCE_HIGH:
                pages=sorted({part['page'] for part in region['parts']});paths=[entry['folder']/'input'/f'page-{page:03d}-original.png' for page in pages]
                if not paths or not all(path.is_file() for path in paths):raise ValueError('行列关系核验缺少原始页面')
                hashes=[digest(path) for path in paths]
                link_data={'task':task,'plan':plan,'table_id':region['table_id'],'target':target,'target_id':target_id,
                           'failed_header_ids':[f['id'] for f in failed],'page_numbers':pages,'page_hashes':hashes,
                           'purpose':'独立核对题目、目标值及其原图行列归属，不以解析表头错误跨度代替关系判断'}
                link=self.ask('semantic-link-verify','依据原始整页独立定位题目所问的行与列，读取交叉位置的实际数值。'
                    'target是解析候选，不得直接信任。必须确认该值、行标题和列标题与题目完全对应；'
                    '列归属、期间、单位、表块或目标位置有歧义时relation_valid=false，不猜测。',link_data,
                    [self.base.image_part(path.read_bytes()) for path in paths])
                link_path=self.output/f'call-{self.call_count:04d}-semantic-link-verify.json'
                link_ok=(link['relation_valid'] and link['confidence']>self.base.CONFIDENCE_HIGH and link['target_id']==target_id
                         and bool(link['row_label'].strip()) and bool(link['column_label'].strip()) and bool(link['reason'].strip()))
                if link_ok:
                    try:link_ok=number(link['observed_text'])==number(target['text'])
                    except (ValueError,ArithmeticError):link_ok=False
                link_record={'verdict':link,'path':str(link_path.resolve()),'sha256':digest(link_path),'target_id':target_id,
                             'failed_header_ids':[f['id'] for f in failed],'status':'passed' if link_ok else 'not_passed'}
                round_record['semantic_link_review']=link_record
                if link_ok:
                    round_record['scope_resolution']='表头解析冲突仍保留；单值行列关系已独立原图核验，仍需满足平均置信度路由'
        refs=mean_numeric_cell_refs(task,plan)
        if (not accepted and refs and allow_reinspect and verdict['plan_valid'] and verdict['structure_valid']
                and verdict['structure_confidence']>self.base.CONFIDENCE_HIGH and failed
                and all('row' in targets[f['id']] and targets[f['id']]['row']<min(ref[0] for ref in refs) for f in failed)):
            cells=[cell_at(table,ref) for ref in refs];ids=[cell_id(cell) for cell in cells]
            if (len(set(ids))==len(ids) and all(key in targets and verified_match(targets[key],checks[key])
                                               and checks[key]['confidence']>self.base.CONFIDENCE_HIGH for key in ids)):
                pages=sorted({part['page'] for part in region['parts']});paths=[entry['folder']/'input'/f'page-{page:03d}-original.png' for page in pages]
                if not paths or not all(path.is_file() for path in paths):raise ValueError('均值行列关系核验缺少原始页面')
                hashes=[digest(path) for path in paths]
                link_data={'task':task,'plan':plan,'table_id':region['table_id'],'targets':{key:targets[key] for key in ids},
                           'target_ids':ids,'failed_header_ids':[f['id'] for f in failed],'page_numbers':pages,'page_hashes':hashes,
                           'purpose':'独立核对全部目标数值及其题目指定的行列归属'}
                link=self.ask('semantic-multi-link-verify','独立对照原图逐项核对题目要求的全部数值、行标题及列标题，以及单位、期间和差值的操作数顺序；'
                    'targets只是解析候选，不能默认正确。必须检查是否遗漏、重复、顺序或表头归属错误；'
                    '有任何歧义时relation_valid=false。每个target_id恰好返回一次，observed_text只填实际数值。',link_data,
                    [self.base.image_part(path.read_bytes()) for path in paths])
                link_path=self.output/f'call-{self.call_count:04d}-semantic-multi-link-verify.json'
                items=link['items'];by_id={x['target_id']:x for x in items}
                link_ok=(link['relation_valid'] and link['confidence']>self.base.CONFIDENCE_HIGH and bool(link['reason'].strip())
                         and len(items)==len(ids)==len(by_id) and set(by_id)==set(ids)
                         and all(by_id[key]['row_label'].strip() and by_id[key]['column_label'].strip() for key in ids))
                if link_ok:
                    try:link_ok=all(number(by_id[key]['observed_text'])==number(targets[key]['text']) for key in ids)
                    except (ValueError,ArithmeticError):link_ok=False
                link_record={'verdict':link,'path':str(link_path.resolve()),'sha256':digest(link_path),'target_ids':ids,
                             'failed_header_ids':[f['id'] for f in failed],'status':'passed' if link_ok else 'not_passed'}
                round_record['semantic_multi_link_review']=link_record
                if link_ok:
                    round_record['scope_resolution']='表头冲突保留；全部操作数与行列关系已独立核验，仍需满足平均置信度路由'
        conflicts = [field for field in round_record['fields']
                     if field['id'] in visual_targets and field['route'] != 'accepted']
        if conflicts:
            round_record['status'] = 'visual_observation_conflict'
            record['visual_observation_conflict'] = [{'id':f['id'],'reason':f['verification']['reason'],
                'observed':f['verification']['text'],'consistent':f['verification']['consistent']} for f in conflicts]
            record['unanswered_reason'] = '视觉观察与独立原图核验冲突：' + '; '.join(f['verification']['reason'] for f in conflicts)
            return False,False
        if accepted:
            if plan.get('kind') == 'calculate':
                candidates = raw_numeric_footnotes(numeric_expression_targets(targets, plan), self.imports, digest(entry['path']))
                if candidates:
                    pages = sorted({item['source']['page'] for item in candidates})
                    originals = [entry['folder']/'input'/f'page-{page:03d}-original.png' for page in pages]
                    if not all(path.is_file() for path in originals):
                        raise ValueError('上标复核缺少原始页面')
                    payload = {'task':task, 'plan':plan, 'candidates':candidates, 'page_numbers':pages,
                               'page_hashes':[digest(path) for path in originals]}
                    verdict_footnote = self.ask('footnote-verify',
                        '独立对照原图逐项确认候选数字和上标是否分别为数值及脚注标记。'
                        '不得仅信解析候选；值、位置或上标含义不清时valid=false。'
                        'candidates只列带上标的数值，不是完整算术操作数清单；其他数值已在常规质检中处理，'
                        '不得仅因其他操作数未出现在candidates而否定本次上标核验。'
                        '每个target_id恰好输出一次，numeric_text仅为数字，marker为原图脚注字符。',
                        payload, [self.base.image_part(path.read_bytes()) for path in originals])
                    call_path = self.output/f'call-{self.call_count:04d}-footnote-verify.json'
                    items = verdict_footnote['items']; by_id = {item['target_id']:item for item in items}
                    valid = (verdict_footnote['valid'] and verdict_footnote['confidence'] > self.base.CONFIDENCE_HIGH
                             and bool(verdict_footnote['reason'].strip()) and len(items) == len(candidates) == len(by_id)
                             and all(item['target_id'] in by_id and by_id[item['target_id']]['numeric_text'] == item['numeric_text']
                                     and by_id[item['target_id']]['marker'] == item['marker'] for item in candidates))
                    record['footnote_review'] = {'call':str(call_path.resolve()), 'sha256':digest(call_path),
                        'verdict':verdict_footnote, 'candidates':candidates, 'status':'passed' if valid else 'not_passed'}
                    if not valid:
                        record['unanswered_reason'] = '数值上标脚注的原图核验未通过'
                        round_record['status'] = 'unresolved_footnote'
                        return False, False
                    record['confirmed_footnotes'] = candidates
            return True,False
        record['unanswered_reason'] = quality_failure_reason(verdict, round_record['fields'], rules, self.base.CONFIDENCE_HIGH)
        # 仅接受已独立读清且位置、跨度明确的字段修订；不更改共享解析事实。
        revised = []
        if allow_reinspect and verdict['structure_valid']:
            for key,item in targets.items():
                check = checks[key]
                if (check['confidence']>self.base.CONFIDENCE_HIGH and check['position_consistent'] and check['span_consistent']
                        and check['state']!='unknown' and (item['text']!=check['text'] or item['state']!=check['state'])):
                    before = deepcopy(item)
                    item.update(text=check['text'],state=check['state'])
                    revised.append({'id':key,'before':before,'after':deepcopy(item),'basis':check})
        if revised:
            round_record['revisions'] = revised
            round_record['status'] = 'replan_required'
            return False,True
        if allow_reinspect and visual_targets:
            failed = [field for field in round_record['fields'] if field['route'] != 'accepted']
            if failed or not verdict['structure_valid']:
                round_record['status'] = 'replan_required'
                round_record['plan_reason'] = ('视觉属性计划的核验范围或依赖存在疑点：'+verdict['plan_reason']+
                    '。具体冲突：'+json.dumps([{'id':f['id'],'reason':f['verification']['reason']} for f in failed],ensure_ascii=False)+
                    '。只保留回答当前题所必需的表头、目标区域和语义依据；不得删除会影响范围或结论的依据。'
                    '若问题要求全范围判断仍需核对完整原图范围。')
                return False,True
        if allow_reinspect and not verdict['plan_valid']:
            round_record['status'] = 'replan_required'
            return False,True
        return False,False

    def repair_row_anchor(self, path, question, record):
        """选表因标签未找到而停止时，仅查找一处行标签；原图独立确认后临时回流。"""
        entry = self.locate(path)
        regions = [r for r in entry['tables'] if r['table_id'] != 'D0']
        if not regions:
            record['parser_anchor_review'] = {'purpose': '修复行标签', 'applied': False,
                                               'reason': '解析结果没有表格候选，无法在既有表格内定位行标签'}
            return None
        candidates = []
        for region in regions:
            table = self.recognize(entry, region)
            candidates.append({'table_id':region['table_id'],'title':region['title'],
                'rows':[{'row':c['row'],'text':c['text'],'companion_cells':[{'col':v['col'],'text':v['text']} for v in table['cells'] if v['row']==c['row'] and v['col']==1]} for c in table['cells'] if c['col']==0]})
        images = []
        for region in regions:
            images.extend(self.crops(entry,region,self.base.PDF_DPI))
        task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
        anchor=self.ask_contract('parser-anchor',
            '只根据原图查找题目所指对象是否有一处行标签被解析错。不要作答、不要恢复整表。'
            '输入rows是完整第一列标签及逻辑行号，部分文字可能错；必须通过相邻行和原图边界对应到现有逻辑行。'
            '仅能明确找到一处可修正的第一列标签时located，否则uncertain或not_found。必须用同行companion_cells文字来匹配输入行号，不要在图上重新数行；图像行号和输入逻辑行号可能不同。'
            'original_text填写输入解析中的旧标签；observed_text填写原图独立读到的新标签，不能把旧值再次抄入新值。reason引用前后行及位置依据。不得根据文件名、页码模式或相似字符猜测。',
            {'task':task,'candidates':candidates,'stop_reason':record.get('unanswered_reason')},images)
        audit={'purpose':'只修复导致选表失败的一处行标签','candidate':anchor,'scope':'第一列及相邻行，保留完整页面上下文','applied':False}
        record['parser_anchor_review']=audit
        if anchor['status']!='located' or anchor['confidence']<=self.base.CONFIDENCE_HIGH:
            return None
        region=next((r for r in regions if r['table_id']==anchor['table_id']),None)
        if region is None:
            raise ValueError('行标签复核返回不存在的表')
        original=self.recognize(entry,region);patched=deepcopy(original)
        labels=sorted([c for c in patched['cells'] if c['col']==0],key=lambda c:c['row'])
        indexes=[i for i,c in enumerate(labels) if c['row']==anchor['row']]
        if len(indexes)!=1 or not anchor['observed_text'].strip():
            raise ValueError('行标签复核坐标不唯一或文本为空')
        index=indexes[0];target=labels[index];before=deepcopy(target)
        if target['text'] != anchor['original_text']:
            raise ValueError('候选原标签与解析记录不一致，不能修订')
        if target['text']==anchor['observed_text']:
            audit['reason']='未发现可修正的标签差异'
            return None
        target.update(text=anchor['observed_text'],state='value')
        neighbors=labels[max(0,index-1):min(len(labels),index+2)]
        companions=[c for c in patched['cells'] if c['row']==target['row'] and c['col']==1]
        if len(companions)!=1 or companions[0]['state']!='value':
            audit['reason']='缺少可用于区分相邻行的同行字段，不修订'
            return None
        neighbors=[*neighbors,*companions]
        targets={f"cell:{c['row']}:{c['col']}":c for c in neighbors}
        verdict=self.ask_contract('verify',VERIFY+
            '\n本次核验修订行标签、同行第二列内容及前后相邻标签。必须确认新标签和同行内容属于同一可见行；与邻行混配必须拒绝。plan_valid表示修订对象确为题目要求；'
            'structure_valid表示这些标签和行顺序在原图可确认，不表示整表所有结构已确认。'
            '这是行标签修订的前置核验，不是最终解题计划核验。禁止因未提供金额、元素限值或最终计算操作而判plan_valid=false；这些将在后续重新规划和按题核验。'
            '每个check的text必须填写原图观察值，不可用旧文字配consistent=false。无法读取时state=unknown且text为空字符串。',
            {'task':task,'targets':targets,'row_label_candidate':anchor,'table_dimensions':{'rows':original['row_count'],'cols':original['col_count']}},
            self.crops(entry,region,self.base.PDF_DPI))
        checks=self.checks_by_id(verdict['checks'],targets)
        audit['verification']=verdict
        if not (verdict['plan_valid'] and verdict['structure_valid'] and verdict['structure_confidence']>self.base.CONFIDENCE_HIGH
                and all(checks[k]['confidence']>self.base.CONFIDENCE_HIGH and checks[k]['position_consistent'] and checks[k]['span_consistent'] and checks[k]['state']=='value'
                        and (verified_match(item,checks[k]) if item is target or item in companions else bool(checks[k]['text'].strip()))
                        for k,item in targets.items())):
            return None
        audit['neighbor_observations'] = {k:checks[k] for k,item in targets.items() if item is not target and item not in companions}
        audit.update(applied=True,before=before,after=deepcopy(target))
        return entry,region,original,patched

    def observe_visual_property(self, entry, question, record, attribute=None, attribute_index=None):
        """输出候选区域而非答案；数量由程序计算，后续仍需独立原图核验。"""
        task = {k: question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
        paths = [entry['folder'] / 'input' / f'page-{i:03d}-original.png'
                 for i in range(1, len(entry['prepared']['page_paths'])+1)]
        if not all(p.is_file() for p in paths):
            raise ValueError('视觉属性缺少未旋转原图')
        images = []
        for page, path in enumerate(paths,1):
            images.extend([{'type':'text','text':f'原始方向第{page}页'}, self.base.image_part(path.read_bytes())])
        payload = {'task':task, 'catalog':entry['tables'], 'page_hashes':[digest(p) for p in paths],
                   'purpose':'识别题目指定视觉属性的候选对象及其位置，不生成答案'}
        if re.search(r'横向拍摄|拍摄方向', attribute or task['question']):
            payload['preprocessing_rotation'] = [x['assessment'].get('rotation_clockwise')
                                                  for x in entry['prepared']['quality']['pages']]
        if attribute is not None:payload['target_attribute']=attribute
        prior_path = os.environ.get('TABLE_VISUAL_REVIEW_RECORD')
        prior_source = None
        if prior_path:
            prior_path=Path(prior_path).resolve(); prior=json.loads(prior_path.read_text())
            if (prior.get('file_name')!=question.get('file_name') or str(prior.get('id'))!=str(question.get('id'))
                    or not prior.get('visual_observation')):
                raise ValueError('定向视觉复核记录与当前题目不匹配')
            folder=Path(prior['recognition_artifact']); saved=json.loads((folder/'parser-manifest.json').read_text())
            if saved['dependencies']['file']!=digest(entry['path']):
                raise ValueError('定向视觉复核记录与当前文件内容不匹配')
            conflicts=[]
            for round_info in prior.get('quality_rounds',[]):
                for field in round_info.get('fields',[]):
                    if field.get('id')=='VISUAL_CANDIDATE_COUNT' and field.get('route')!='accepted':
                        conflicts.append({'reason':field['verification']['reason'],'observed':field['verification']['text']})
            if not conflicts: raise ValueError('定向视觉复核必须有具体的原图核验冲突')
            prior_source={'path':str(prior_path),'sha256':digest(prior_path),'conflicts':conflicts,
                          'previous_observation':prior['visual_observation']}
            payload['prior_review']=prior_source
            payload['purpose']='针对前次候选与独立核验的具体冲突，重新检查对象边界和完整范围，不假定前次结果正确'
        prompt = (
            '只依照原图及题目对象定位相关视觉属性实例，输出候选范围和实例列表，不给最终答案。'
            '若题目问逻辑列数，按最底层独立逻辑列分别返回候选区域；跨列合并表头不可当成一列，多层表头不可重复计数。只统计题目指定的主表；存在多张可能的表而题意不能区分时uncertain，不能仅按位置、先后或多数票选择。目标表或列边界不明确时uncertain。'
            '“是否含”是对目标表是否存在所问内容或属性的判断，不要求表中出现题目原句；表头近义关系须结合业务对象和原图核实，含义不同或无法确认时返回uncertain，不凭名称相近直接认定等同。'
            '如果任务是“提取‘是否含某内容’对应的值”，且上下文已经确定不存在同名原文字段，本次观察对象就是该内容或属性本身；不要再把缺少该同名元数据字段当作不确定理由。原图中找到了所问内容，就记录其实际区域；确实无法确认内容含义、对象或范围时才返回uncertain。'
            '图片页码从1开始，bbox是对应原始整页的0到1000坐标；每个实例仅出现一次，不按文字重复切分。'
            '若问是否含明细行，实例是确实属于目标表的明细行；问合计区则实例是有依据的合计区域；'
            '问颜色标签则实例是带目标颜色标签的不同模块，不把同一模块内多个文字片段算作多个模块。'
            '手写批注必须是原图真实笔迹，不能将印刷文字、遮挡或噪点算作手写。'
            '拍摄方向综合原图、文字阅读方向、图像版面和预处理旋转记录判断，记录依据；不把预处理旋转直接当作原始方向。'
            '不确定目标表、属性含义、范围或有遗漏时status=uncertain、complete=false，不猜测。'
            '若有prior_review，针对冲突重新检查每个匹配实例的边界及题目对象；不同标题、分隔线分开的模块分别记录，'
            '区分页面标题与题目所指模块，不能未经确认合并多个模块，也不能把范围外标题计入。'
            '原图没有匹配实例且完整查找后，matches为空；confidence是观察把握而非官方正确率。')
        if attribute is not None:prompt+='本次只观察target_attribute这一项，不得把其他属性的候选混入。'
        replay_dir = os.environ.get('TABLE_VISUAL_REPLAY_DIR')
        matches = []
        if replay_dir:
            from pipeline_contracts import instructions, validate
            for candidate in Path(replay_dir).glob('call-*-visual-observe.json'):
                call = json.loads(candidate.read_text())
                request = call.get('request',{}); messages = request.get('messages',[])
                hashes = [part.get('sha256') for msg in messages if isinstance(msg.get('content'),list)
                          for part in msg['content'] if part.get('type')=='image_url']
                prior_prompt=prompt.replace('拍摄方向综合原图、文字阅读方向、图像版面和预处理旋转记录判断，记录依据；不把预处理旋转直接当作原始方向。',
                    '拍摄方向仅在图像证据能支持时描述，不把预处理旋转当原始拍摄方向。')
                if attribute is not None:
                    prior_prompt=prior_prompt.replace('本次只观察target_attribute这一项，不得把其他属性的候选混入。',
                        '本次只观察target_attribute这一项，不得把其他属性的候选混入；拍摄方向无法从页面横竖单独推断。')
                allowed_prompts=[prompt+instructions('visual-observe')]
                if not re.search(r'横向拍摄|拍摄方向',attribute or task['question']):
                    allowed_prompts.append(prior_prompt+instructions('visual-observe'))
                if (call.get('contract_valid') and call.get('input')==payload
                    and call.get('model')==self.base.VISION_MODEL and hashes==payload['page_hashes']
                    and request.get('extra_body',{}).get('enable_thinking') is False
                    and messages and messages[0].get('content') in allowed_prompts):
                    validate('visual-observe',json.loads(call['content']));matches.append((candidate,call))
        if len(matches)>1: raise ValueError('同一视觉观察有多个可复用来源，需明确选择')
        if matches:
            call_path, call = matches[0]; obs=json.loads(call['content']); record['visual_observation_cache_hit']=True
            prior_conflicts=[]
            for previous in call_path.parent.glob('call-*-verify.json'):
                review=json.loads(previous.read_text()); data=review.get('input',{})
                if not review.get('contract_valid') or data.get('task')!=task: continue
                target=data.get('targets',{}).get('VISUAL_CANDIDATE_COUNT')
                if not target or target.get('source',{}).get('observation')!=obs: continue
                verdict=json.loads(review['content'])
                for check in verdict.get('checks',[]):
                    if check['id']=='VISUAL_CANDIDATE_COUNT' and not verified_match(target,check):
                        prior_conflicts.append({'call':str(previous.resolve()),'reason':check['reason'],
                                                'observed':check['text']})
            if prior_conflicts:
                record['visual_observation_conflict']=prior_conflicts
                raise ValueError('已有原图核验与视觉观察冲突，不能复用该候选；需明确范围后重新观察')
        else:
            obs = self.ask('visual-observe',prompt,payload,images)
            call_path=self.output/f'call-{self.call_count:04d}-visual-observe.json'
            record['visual_observation_cache_hit']=False
        if attribute is None:record['visual_observation'] = obs
        else:record.setdefault('visual_attribute_observations',[]).append({'attribute':attribute,'index':attribute_index,'observation':obs,'call':str(call_path.resolve()),'call_sha256':digest(call_path)})
        if obs['status'] != 'observed' or not obs['complete']:
            raise ValueError('视觉属性范围或观察不确定：'+obs['reason'])
        if prior_source:
            original=prior_source['previous_observation']
            signature=lambda o: sorted((x['page'],tuple(x['bbox'])) for x in o['matches'])
            if signature(obs)==signature(original) and obs['scope']==original['scope']:
                raise ValueError('定向复核没有改变存在冲突的观察范围或候选，不能仅凭再次确认消除冲突')
        seen = set()
        for item in obs['matches']:
            if item['page'] > len(paths): raise ValueError('视觉属性页码越界')
            box = bbox(item['bbox'])
            key = (item['page'],tuple(box))
            if key in seen: raise ValueError('视觉候选区域重复')
            seen.add(key)
        scope_review = None
        if len(obs['matches'])>1 and is_visual_count_task(attribute or task['question']):
            review_input={'task':task,'observation':obs,'page_hashes':payload['page_hashes'],
                          'purpose':'独立核验统计对象的语义范围及每个候选是否属于题目所问对象'}
            scope_prompt=('独立对照原图核对目标主表及每个候选是否恰好对应一个最底层逻辑列，检查是否遗漏、重复或错把合并表头当独立列；'
                          '主表范围或列边界不明确必须ambiguous。' if re.search(r'列数',attribute or task['question']) else
                          '独立对照原始页面和题干，逐个核对候选是否都是题目要求统计的对象。严格区分页面或日期标题、数据表头与实际内容模块；'
                          '若题意无法判断是否包含标题，或一个候选是否包含多个独立内容模块不明确，必须status=ambiguous。')
            scope_review=self.ask('visual-scope-review',scope_prompt+
                'included填写属于题意范围的候选序号，从0开始；只有题意和所有候选归属均清楚才clear。不得仅依据观察模型结论。',review_input,images)
            review_path=self.output/f'call-{self.call_count:04d}-visual-scope-review.json'
            record['visual_scope_review']=scope_review
            if (scope_review['status']!='clear' or scope_review['confidence']<=self.base.CONFIDENCE_HIGH
                    or sorted(scope_review['included'])!=list(range(len(obs['matches'])))):
                raise ValueError('视觉对象统计范围仍有冲突：'+scope_review['reason'])
            scope_review={'result':scope_review,'path':str(review_path.resolve()),'sha256':digest(review_path)}
        # 存在性阳性只需一处可定位候选，否定和计数仍必须覆盖完整范围。
        existence=(attribute is not None or task.get('answer_format')=='string') and bool(re.search(r'是否|有无|有没有',attribute or task['question']))
        selected=[0] if existence and obs['matches'] else list(range(len(obs['matches'])))
        item = {'id':f'VISUAL_CANDIDATE_COUNT_{attribute_index}' if attribute is not None else 'VISUAL_CANDIDATE_COUNT','text':str(len(selected)),'state':'value',
                'confidence':None,'kind':'visual_candidate_count','table_ids':[],
                'source':{'page':1,'scope':'original_pages','document_sha256':digest(entry['path']),
                          'method':'visual_candidate_count','page_hashes':payload['page_hashes'],
                          'observation':obs,'task':task,'observation_call':str(call_path.resolve()),
                          'observation_call_sha256':digest(call_path),'prior_review':prior_source,'scope_review':scope_review,
                          'selected_indexes':selected,'selection_purpose':'存在性阳性证据' if existence and selected else '完整候选范围',
                          **({'target_attribute':attribute,'attribute_index':attribute_index} if attribute is not None else {})}}
        return item

    def observe_handwriting_text(self,entry,question,record):
        """按题目读取可见手写原文，保留页面与模型观察来源。"""
        task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
        paths=[entry['folder']/'input'/f'page-{i:03d}-original.png' for i in range(1,len(entry['prepared']['page_paths'])+1)]
        if not all(p.is_file() for p in paths):raise ValueError('手写批注缺少原始页面')
        images=[]
        for page,path in enumerate(paths,1):
            images.extend([{'type':'text','text':f'原始方向第{page}页'},self.base.image_part(path.read_bytes())])
        data={'task':task,'catalog':entry['tables'],'page_hashes':[digest(p) for p in paths],
              'purpose':'定位手写原文及原图位置，不计算、不填写答案'}
        observed=self.ask('visual-handwriting',
            '只找与题目相关的真实手写文字或数字，区分印刷内容、遮挡和噪声。'
            'items逐项填写原文text、原始页面及0到1000的bbox；不可读、数量或范围不明确时status=uncertain、complete=false。'
            '不要从题目推算或补写；不要把手写以外内容当作批注。',data,images)
        record['handwriting_observation']=observed
        if observed['status']!='observed' or not observed['complete']:
            raise ValueError('手写内容无法确定：'+observed['reason'])
        if not observed['items']:raise ValueError('未找到可确认的手写内容，无法提取数值')
        ambiguous=handwriting_ambiguity(task,observed)
        if ambiguous:raise ValueError(ambiguous)
        call_path=self.output/f'call-{self.call_count:04d}-visual-handwriting.json';facts=[];seen=set()
        for index,item in enumerate(observed['items']):
            if item['page']>len(paths) or not item['text'].strip():raise ValueError('手写观察页码或文本无效')
            box=bbox(item['bbox']);key=(item['page'],tuple(box))
            if key in seen:raise ValueError('手写观察位置重复')
            seen.add(key)
            facts.append({'id':f'VISUAL_HANDWRITING_{index}','text':item['text'],'state':'value','confidence':item['confidence'],
                'kind':'visual_handwriting','table_ids':[],
                'source':{'method':'visual_handwriting','page':item['page'],'bbox':box,'document_sha256':digest(entry['path']),
                    'page_hashes':data['page_hashes'],'observation':observed,'item_index':index,'task':task,
                    'observation_call':str(call_path.resolve()),'observation_call_sha256':digest(call_path)}})
        return facts

    def repair_icon_cells(self, path, question, record):
        """按题目定位未识别符号，仅将候选回流当前表；最终结果仍需逐字段原图质检。"""
        selection=record.get('selection') or {};table_id=selection.get('table_id')
        if selection.get('status')!='selected' or not table_id or table_id=='D0':return None
        entry=self.locate(path); region=next((r for r in entry['tables'] if r['table_id']==table_id),None)
        if region is None:return None
        table=self.tables[(entry['key'],table_id)];unknown=[c for c in table['cells'] if c['state']=='unknown']
        if not unknown:return None
        task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
        target=self.ask('icon-target',
            '根据题目、已有网格与初次计划失败原因，判断是否需要从原图读取表示支持/不支持的图标。'
            '只输出影响本题所需的unknown网格坐标；比较或极值必须覆盖所有候选组及可能影响结果的行，'
            '不能包含表头或文字已识别的格子。若表格未显示符号或题意不需要，not_applicable；范围不清uncertain。',
            {'task':task,'table':verification_view(table),'prior_plan':record.get('plan'),
             'prior_failure':record.get('unanswered_reason'),'unknown_refs':[[c['row'],c['col']] for c in unknown]})
        record['icon_target']=target
        if target['status']!='needed':return None
        refs=target['refs']
        if not refs:return None
        pages=sorted({p['page'] for p in region['parts']})
        if len(pages)!=1 or any(c['source'].get('page')!=pages[0] for c in unknown):
            raise ValueError('图标定位仅支持可确认的单页完整网格，跨页关系需单独核验')
        original_path=entry['folder']/'input'/f'page-{pages[0]:03d}-original.png'
        if not original_path.is_file():raise ValueError('图标定位缺少原始页面')
        grid=ruled_grid(original_path,table)
        selected=[]
        for ref in refs:
            cell=cell_at(table,ref)
            if cell['state']!='unknown' or [cell['row'],cell['col']]!=ref:
                raise ValueError('图标目标必须是未识别单元格左上坐标')
            selected.append(cell)
        key=lambda c:(entry['key'],table_id,cell_id(c))
        replay_dir=os.environ.get('TABLE_ICON_OBSERVATION_REPLAY_DIR')
        if replay_dir:
            reused=[]; conflicts=[]
            for call_path in Path(replay_dir).glob('call-*-field-reinspect.json'):
                call=json.loads(call_path.read_text());data=call.get('input',{});contact=Path(data.get('contact_path',''))
                if (not call.get('contract_valid') or data.get('document_sha256')!=digest(entry['path'])
                        or data.get('region',{}).get('table_id')!=table_id or data.get('grid')!=grid
                        or data.get('table')!=verification_view(table) or not contact.is_file()
                        or digest(contact)!=data.get('contact_sha256') or data.get('page_hashes')!=[digest(original_path)]):
                    continue
                actual=[part.get('sha256') for msg in call.get('request',{}).get('messages',[])
                        if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
                if actual!=[*data['page_hashes'],data['contact_sha256']]:continue
                items=json.loads(call['content']).get('items',[]); by_id={x['id']:x for x in items}
                if len(by_id)!=len(items) or set(by_id)!=set(data.get('targets',{})):
                    continue
                if any(not ((v['state']=='value' and v['text'] in ('✓','✗')) or (v['state']=='unknown' and v['text']=='')) for v in items):continue
                for cell in selected:
                    ident=cell_id(cell);value=by_id.get(ident);target_data=data['targets'].get(ident,{})
                    expected=[grid['cols'][cell['col']]+1,grid['rows'][cell['row']]+1,grid['cols'][cell['col']+1],grid['rows'][cell['row']+1]]
                    if not value or value['state']!='value' or target_data.get('bbox_pixels')!=expected:continue
                    old=self.icon_candidates.get(key(cell))
                    if old and old['item']['text']!=value['text']:
                        conflicts.append(ident);self.icon_candidates.pop(key(cell),None);continue
                    if ident in conflicts:continue
                    self.icon_candidates[key(cell)]={'item':deepcopy(value),'call':str(call_path.resolve()),
                        'call_sha256':digest(call_path),'task':data['task'],'image_hashes':data['page_hashes'],
                        'grid':grid,'contact_path':data['contact_path'],'contact_sha256':data['contact_sha256']}
                    reused.append(ident)
            record['icon_candidate_reuse']={'ids':sorted(set(reused)),'conflicts':sorted(set(conflicts)),
                                            'status':'候选复用，仍须当前题独立原图核验'}
            if conflicts:raise ValueError('历史图标候选冲突：'+','.join(sorted(set(conflicts))))
        missing=[c for c in selected if key(c) not in self.icon_candidates]
        if missing:
            pages=sorted({p['page'] for p in region['parts']})
            paths=[entry['folder']/'input'/f'page-{page:03d}-original.png' for page in pages]
            if not all(p.is_file() for p in paths):raise ValueError('图标观察缺少原始页面')
            images=[]
            for page,original in zip(pages,paths):
                images.extend([{'type':'text','text':f'未旋转原始第{page}页，保留完整表上下文'},self.base.image_part(original.read_bytes())])
            sheet=icon_contact_sheet(original_path,grid,missing,self.output/f'icon-contact-{self.call_count+1:04d}.png')
            images.extend([{'type':'text','text':'以下接触图按原始实线网格裁切，标签cell:行:列从0开始，每块只包含相应单元格。'},self.base.image_part(sheet.read_bytes())])
            targets={}
            for c in missing:
                row_context=[{'row':v['row'],'col':v['col'],'text':v['text']} for v in table['cells']
                             if v['state']=='value' and v['row']<=c['row']<v['row']+v['rowspan']][:12]
                col_context=[{'row':v['row'],'col':v['col'],'text':v['text']} for v in table['cells']
                             if v['state']=='value' and v['col']<=c['col']<v['col']+v['colspan'] and v['row']<c['row']][:12]
                targets[cell_id(c)]={'row':c['row'],'col':c['col'],'state':c['state'],'text':c['text'],
                                     'row_context':row_context,'column_context':col_context,
                                     'bbox_pixels':[grid['cols'][c['col']]+1,grid['rows'][c['row']]+1,grid['cols'][c['col']+1],grid['rows'][c['row']+1]]}
            data={'task':task,'region':region,'table':verification_view(table),'targets':targets,
                  'coordinate_convention':'row和col均从0开始，row=0是逻辑首行，col=0是逻辑首列；以row_context和column_context确定交叉位置',
                  'document_sha256':digest(entry['path']),'page_hashes':[digest(p) for p in paths],
                  'grid':grid,'contact_path':str(sheet.resolve()),'contact_sha256':digest(sheet)}
            result=self.ask('field-reinspect',
                '仅在完整原图对应的指定行列位置识别图标。row和col从0开始，第一行是row=0，第一列是col=0，'
                '绝对不能将数字另加1；每个目标必须根据row_context行标题与column_context列标题定位交叉位置。'
                '绿色勾号规范输出✓，红色叉号规范输出✗；不能返回表头或行标题文本。'
                '不能根据邻格、颜色习惯或题目预期猜测。看不清或没有可判断符号一律state=unknown且text为空。'
                '只输出targets每个id一次，不多也不少，不重识别其他单元格。',data,images)
            items={v['id']:v for v in result['items']}
            if len(items)!=len(result['items']) or set(items)!=set(targets):
                raise ValueError('图标观察没有恰好覆盖请求位置')
            if any(not ((v['state']=='value' and v['text'] in ('✓','✗')) or
                        (v['state']=='unknown' and v['text']=='')) for v in items.values()):
                raise ValueError('图标观察混入行列标题、空白或其他文本，坐标契约无法确认；整批候选不得复用')
            call_path=self.output/f'call-{self.call_count:04d}-field-reinspect.json'
            call=json.loads(call_path.read_text());image_hashes=[p.get('sha256') for m in call['request']['messages'] if isinstance(m.get('content'),list) for p in m['content'] if p.get('type')=='image_url']
            if image_hashes!=[*data['page_hashes'],data['contact_sha256']]:raise ValueError('图标观察图片来源不匹配')
            for cell in missing:
                item=items[cell_id(cell)]
                if item['state']=='value' and item['text'] in ('✓','✗'):
                    self.icon_candidates[key(cell)]={'item':deepcopy(item),'call':str(call_path.resolve()),
                        'call_sha256':digest(call_path),'task':task,'image_hashes':data['page_hashes'],
                        'grid':grid,'contact_path':data['contact_path'],'contact_sha256':data['contact_sha256']}
        patched=deepcopy(table); changed=[]
        for original in selected:
            candidate=self.icon_candidates.get(key(original))
            if not candidate:continue
            cell=cell_at(patched,[original['row'],original['col']]);old=deepcopy(cell)
            cell.update(text=candidate['item']['text'],state='value',confidence=candidate['item']['confidence'])
            cell['source']={'method':'icon_observation','original':old,'document_sha256':digest(entry['path']),
                'table_id':table_id,'candidate':deepcopy(candidate)}
            changed.append({'ref':[cell['row'],cell['col']],'before':old,'candidate':deepcopy(candidate)})
        if not changed:
            record['icon_repair_reason']='目标位置没有可辨认的勾叉候选';return None
        record['icon_candidate_repairs']=changed
        record['icon_candidate_scope']='候选仅当前执行使用；原始解析不改写，每题仍独立核验'
        self.icon_review_sheet=icon_contact_sheet(original_path,grid,selected,self.output/f'icon-review-{self.call_count:04d}.png')
        return entry,region,table,patched

    def load_icon_replay(self, path, question, environment='TABLE_ICON_REPLAY_DIR', require_unknown=True):
        """只复用同题、同源、选表唯一且计划确因未识别而停止的历史记录。"""
        directory=os.environ.get(environment)
        if not directory:return None
        directory=Path(directory);found=[]
        task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
        for result_path in directory.glob('result-*.json'):
            prior=json.loads(result_path.read_text())
            if (str(prior.get('id'))!=str(question['id']) or prior.get('file_name')!=question['file_name']
                    or not prior.get('plan') or (require_unknown and prior['plan'].get('kind')!='unknown')
                    or (prior.get('selection') or {}).get('status')!='selected'):
                continue
            specs=[x for x in prior.get('parser_import_manifest',[]) if x.get('document_sha256')==digest(Path(path))]
            if len(specs)!=1 or digest(Path(specs[0]['raw_path']))!=specs[0]['raw_sha256']:continue
            same_task=False
            for call_path in (directory/'pipeline').glob('call-*-plan.json'):
                call=json.loads(call_path.read_text())
                if call.get('contract_valid') and call.get('input',{}).get('task')==task and json.loads(call['content'])==prior['plan']:
                    same_task=True;break
            if same_task:found.append((result_path,prior))
        if len(found)>1:raise ValueError('图标历史选表有多个匹配来源，不能自动选择')
        return found[0] if found else None

    def solve(self,path,question,record,candidate_id=None):
        self.active_question = question['question']
        call_start = self.call_count
        replay=self.load_icon_replay(path,question) if candidate_id is None else None
        if replay:
            prior_path,prior=replay
            reason=(prior.get('plan') or {}).get('reason',''); query=question['question']
            if re.search(r'图标|勾号|叉号|符号',reason) or (re.search(r'支持|功能',query) and re.search(r'空|unknown|无法|未识别',reason,re.I)):
                record.update(selection=deepcopy(prior['selection']),plan=deepcopy(prior['plan']),
                              unanswered_reason=prior.get('unanswered_reason'),
                              icon_reused_prior={'path':str(prior_path.resolve()),'sha256':digest(prior_path)})
                try:repair=self.repair_icon_cells(path,question,record)
                except ValueError as error:
                    record['icon_repair_reason']=str(error);record['unanswered_reason']='图标定向观察未完成：'+str(error);repair=None
                if not repair:
                    record['unanswered_reason']=record.get('icon_repair_reason') or prior.get('unanswered_reason') or '图标候选不足'
                    record.update(parser_import_manifest=self.imports,pipeline_calls=self.call_count-call_start,raw_output=json.dumps({'answer':''}))
                    return ''
                if repair:
                    entry,region,original,patched=repair;key=(entry['key'],region['table_id'])
                    self.tables[key]=patched;record['before_icon_repair']=deepcopy(prior)
                    self.icon_verification_active=True
                    try:result=super().solve(path,question,record,region['table_id'])
                    finally:self.tables[key]=original;self.icon_verification_active=False;self.icon_review_sheet=None
                    record['pipeline_calls']=self.call_count-call_start;record['parser_import_manifest']=self.imports
                    if record.get('answer_evidence'):
                        artifact=Path(record['answer_evidence']);evidence=json.loads(artifact.read_text())
                        evidence['coordinate_spaces']={'source':'page plus parser_position; icon candidates retain original page evidence'}
                        evidence['parser_validation_policy']=record.get('parser_validation_policy');atomic_json(artifact,evidence)
                    return result
        spatial = re.search(r'竖排.*(?:标题|表名)|(?:标题|表名).*竖排', question['question'])
        visual = re.search(r'二维码|图标|明细行|合计区|横向拍摄|手写批注|(?:红|橙|黄|绿|青|蓝|紫|黑|白|灰)色(?:标签|标记)', question['question'])
        if re.search(r'ASCII.{0,12}(?:线框|边框|表格线)', question['question'], re.I):
            visual = True
        # 颜色区域计数需要原图观察，文字解析本身不能提供颜色依据。
        if is_visual_count_task(question['question']) and re.search(
                r'(?:红|橙|黄|绿|青|蓝|紫|黑|白|灰)色(?:的)?(?:方块|色块|区域|单元格)', question['question']):
            visual = True
        column_question = bool(re.search(r'列数',question['question']) and question.get('answer_format')=='number')
        visual = bool(visual or column_question)
        quoted_existence = re.search(r'[“"「]是否(?:含有|包含|含)[^”"」]+[”"」]',question['question'])
        if quoted_existence and question.get('answer_format') != 'json_array':
            candidate_entry=self.locate(path)
            texts=[c.get('text','') for t in candidate_entry['parser_facts']['tables'] for c in t['cells']]
            texts += [c.get('text','') for c in candidate_entry['context']]
            routed=existence_attribute_route(question,texts)
            record['existence_routing']={'quoted_field':quoted_existence.group(0),'exact_field_present':not routed,
                'decision':'原图属性核验' if routed else '提取已有同名字段值'}
            visual = routed
        task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
        attributes = ordered_visual_attributes(task) if visual and mixed_visual_count_error(task) else []
        if visual and mixed_visual_count_error(task) and not attributes:
            record.update(unanswered_reason='多个属性需要分别建立证据；一个混合视觉计数不能代表任一属性，当前未确认逐属性映射',
                          visual_scope_status='multiple_attributes_unresolved',pipeline_calls=self.call_count-call_start,
                          raw_output=json.dumps({'answer':''}))
            return ''
        plan_replay=self.load_icon_replay(path,question,'TABLE_PLAN_REPLAY_DIR',False) if not spatial and not visual and candidate_id is None else None
        if plan_replay:
            prior_path,prior=plan_replay
            record['plan_replay_source']={'path':str(prior_path.resolve()),'sha256':digest(prior_path)}
            record['previous_plan']=deepcopy(prior['plan'])
            record['retry_feedback']='原计划在程序或质检阶段未完成：'+str(prior.get('error') or prior.get('unanswered_reason') or '未知问题')
            if prior.get('visual_column_count_candidate'):
                entry=self.locate(path);fact=self.observe_visual_property(entry,question,record)
                old_context=entry['context'];entry['context']=[*deepcopy(old_context),fact]
                record['visual_column_count_candidate']=True
                record['retry_feedback']='上一计划错误地统计了解析网格子集；必须直接使用独立核验的视觉列候选计数。'
                try:result=super().solve(path,question,record,prior['selection']['table_id'])
                finally:entry['context']=old_context
            else:
                result=super().solve(path,question,record,prior['selection']['table_id'])
            record['pipeline_calls']=self.call_count-call_start;record['parser_import_manifest']=self.imports
            if record.get('answer_evidence'):
                artifact=Path(record['answer_evidence']);evidence=json.loads(artifact.read_text())
                evidence['coordinate_spaces']={'source':'page plus parser_position in original service space; no synthesized cell bbox'}
                evidence['parser_validation_policy']=record.get('parser_validation_policy');atomic_json(artifact,evidence)
            return result
        entry = self.locate(path) if spatial or visual else None
        visual_previous_context = None
        if visual:
            visual_previous_context = entry['context']
            try:
                handwriting_value=('手写批注' in question['question'] and not re.search(r'是否|有无|有没有',question['question']))
                if attributes:
                    facts=[];failures=[]
                    for index,attribute in enumerate(attributes):
                        try:facts.append(self.observe_visual_property(entry,question,record,attribute,index))
                        except ValueError as error:failures.append({'attribute':attribute,'index':index,'reason':str(error)})
                    record['visual_attribute_failures']=failures
                    if failures:
                        # 其他属性仍单独核验并保存，不把已确认项误写成完整数组。
                        images=[self.base.image_part((entry['folder']/'input'/f'page-{i:03d}-original.png').read_bytes()) for i in range(1,len(entry['prepared']['page_paths'])+1)]
                        record['visual_attribute_reviews']=[]
                        for fact in facts:
                            targets={fact['id']:fact};attribute=fact['source']['target_attribute']
                            try:
                                verdict=self.ask_contract('verify',VERIFY+'\n只核验target_attribute这一项与目标表的对应关系。候选计数为程序对选中实例的数量，不是原图印刷数字；阳性证据只需确认选中的一处。checks仅覆盖targets。',
                                    {'task':task,'target_attribute':attribute,'targets':targets,'context':[fact],'scope':'单个属性及必要原图上下文'},images)
                                checks=self.checks_by_id(verdict['checks'],targets);check=checks[fact['id']]
                                accepted=(verdict['plan_valid'] and verdict['structure_valid'] and verdict['structure_confidence']>self.base.CONFIDENCE_HIGH
                                          and verified_match(fact,check) and check['confidence']>self.base.CONFIDENCE_HIGH)
                                record['visual_attribute_reviews'].append({'attribute':attribute,'target_id':fact['id'],'status':'passed' if accepted else 'not_passed','verdict':verdict,'call':str((self.output/f'call-{self.call_count:04d}-verify.json').resolve())})
                            except ValueError as error:
                                record['visual_attribute_reviews'].append({'attribute':attribute,'target_id':fact['id'],'status':'error','reason':str(error)})
                        record.update(unanswered_reason='多属性存在未确认项：'+json.dumps(failures,ensure_ascii=False),pipeline_calls=self.call_count-call_start,raw_output=json.dumps({'answer':''}))
                        return ''
                    record['multi_visual_attributes']=attributes
                else:
                    facts = self.observe_handwriting_text(entry,question,record) if handwriting_value else [self.observe_visual_property(entry, question, record)]
                    if column_question:record['visual_column_count_candidate']=True
            except ValueError as error:
                record.update(unanswered_reason=str(error), pipeline_calls=self.call_count-call_start,
                              raw_output=json.dumps({'answer':''}))
                return ''
            entry['context'] = [*deepcopy(entry['context']), *facts]
        previous_context = None
        if spatial:
            candidates = [c for c in entry['context'] if c['kind'] != 'document_page_count']
            images = self.crops(entry, {'table_id':'D0','parts':[{'page':p,'bbox':[0,0,1000,1000]} for p in sorted({c['source']['page'] for c in candidates})]}, self.base.PDF_DPI)
            selected = self.ask_contract('select-visual',
                '依据未旋转原始页面，定位题目所指的竖排标题或表名。不要按旋转后的左右判断。'
                '候选为已有表外文字，table_id和candidates使用输入的context_id；仅能够从原图确认文字、位置及题意时selected。'
                '表名与标准号须区分。找不到则not_found，多项同样满足则ambiguous。不生成答案、不识别整表。',
                {'task':{k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')},
                 'contexts':[{'context_id':c['id'],'text':c['text'],'page':c['source']['page']} for c in candidates]}, images)
            record['spatial_context_selection'] = selected
            matches = [c for c in candidates if c['id']==selected.get('table_id')]
            if selected['status']=='selected' and len(matches)==1 and selected['candidates']==[matches[0]['id']]:
                previous_context = entry['context']
                entry['context'] = deepcopy(matches)
                candidate_id = 'D0'
            else:
                record.update(unanswered_reason='原始方向的表外位置未唯一确认：'+selected['reason'],
                              pipeline_calls=self.call_count-call_start, raw_output=json.dumps({'answer':''}))
                return ''
        try:
            result = super().solve(path,question,record,candidate_id)
        finally:
            if previous_context is not None:entry['context'] = previous_context
            if visual_previous_context is not None:entry['context'] = visual_previous_context
        if (not result and not visual and re.search(r'列数',question['question']) and question.get('answer_format')=='number'
                and (record.get('selection') or {}).get('status')=='selected'
                and any(q.get('status') in ('unresolved_fields','unresolved_structure') for q in record.get('quality_rounds',[]))):
            # 仅当已有计划的列结构确有冲突时，观察原图列边界；不重解析文档。
            prior=deepcopy(record);entry=self.locate(path)
            try:
                fact=self.observe_visual_property(entry,question,record)
                old_context=entry['context'];entry['context']=[*deepcopy(old_context),fact]
                record['prior_column_attempt']=prior;record['visual_column_count_candidate']=True
                try:result=super().solve(path,question,record,prior['selection']['table_id'])
                finally:entry['context']=old_context
            except ValueError as error:
                record['unanswered_reason']='原图列边界未能定向确认：'+str(error)
        if not result and (record.get('plan') or {}).get('kind')=='unknown':
            reason=(record.get('plan') or {}).get('reason',''); query=question['question']
            if re.search(r'图标|勾号|叉号|符号',reason) or (re.search(r'支持|功能',query) and re.search(r'空|unknown|无法|未识别',reason,re.I)):
                before=deepcopy(record)
                try:repair=self.repair_icon_cells(path,question,record)
                except ValueError as error:
                    record['icon_repair_reason']=str(error);record['unanswered_reason']='图标定向观察未完成：'+str(error);repair=None
                if repair:
                    entry,region,original,patched=repair
                    key=(entry['key'],region['table_id']);self.tables[key]=patched
                    record['before_icon_repair']=before
                    self.icon_verification_active=True
                    try:result=super().solve(path,question,record,region['table_id'])
                    finally:self.tables[key]=original;self.icon_verification_active=False;self.icon_review_sheet=None
        if not result and record.get('selection',{}).get('status')=='not_found':
            # 单题只查一次行标签，不将候选歧义或API错误扩大为整表重识别。
            prior=deepcopy(record)
            repair=self.repair_row_anchor(path,question,record)
            if repair:
                entry,region,original,patched=repair
                key=(entry['key'],region['table_id'])
                old_labels=region.get('row_labels')
                old_headers=region.get('headers')
                self.tables[key]=patched
                region['row_labels']=[c['text'] for c in patched['cells'] if c['col']<2 and c['text']]
                region['headers']=' | '.join(c['text'] for c in patched['cells'] if c['row']<4)
                record['before_anchor_repair']=prior
                try:
                    result=super().solve(path,question,record,candidate_id)
                finally:
                    self.tables[key]=original
                    region['row_labels']=old_labels
                    region['headers']=old_headers

        record['pipeline_calls'] = self.call_count - call_start
        record['parser_import_manifest'] = self.imports
        if record.get('answer_evidence'):
            artifact = Path(record['answer_evidence']); evidence=json.loads(artifact.read_text())
            evidence['coordinate_spaces']={'source':'page plus parser_position in original service space; no synthesized cell bbox'}
            evidence['parser_validation_policy']=record.get('parser_validation_policy')
            atomic_json(artifact,evidence)
        return result


def validate_targeted_review_result(record, evidence, question, base, calls, allow_unresolved=False):
    """重建原质检和逐字段修订，防止恢复答案脱离原图及原计划。"""
    checkpoint = record.get('checkpoint')
    if checkpoint:
        path = Path(checkpoint.get('record', ''))
        if not path.is_file() or digest(path) != checkpoint.get('sha256'):
            raise ValueError('恢复检查点不存在或已变化')
        saved = json.loads(path.read_text())
        prior_rounds = saved.get('local_reinspection_rounds', [])
        if (not prior_rounds or str(saved.get('id')) != str(question['id'])
                or record.get('local_reinspection_rounds', [])[:len(prior_rounds)] != prior_rounds
                or checkpoint.get('continued_from') != prior_rounds[-1].get('call')):
            raise ValueError('恢复检查点与实际局部复核不一致')
    provenance = evidence['targeted_review_provenance']
    prior_path = Path(provenance['record'])
    artifact_path = Path(provenance['artifact'])
    if digest(prior_path) != provenance['record_sha256'] or digest(artifact_path) != provenance['artifact_sha256']:
        raise ValueError('恢复入口原始质检或证据已变化')
    prior = json.loads(prior_path.read_text())
    original = json.loads(artifact_path.read_text())
    if str(prior.get('id')) != str(question['id']) or prior.get('answer_evidence') != str(artifact_path):
        raise ValueError('恢复入口的原始证据与题目不一致')
    validate_saved_result(prior, original, question, base, prior_path.parent / 'pipeline', evidence_only=True)
    table = deepcopy(original['table'])
    plan = evidence['plan']
    old_plan = original['plan']
    if ({k:v for k,v in plan.items() if k != 'evidence_refs'} !=
            {k:v for k,v in old_plan.items() if k != 'evidence_refs'} or evidence['context'] != original['context']):
        raise ValueError('恢复入口改变了已核验计划或表外上下文')
    task = {k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
    checks = {item['id']:deepcopy(item['verification']) for item in prior['quality_rounds'][-1]['fields']}
    structure_score = prior['quality_rounds'][-1]['structure_confidence']
    image_hashes = lambda call: [part.get('sha256') for message in call.get('request', {}).get('messages', [])
        if isinstance(message.get('content'), list) for part in message['content'] if part.get('type') == 'image_url']
    chunks = prior.get('chunked_verification', {}).get('chunks', [])
    if chunks:
        original_images = image_hashes(json.loads(Path(chunks[0]['path']).read_text()))
    else:
        original_calls = [json.loads(path.read_text()) for path in (prior_path.parent / 'pipeline').glob('call-*-verify.json')]
        image_sets = {tuple(image_hashes(call)) for call in original_calls if call.get('contract_valid')
                      and call.get('input', {}).get('task') == task and call.get('input', {}).get('plan') == old_plan
                      and image_hashes(call)}
        if len(image_sets) != 1:
            raise ValueError('恢复入口原核验图片来源不唯一')
        original_images = list(next(iter(image_sets)))
    folder = Path(record['recognition_artifact'])
    cache_manifest = json.loads((folder / 'parser-manifest.json').read_text())
    prepared_images = []
    for name, expected in sorted(cache_manifest.get('artifacts', {}).items()):
        if re.fullmatch(r'input/page-\d+-prepared\.png', name):
            if digest(folder / name) != expected:
                raise ValueError('恢复入口预处理页面已变化')
            prepared_images.append(expected)
    reviews = record.get('targeted_verification', [])
    review_index = 0
    for revision in record.get('local_reinspection_rounds', []):
        if len(revision.get('scope', [])) != 1:
            raise ValueError('定向复核范围不唯一')
        key = revision['scope'][0]
        row, col = map(int, key.split(':')[1:])
        cell = cell_at(table, [row,col])
        if cell != revision['before']:
            raise ValueError('定向复核原值与原始表格不一致')
        call_path, crop = Path(revision['call']), Path(revision['crop'])
        if digest(call_path) != revision['sha256'] or digest(crop) != revision['crop_sha256']:
            raise ValueError('定向复核调用或裁剪已变化')
        call = json.loads(call_path.read_text())
        payload = call.get('input', {})
        review_info = revision.get('review', {})
        page = review_info.get('page')
        original_page = Path(record['recognition_artifact']) / 'input' / f'page-{page:03d}-original.png'
        invalid = revision.get('status') == 'invalid'
        if (call.get('phase') != 'field-reinspect'
                or (invalid and (call.get('contract_valid') is not False or not revision.get('error')))
                or (not call.get('contract_valid') and not (invalid and revision.get('error') == call.get('validation_error')))
                or str(question['id']) not in call.get('question_ids', []) or payload.get('task') != task
                or payload.get('targets') != {key:revision['before']}
                or payload.get('review') != review_info or not original_page.is_file()
                or digest(original_page) != review_info.get('original_sha256')
                or image_hashes(call) != [digest(original_page), digest(crop)]
                or (not invalid and revision['candidate'] not in json.loads(call['content']).get('items', []))):
            raise ValueError('定向复核与原题、字段或实际响应不一致')
        if invalid:
            if revision['before'] != revision['after']:
                raise ValueError('无效复核不能修改字段')
            continue
        cell.update(text=revision['after']['text'], state=revision['after']['state'])
        if cell != revision['after']:
            raise ValueError('定向复核改变了未授权的字段属性')
        if revision:
            review = reviews[review_index] if review_index < len(reviews) else None
            if not review or review.get('target') != key or digest(review['call']) != review['sha256']:
                raise ValueError('变化字段缺少独立原图核验')
            verify = json.loads(Path(review['call']).read_text())
            payload = verify.get('input', {})
            if (not verify.get('contract_valid') or verify.get('phase') != 'verify'
                    or payload.get('task') != task or payload.get('plan') != plan
                    or not image_hashes(verify) or
                    (image_hashes(verify) != original_images and image_hashes(verify) != prepared_images)
                    or json.loads(verify['content']) != review['verdict']):
                raise ValueError('变化字段的核验调用不匹配')
            targets = dependencies(table, original['context'], plan, task['question_type'], number, normalize)
            names = review.get('targets', [key])
            if (not names or key not in names or len(set(names)) != len(names)
                    or any(name not in targets for name in names)
                    or payload.get('targets') != {name:targets[name] for name in names}):
                raise ValueError('变化字段的核验目标不匹配')
            checks.update(Pipeline.checks_by_id(review['verdict']['checks'], {name:targets[name] for name in names}))
            structure_score = review['verdict']['structure_confidence']
            review_index += 1
    if table != evidence['table'] or review_index != len(reviews):
        raise ValueError('恢复后的表格或核验记录不能由原始结果重建')
    old_targets = dependencies(original['table'], original['context'], old_plan, task['question_type'], number, normalize)
    targets = dependencies(table, original['context'], plan, task['question_type'], number, normalize)
    checks = {key:value for key,value in checks.items() if key in targets}
    if set(checks) != set(targets):
        raise ValueError('恢复后的计划存在未核验依赖')
    average = average_confidence_route(checks, structure_score, base.CONFIDENCE_HIGH)
    if (average != record['quality_rounds'][-1].get('average_confidence') or
            (not allow_unresolved and average['status'] != 'accepted')):
        raise ValueError('恢复后的平均置信度不能重建')
    if allow_unresolved:
        return {'table':table, 'context':original['context'], 'plan':plan,
                'checks':checks, 'structure_confidence':structure_score}
    answer = execute(table, plan, task['question_type'], original['context'])
    formatted = base.format_answer(json.dumps({'answer':answer}, ensure_ascii=False), task['question_type'], task['answer_format'], [table,original['context']])
    if formatted != record.get('answer'):
        raise ValueError('恢复答案不能由原证据和程序执行重建')


def validate_saved_result(record, evidence, question, base, calls, evidence_only=False):
    """汇总前重新核对解析来源、真实核验响应、计划依赖和确定性重算。"""
    source_hash = digest(base.FILES_DIR / question['file_name'])
    failure = record.get('prior_contract_failure')
    if failure:
        old_record, old_evidence, old_call = (Path(failure[name]) for name in ('record','evidence','call'))
        if any(not path.is_file() or digest(path) != failure[name + '_sha256']
               for name,path in (('record',old_record),('evidence',old_evidence),('call',old_call))):
            raise ValueError('契约失败的来源记录、证据或调用已变化')
        previous = json.loads(old_record.read_text()); old = json.loads(old_evidence.read_text())
        call = json.loads(old_call.read_text()); data = call.get('input', {})
        task = {key:question.get(key) or '' for key in ('question_type','question','table_hint','answer_format')}
        try:
            conflicting = [item for item in json.loads(call['content'])['checks'] if item.get('id') == failure['target']
                           and item.get('state') in ('empty','unknown') and item.get('text')]
        except (ValueError, KeyError, TypeError):
            conflicting = []
        if (str(previous.get('id')) != str(question['id']) or previous.get('answer_evidence') != str(old_evidence)
                or call.get('validation_error') != '复核文本与状态不一致' or data.get('task') != task
                or data.get('plan') != old.get('plan') or data.get('table') != verification_view(old.get('table', {}))
                or data.get('context') != old.get('context') or len(conflicting) != 1
                or not any(item.get('scope') == [failure['target']] for item in record.get('local_reinspection_rounds', []))):
            raise ValueError('契约失败记录与当前题目或局部复核不匹配')
    matches = [x for x in record.get('parser_import_manifest',[]) if x['document_sha256']==source_hash]
    if len(matches)!=1 or digest(matches[0]['raw_path'])!=matches[0]['raw_sha256']:
        raise ValueError('解析答案来源文件或原始响应已变化')
    table,context,plan = evidence['table'],evidence['context'],evidence['plan']
    for revision in record.get('local_reinspection_rounds', []):
        call_path=Path(revision.get('call','')); crop_path=Path(revision.get('crop',''))
        if (revision.get('source_sha256')!=source_hash or not call_path.is_file()
                or digest(call_path)!=revision.get('sha256') or not crop_path.is_file()
                or digest(crop_path)!=revision.get('crop_sha256')):
            raise ValueError('局部字段修订的文件、裁剪或调用来源不匹配')
        call=json.loads(call_path.read_text());payload=call.get('input',{});review=revision.get('review',{})
        page=review.get('page'); original=Path(record['recognition_artifact'])/'input'/f'page-{page:03d}-original.png'
        images=[part.get('sha256') for message in call.get('request',{}).get('messages',[])
                if isinstance(message.get('content'),list) for part in message['content'] if part.get('type')=='image_url']
        if (not original.is_file() or digest(original)!=review.get('original_sha256')
                or images!=[digest(original),digest(crop_path)] or not call.get('contract_valid')
                or call.get('phase')!='field-reinspect' or payload.get('review')!=review
                or payload.get('task')!={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
                or payload.get('targets')!={revision['scope'][0]:revision['before']}
                or revision['candidate'] not in json.loads(call['content']).get('items',[])):
            raise ValueError('局部字段修订与原图、任务或模型响应不一致')
    header_review = record.get('header_structure_review')
    if header_review and header_review.get('status') != 'unresolved':
        artifact = Path(header_review.get('artifact', '')); call_path = Path(header_review.get('call', ''))
        if (not artifact.is_file() or digest(artifact) != header_review.get('sha256')
                or not call_path.is_file() or digest(call_path) != header_review.get('call_sha256')):
            raise ValueError('表头修订来源不存在或已变化')
        saved = json.loads(artifact.read_text()); call = json.loads(call_path.read_text())
        if (saved.get('table') != table or saved.get('table_id') != evidence.get('table_id')
                or call.get('phase') != 'recognize-header' or not str(call.get('model', '')).lower().startswith('qwen')
                or str(question['id']) not in (call.get('question_ids') or []) or not call.get('contract_valid')):
            raise ValueError('表头候选与实际识别调用或最终结构不一致')
        original = json.loads(call['content'])
        if (original.get('row_count') != table.get('row_count') or original.get('col_count') != table.get('col_count')
                or len(original.get('cells', [])) != len(table.get('cells', []))):
            raise ValueError('表头候选尺寸或单元格与模型输出不一致')
        normalizations = saved.get('normalizations', [])
        changes = {(item['row'], item['col']): item for item in normalizations}
        if len(changes) != len(normalizations):
            raise ValueError('表头空白状态修订重复')
        for raw, cell in zip(original['cells'], table['cells']):
            change = changes.pop((raw['row'], raw['col']), None)
            text_ok = raw['text'] == cell['text']
            if change is not None:
                text_ok = (raw['state'] == cell['state'] == 'empty' and raw['text'].strip().casefold() == 'empty'
                           and change.get('before') == raw['text'] and change.get('after') == cell['text'] == '')
            if (not text_ok or any(raw.get(k) != cell.get(k) for k in ('row','col','rowspan','colspan','state','confidence'))
                    or any(raw['source'].get(k) != cell['source'].get(k) for k in ('part','bbox'))):
                raise ValueError('表头候选单元格与模型输出不一致')
        if changes:
            raise ValueError('表头空白修订未对应模型单元格')
        folder = Path(record['recognition_artifact']); manifest = json.loads((folder/'parser-manifest.json').read_text())
        facts_path = folder/'facts.json'
        if not facts_path.is_file() or digest(facts_path) != manifest['artifacts'].get('facts.json'):
            raise ValueError('表头修订的解析事实来源不匹配')
        region = call.get('input', {}).get('region', {})
        parts = region.get('parts', [])
        crops = [folder / f"{evidence.get('table_id')}-part-{i}-normal.png" for i in range(len(parts))]
        images = [part.get('sha256') for message in call.get('request', {}).get('messages', [])
                  if isinstance(message.get('content'), list) for part in message['content'] if part.get('type') == 'image_url']
        if (region.get('table_id') != evidence.get('table_id') or not parts
                or not all(path.is_file() for path in crops) or images != [digest(path) for path in crops]):
            raise ValueError('表头识别图片与文档区域来源不一致')
        originals = [item for item in json.loads(facts_path.read_text())['tables'] if item['table_id'] == evidence.get('table_id')]
        if (len(originals) != 1 or originals[0]['row_count'] != table['row_count']
                or originals[0]['col_count'] != table['col_count']):
            raise ValueError('表头修订与原始解析的完整行列数不一致')
        if len(parts) != 1 or parts[0].get('page') != originals[0]['source']['page'] or parts[0].get('bbox') != [0,0,1000,1000]:
            raise ValueError('表头识别区域未对应原解析表所在页')
    task_check={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
    for reference in (record.get('plan_replay_source'),record.get('icon_reused_prior')):
        if not reference:continue
        path=Path(reference['path'])
        if not path.is_file() or digest(path)!=reference['sha256']:
            raise ValueError('历史计划来源不存在或已变化')
        prior=json.loads(path.read_text());prior_selection=prior.get('selection') or {}
        if (str(prior.get('id'))!=str(question['id']) or prior.get('file_name')!=question['file_name']
            or prior_selection.get('status')!='selected' or prior_selection.get('table_id')!=evidence.get('table_id')):
            raise ValueError('历史计划或选表与当前题目不一致')
        if not any(c.get('document_sha256')==source_hash and Path(c.get('raw_path','')).is_file()
                   and digest(Path(c['raw_path']))==c.get('raw_sha256') for c in prior.get('parser_import_manifest',[])):
            raise ValueError('历史计划文档来源不一致')
        calls_prior=path.parent/'pipeline'; matched_prior=False
        for item in calls_prior.glob('call-*-plan.json'):
            call=json.loads(item.read_text())
            if call.get('contract_valid') and call.get('input',{}).get('task')==task_check and json.loads(call['content'])==prior.get('plan'):
                matched_prior=True;break
        if not matched_prior:raise ValueError('历史计划缺少同题模型调用记录')
    metadata = [c for c in context if c.get('kind')=='document_page_count']
    if metadata:
        import fitz
        with fitz.open(base.FILES_DIR / question['file_name']) as pdf:
            pages = pdf.page_count
        if any(c['text']!=str(pages) or c['source'].get('document_sha256')!=source_hash or c['source'].get('value')!=pages for c in metadata):
            raise ValueError('文件页数事实与当前PDF不匹配')
    layouts = [c for c in context if c.get('kind')=='parsed_page_table_count']
    if layouts:
        spec=matches[0]
        parsed=convert(json.loads(Path(spec['raw_path']).read_text()), source_hash, spec['raw_sha256'], spec['configuration'])
        for c in layouts:
            blocks=[t for t in parsed['tables'] if t['source']['page']==c['source']['page']]
            if c['text']!=str(len(blocks)) or c['source'].get('layout_ids')!=[t['source']['layout_id'] for t in blocks]:
                raise ValueError('页级候选表块统计与原始解析不一致')
    targets=dependencies(table,context,plan,question['question_type'],number,normalize)
    task={k:question.get(k) or '' for k in ('question_type','question','table_hint','answer_format')}
    semantic_error=conditional_selection_error(task,plan)
    if semantic_error:raise ValueError('合并时计划语义检查未通过：'+semantic_error)
    if record.get('visual_column_count_candidate'):
        reason=visual_column_plan_error(plan)
        if reason:raise ValueError('合并时列结构计划检查未通过：'+reason)
    validated_icon_calls=set()
    for item in (x for x in targets.values() if x.get('source',{}).get('method')=='icon_observation'):
        source=item['source']; candidate=source['candidate']; original=source['original']
        folder=Path(record['recognition_artifact']); manifest=json.loads((folder/'parser-manifest.json').read_text())
        facts_path=folder/'facts.json'
        if digest(facts_path)!=manifest['artifacts'].get('facts.json') or source.get('document_sha256')!=source_hash:
            raise ValueError('图标候选的解析事实或文件来源不匹配')
        facts=json.loads(facts_path.read_text()); tables=[t for t in facts['tables'] if t['table_id']==source['table_id']]
        if len(tables)!=1:raise ValueError('图标候选表不存在或不唯一')
        prior=[c for c in tables[0]['cells'] if c['row']==item['row'] and c['col']==item['col']]
        if len(prior)!=1 or prior[0]!=original or original['state']!='unknown':
            raise ValueError('图标候选覆盖了非unknown的解析事实')
        call_path=Path(candidate['call'])
        if not call_path.is_file() or digest(call_path)!=candidate['call_sha256']:
            raise ValueError('图标观察调用不存在或已变化')
        call=json.loads(call_path.read_text()); data=call.get('input',{}); ident=cell_id(item)
        originals=[folder/'input'/f"page-{part['page']:03d}-original.png" for part in sorted(data.get('region',{}).get('parts',[]),key=lambda p:p['page'])]
        originals=list(dict.fromkeys(originals));hashes=[digest(x) for x in originals if x.is_file()]
        images=[part.get('sha256') for msg in call.get('request',{}).get('messages',[])
                if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
        obs=json.loads(call['content']); observed=[x for x in obs.get('items',[]) if x.get('id')==ident]
        contact=Path(candidate.get('contact_path',''))
        if str(call_path) not in validated_icon_calls:
            if len(originals)!=1 or not originals[0].is_file():raise ValueError('图标候选缺少唯一原图')
            grid=ruled_grid(originals[0],tables[0])
            if grid!=candidate.get('grid') or data.get('grid')!=grid:
                raise ValueError('图标候选网格与当前原图不匹配')
            if not contact.is_file() or digest(contact)!=candidate.get('contact_sha256') or data.get('contact_sha256')!=candidate.get('contact_sha256'):
                raise ValueError('图标接触图不存在或已变化')
            all_cells=[]
            for target_id,target in data.get('targets',{}).items():
                r,c=target['row'],target['col'];expected=[grid['cols'][c]+1,grid['rows'][r]+1,grid['cols'][c+1],grid['rows'][r+1]]
                if target_id!=f'cell:{r}:{c}' or target.get('bbox_pixels')!=expected:
                    raise ValueError('图标目标坐标与实线网格不匹配')
                all_cells.append({'row':r,'col':c})
            with tempfile.TemporaryDirectory() as tmp:
                rebuilt=icon_contact_sheet(originals[0],grid,all_cells,Path(tmp)/'contact.png')
                if digest(rebuilt)!=candidate['contact_sha256']:raise ValueError('图标接触图与原图及网格不一致')
            if any(not ((v['state']=='value' and v['text'] in ('✓','✗')) or (v['state']=='unknown' and v['text']=='')) for v in obs.get('items',[])):
                raise ValueError('图标观察输出含有无关文字或不确定空白')
            validated_icon_calls.add(str(call_path))
        if (not call.get('contract_valid') or call.get('phase')!='field-reinspect'
            or data.get('document_sha256')!=source_hash or data.get('region',{}).get('table_id')!=source['table_id']
            or data.get('task')!=candidate['task'] or data.get('page_hashes')!=candidate['image_hashes']
            or hashes!=candidate['image_hashes'] or images!=[*hashes,candidate['contact_sha256']]
            or any(data.get('targets',{}).get(ident,{}).get(k)!=v for k,v in
                   {'row':original['row'],'col':original['col'],'state':'unknown','text':original['text']}.items())
            or not data.get('coordinate_convention') or data.get('table') is None
            or len(observed)!=1 or observed[0]!=candidate['item'] or item['text']!=observed[0]['text']
            or item['state']!='value' or item['text'] not in ('✓','✗')):
            raise ValueError('图标候选缺少匹配原图的观察记录')
    for item in (x for x in targets.values() if x.get('kind')=='visual_title_candidate'):
        source=item['source'];obs=source.get('observation',{});folder=Path(record['recognition_artifact']);hashes=source.get('page_hashes',[])
        originals=[folder/'input'/f'page-{i:03d}-original.png' for i in range(1,len(hashes)+1)]
        call_path=Path(source.get('observation_call',''))
        if (not hashes or not all(p.is_file() for p in originals) or [digest(p) for p in originals]!=hashes
            or source.get('document_sha256')!=source_hash or not call_path.is_file()
            or digest(call_path)!=source.get('observation_call_sha256') or obs.get('status')!='observed'
            or not obs.get('complete') or len(obs.get('items',[]))!=1):
            raise ValueError('标题候选来源或原图不匹配')
        call=json.loads(call_path.read_text());data=call.get('input',{});candidate=obs['items'][0]
        images=[part.get('sha256') for msg in call.get('request',{}).get('messages',[])
                if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
        if (not call.get('contract_valid') or call.get('phase')!='visual-title' or data.get('task')!=task
            or data.get('page_hashes')!=hashes or images!=hashes or json.loads(call['content'])!=obs
            or item['text']!=candidate['text'] or source['page']!=candidate['page'] or source['bbox']!=candidate['bbox']):
            raise ValueError('标题候选缺少匹配原图和任务的观察记录')
    for item in (x for x in targets.values() if x.get('kind')=='visual_handwriting'):
        source=item['source'];obs=source.get('observation',{});index=source.get('item_index');folder=Path(record['recognition_artifact'])
        ambiguous=handwriting_ambiguity(task,obs)
        if ambiguous:raise ValueError(ambiguous)
        hashes=source.get('page_hashes',[]);originals=[folder/'input'/f'page-{i:03d}-original.png' for i in range(1,len(hashes)+1)]
        call_path=Path(source.get('observation_call',''))
        if (not hashes or not all(x.is_file() for x in originals) or [digest(x) for x in originals]!=hashes
            or source.get('document_sha256')!=source_hash or source.get('task')!=task
            or not call_path.is_file() or digest(call_path)!=source.get('observation_call_sha256')
            or obs.get('status')!='observed' or not obs.get('complete') or type(index) is not int or not 0<=index<len(obs.get('items',[]))):
            raise ValueError('手写候选来源或原图不匹配')
        observed=obs['items'][index];call=json.loads(call_path.read_text());data=call.get('input',{})
        images=[part.get('sha256') for msg in call.get('request',{}).get('messages',[])
                if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
        if (not call.get('contract_valid') or call.get('phase')!='visual-handwriting' or data.get('task')!=task
            or data.get('page_hashes')!=hashes or images!=hashes or json.loads(call['content'])!=obs
            or item['text']!=observed['text'] or source['page']!=observed['page'] or source['bbox']!=observed['bbox']):
            raise ValueError('缺少匹配手写原文和原图的视觉观察记录')
    for item in (x for x in targets.values() if x.get('kind')=='visual_candidate_count'):
        if mixed_visual_count_error(task):
            reason=multiple_attribute_plan_error(task,plan)
            if reason:raise ValueError(reason)
            attrs=ordered_visual_attributes(task);idx=item['source'].get('attribute_index')
            if type(idx) is not int or not 0<=idx<len(attrs) or item['source'].get('target_attribute')!=attrs[idx] or item['id']!=f'VISUAL_CANDIDATE_COUNT_{idx}':
                raise ValueError('多属性候选与题目顺序或属性不匹配')
        rounds_to_check = ([] if record.get('quality_rounds',[{}])[-1].get('policy') == 'question_average_confidence_v1'
                           else record.get('quality_rounds',[]))
        for round_info in rounds_to_check:
            if (round_info.get('status')=='visual_observation_conflict' or
                    any(f.get('id')==item['id'] and f.get('route')!='accepted' for f in round_info.get('fields',[]))):
                raise ValueError('历史原图核验与视觉观察存在未解决冲突，不能合并')
        source=item['source']; obs=source.get('observation',{}); folder=Path(record['recognition_artifact'])
        hashes=source.get('page_hashes',[])
        originals=[folder/'input'/f'page-{i:03d}-original.png' for i in range(1,len(hashes)+1)]
        prior=source.get('prior_review')
        if prior:
            prior_path=Path(prior.get('path',''))
            if not prior_path.is_file() or digest(prior_path)!=prior.get('sha256'):
                raise ValueError('定向复核的历史冲突记录不存在或已变化')
            prior_result=json.loads(prior_path.read_text())
            if (str(prior_result.get('id'))!=str(question['id']) or prior_result.get('file_name')!=question['file_name']
                    or prior_result.get('visual_observation')!=prior.get('previous_observation')):
                raise ValueError('定向复核的历史观察与当前题目不匹配')
            conflicts=[{'reason':f['verification']['reason'],'observed':f['verification']['text']}
                       for q in prior_result.get('quality_rounds',[]) for f in q.get('fields',[])
                       if f.get('id')=='VISUAL_CANDIDATE_COUNT' and f.get('route')!='accepted']
            if not conflicts or conflicts!=prior.get('conflicts'):
                raise ValueError('定向复核的历史冲突依据不匹配')
            old=prior['previous_observation'];signature=lambda o:sorted((x['page'],tuple(x['bbox'])) for x in o['matches'])
            if signature(obs)==signature(old) and obs['scope']==old['scope']:
                raise ValueError('定向复核没有改变存在冲突的观察范围或候选')
        selected=source.get('selected_indexes',list(range(len(obs.get('matches',[])))))
        if (not isinstance(selected,list) or len(set(selected))!=len(selected)
                or any(type(i) is not int or not 0<=i<len(obs.get('matches',[])) for i in selected)):
            raise ValueError('视觉候选选择索引不合法')
        if len(selected)!=len(obs.get('matches',[])) and not (len(selected)==1 and (task.get('answer_format')=='string' or source.get('target_attribute'))
                and re.search(r'是否|有无|有没有',task.get('question','')) and source.get('selection_purpose')=='存在性阳性证据'):
            raise ValueError('视觉候选范围被不恰当地缩减')
        if (not hashes or not all(p.is_file() for p in originals)
                or [digest(p) for p in originals]!=hashes or source.get('document_sha256')!=source_hash
                or source.get('task')!=task or obs.get('status')!='observed' or not obs.get('complete')
                or item['text']!=str(len(selected))):
            raise ValueError('视觉候选事实或原图来源不匹配')
        scope_review=source.get('scope_review')
        if scope_review or (len(obs['matches'])>1 and is_visual_count_task(source.get('target_attribute') or task['question'])):
            if not scope_review: raise ValueError('多对象视觉统计缺少独立语义范围核验')
            scope_path=Path(scope_review.get('path',''))
            if not scope_path.is_file() or digest(scope_path)!=scope_review.get('sha256'):
                raise ValueError('视觉范围核验来源不存在或已变化')
            scope_call=json.loads(scope_path.read_text()); scope=scope_review['result']; data=scope_call.get('input',{})
            images=[part.get('sha256') for msg in scope_call.get('request',{}).get('messages',[])
                    if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
            if (not scope_call.get('contract_valid') or scope_call.get('phase')!='visual-scope-review'
                    or data.get('task')!=task or data.get('observation')!=obs or data.get('page_hashes')!=hashes
                    or images!=hashes or json.loads(scope_call['content'])!=scope or scope.get('status')!='clear'
                    or scope.get('confidence',0)<=base.CONFIDENCE_HIGH
                    or sorted(scope.get('included',[]))!=list(range(len(obs['matches'])))):
                raise ValueError('视觉对象语义范围未被独立核验')
        found=False
        path=Path(source.get('observation_call',''))
        if not path.is_file() or digest(path)!=source.get('observation_call_sha256'):
            raise ValueError('视觉观察来源调用不存在或已变化')
        for path in [path]:
            call=json.loads(path.read_text()); data=call.get('input',{})
            if not call.get('contract_valid') or data.get('task')!=task or data.get('page_hashes')!=hashes or data.get('prior_review')!=prior or data.get('target_attribute')!=source.get('target_attribute'):
                continue
            actual=[part.get('sha256') for msg in call.get('request',{}).get('messages',[])
                    if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
            if actual==hashes and json.loads(call['content'])==obs: found=True
        if not found: raise ValueError('缺少匹配原图及题目的视觉观察调用')
    matched=False
    if record.get('quality_rounds') and record['quality_rounds'][-1].get('column_structure_verdict'):
        expressions=plan.get('expressions',[])
        if not (re.search(r'列数',task.get('question','')) and task.get('answer_format')=='number'
                and plan.get('kind')=='calculate' and not plan.get('as_array') and len(expressions)==1
                and expressions[0].get('op')=='count_columns' and not plan.get('rules') and not plan.get('context_ids')
                and expressions[0]['columns']==list(range(table['col_count']))
                and all(ref in [[expressions[0]['row'],col] for col in expressions[0]['columns']] for ref in plan.get('evidence_refs',[]))):
            raise ValueError('列结构核验不能用于其他计划或不完整列范围')
        for p in calls.glob('call-*-column-structure-verify.json'):
            d=json.loads(p.read_text());data=d.get('input',{});pages=data.get('page_numbers',[])
            originals=[Path(record['recognition_artifact'])/'input'/f'page-{i:03d}-original.png' for i in pages]
            images=[part.get('sha256') for msg in d.get('request',{}).get('messages',[])
                    if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
            if (not d.get('contract_valid') or data.get('task')!=task or data.get('plan')!=plan
                or data.get('table_id')!=(record.get('selection') or {}).get('table_id')
                or data.get('row_count')!=table['row_count'] or data.get('col_count')!=table['col_count']
                or not pages or not all(x.is_file() for x in originals)
                or [digest(x) for x in originals]!=data.get('page_hashes') or images!=data.get('page_hashes')):
                continue
            verdict=json.loads(d['content'])
            if (verdict==record['quality_rounds'][-1]['column_structure_verdict'] and verdict['plan_valid']
                and verdict['structure_valid'] and verdict['confidence']>base.CONFIDENCE_HIGH
                and verdict['column_count']==table['col_count'] and verdict['reason'].strip()):matched=True
    if record.get('quality_rounds') and record['quality_rounds'][-1].get('language_verdict'):
        from language_evidence import accept_language
        expressions=plan.get('expressions',[])
        if not (plan.get('kind')=='calculate' and len(expressions)==1 and expressions[0].get('op')=='language' and not plan['rules'] and not plan['context_ids']):
            raise ValueError('语言核验不能用于其他计划')
        expected={k:{'text':v['text'],'row':v['row'],'col':v['col']} for k,v in targets.items()}
        for p in calls.glob('call-*-language-verify.json'):
            d=json.loads(p.read_text());data=d.get('input',{})
            if d.get('contract_valid') and data.get('task')==task and data.get('targets')==expected:
                verdict=json.loads(d['content'])
                if verdict==record['quality_rounds'][-1]['language_verdict'] and accept_language(verdict,plan,targets,base.CONFIDENCE_HIGH)[0]:matched=True
    for p in calls.glob('call-*-verify.json'):
        d=json.loads(p.read_text());data=d.get('input',{})
        if not d.get('contract_valid') or data.get('task')!=task or data.get('plan')!=plan or data.get('targets')!=targets:
            continue
        v=json.loads(d['content'])
        try:checks=Pipeline.checks_by_id(v['checks'],targets)
        except ValueError:
            # 历史响应可能语法合法但缺少字段；只跳过该次，后续仍须找到与已接受质检完全一致的有效响应。
            continue
        reviews=record.get('quality_rounds',[{}])[-1].get('field_contract_reviews',[])
        for review in reviews:
            review_path=Path(review.get('path',''))
            if not review_path.is_file() or digest(review_path)!=review.get('sha256'):
                raise ValueError('字段契约复核记录不存在或已变化')
            call=json.loads(review_path.read_text());data_review=call.get('input',{});keys=review.get('targets',[])
            if (not call.get('contract_valid') or data_review.get('task')!=task or data_review.get('plan')!=plan
                    or data_review.get('targets')!={k:targets[k] for k in keys}
                    or json.loads(call['content'])!=review.get('verdict')):
                raise ValueError('字段契约复核与最终计划或字段不匹配')
            rv=review['verdict'];updated=Pipeline.checks_by_id(rv['checks'],data_review['targets'])
            if not (rv['plan_valid'] and rv['structure_valid'] and rv['structure_confidence']>base.CONFIDENCE_HIGH):
                raise ValueError('字段契约复核未通过')
            checks.update(updated)
        if any(t.get('source',{}).get('method')=='icon_observation' for t in targets.values()):
            review=data.get('icon_review_sheet') or {};review_path=Path(review.get('path',''))
            if not review_path.is_file() or digest(review_path)!=review.get('sha256'):continue
            images=[part.get('sha256') for msg in d.get('request',{}).get('messages',[])
                    if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
            required={digest(Path(record['recognition_artifact'])/'input'/f"page-{t['source']['original']['source']['page']:03d}-original.png")
                      for t in targets.values() if t.get('source',{}).get('method')=='icon_observation'}
            if not required.issubset(images) or review['sha256'] not in images:continue
        link_review=record.get('quality_rounds',[{}])[-1].get('semantic_link_review')
        if link_review:
            ref=single_numeric_cell_ref(task,plan);target=cell_at(table,ref) if ref is not None else None
            target_id=cell_id(target) if target else None
            failed=[key for key,item in targets.items() if not (verified_match(item,checks[key]) and checks[key]['confidence']>base.CONFIDENCE_HIGH)]
            link_path=Path(link_review.get('path',''))
            if (not target or target_id not in targets or target_id in failed or not failed
                or any('row' not in targets[k] or targets[k]['row']>=ref[0] for k in failed)
                or link_review.get('failed_header_ids')!=failed or link_review.get('target_id')!=target_id
                or not link_path.is_file() or digest(link_path)!=link_review.get('sha256')):
                raise ValueError('行列关系核验范围或来源不匹配')
            link_call=json.loads(link_path.read_text());ld=link_call.get('input',{});pages=ld.get('page_numbers',[])
            originals=[Path(record['recognition_artifact'])/'input'/f'page-{i:03d}-original.png' for i in pages]
            images=[part.get('sha256') for msg in link_call.get('request',{}).get('messages',[])
                    if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
            link=link_review['verdict']
            if (not link_call.get('contract_valid') or link_call.get('phase')!='semantic-link-verify'
                or ld.get('task')!=task or ld.get('plan')!=plan or ld.get('target')!=target
                or ld.get('target_id')!=target_id or ld.get('failed_header_ids')!=failed
                or ld.get('table_id')!=(record.get('selection') or {}).get('table_id')
                or not pages or not all(x.is_file() for x in originals) or [digest(x) for x in originals]!=ld.get('page_hashes')
                or images!=ld.get('page_hashes') or json.loads(link_call['content'])!=link
                or not v['plan_valid'] or not v['structure_valid'] or v['structure_confidence']<=base.CONFIDENCE_HIGH
                or not link['relation_valid'] or link['confidence']<=base.CONFIDENCE_HIGH or link['target_id']!=target_id
                or not link['row_label'].strip() or not link['column_label'].strip() or not link['reason'].strip()):
                raise ValueError('行列关系独立原图核验未通过')
            try:
                if number(link['observed_text'])!=number(target['text']):raise ValueError('关系核验数值与目标不一致')
            except ArithmeticError as error:raise ValueError('关系核验数值无效') from error
            matched=True
        multi_review=record.get('quality_rounds',[{}])[-1].get('semantic_multi_link_review')
        if multi_review:
            refs=mean_numeric_cell_refs(task,plan);cells=[cell_at(table,ref) for ref in refs];ids=[cell_id(cell) for cell in cells]
            failed=[key for key,item in targets.items() if not (verified_match(item,checks[key]) and checks[key]['confidence']>base.CONFIDENCE_HIGH)]
            link_path=Path(multi_review.get('path',''))
            if (not refs or len(set(ids))!=len(ids) or not failed or any(key in failed or key not in targets for key in ids)
                or any('row' not in targets[k] or targets[k]['row']>=min(ref[0] for ref in refs) for k in failed)
                or multi_review.get('target_ids')!=ids or multi_review.get('failed_header_ids')!=failed
                or not link_path.is_file() or digest(link_path)!=multi_review.get('sha256')):
                raise ValueError('均值关系核验范围或来源不匹配')
            link_call=json.loads(link_path.read_text());ld=link_call.get('input',{});pages=ld.get('page_numbers',[])
            originals=[Path(record['recognition_artifact'])/'input'/f'page-{i:03d}-original.png' for i in pages]
            images=[part.get('sha256') for msg in link_call.get('request',{}).get('messages',[])
                    if isinstance(msg.get('content'),list) for part in msg['content'] if part.get('type')=='image_url']
            link=multi_review['verdict'];items=link['items'];by_id={x['target_id']:x for x in items}
            if (not link_call.get('contract_valid') or link_call.get('phase')!='semantic-multi-link-verify'
                or ld.get('task')!=task or ld.get('plan')!=plan or ld.get('targets')!={key:targets[key] for key in ids}
                or ld.get('target_ids')!=ids or ld.get('failed_header_ids')!=failed
                or ld.get('table_id')!=(record.get('selection') or {}).get('table_id')
                or not pages or not all(x.is_file() for x in originals) or [digest(x) for x in originals]!=ld.get('page_hashes')
                or images!=ld.get('page_hashes') or json.loads(link_call['content'])!=link
                or not v['plan_valid'] or not v['structure_valid'] or v['structure_confidence']<=base.CONFIDENCE_HIGH
                or not link['relation_valid'] or link['confidence']<=base.CONFIDENCE_HIGH or not link['reason'].strip()
                or len(items)!=len(ids) or len(by_id)!=len(ids) or set(by_id)!=set(ids)
                or any(not by_id[key]['row_label'].strip() or not by_id[key]['column_label'].strip() for key in ids)):
                raise ValueError('均值操作数独立原图核验未通过')
            try:
                if any(number(by_id[key]['observed_text'])!=number(targets[key]['text']) for key in ids):raise ValueError('均值核验数值与目标不一致')
            except ArithmeticError as error:raise ValueError('均值核验数值无效') from error
            matched=True
        accepted_round = record.get('quality_rounds',[{}])[-1]
        if accepted_round.get('policy') == 'question_average_confidence_v1':
            computed = average_confidence_route(checks, v.get('structure_confidence'), base.CONFIDENCE_HIGH)
            if computed == accepted_round.get('average_confidence') and computed['status'] == 'accepted':
                matched = True
        elif (v['plan_valid'] and v['structure_valid'] and v['structure_confidence']>base.CONFIDENCE_HIGH
                and all(verified_match(item,checks[k]) and checks[k]['confidence']>base.CONFIDENCE_HIGH for k,item in targets.items())):
            matched=True
    chunked=record.get('chunked_verification')
    if chunked and record.get('quality_rounds',[{}])[-1].get('policy')=='question_average_confidence_v1':
        chunks=chunked.get('chunks',[]);seen=[];verdicts=[];image_hashes=None
        for chunk in chunks:
            call_path=Path(chunk['path'])
            if not call_path.is_file() or digest(call_path)!=chunk.get('sha256'):
                raise ValueError('分批核验调用来源发生变化')
            call=json.loads(call_path.read_text());data=call.get('input',{});keys=chunk['targets']
            subset={key:targets[key] for key in keys}
            result=json.loads(call['content']);Pipeline.checks_by_id(result['checks'],subset)
            hashes=[p.get('sha256') for m in call.get('request',{}).get('messages',[])
                    if isinstance(m.get('content'),list) for p in m['content'] if p.get('type')=='image_url']
            if (not call.get('contract_valid') or data.get('task')!=task or data.get('plan')!=plan
                    or data.get('targets')!=subset or result!=chunk['verdict'] or not hashes
                    or image_hashes is not None and hashes!=image_hashes):
                raise ValueError('分批核验与题目、字段或原图不一致')
            image_hashes=hashes;seen.extend(keys);verdicts.append(result)
        if len(seen)!=len(set(seen)) or set(seen)!=set(targets) or not verdicts:
            raise ValueError('分批核验字段遗漏或重复')
        combined={'plan_valid':all(x['plan_valid'] for x in verdicts),
                  'plan_reason':'；'.join(x['plan_reason'] for x in verdicts),
                  'structure_valid':all(x['structure_valid'] for x in verdicts),
                  'structure_confidence':min(x['structure_confidence'] for x in verdicts),
                  'checks':[item for x in verdicts for item in x['checks']]}
        if combined!=chunked.get('combined'):
            raise ValueError('分批核验合并结果不一致')
        scores=average_confidence_route(Pipeline.checks_by_id(combined['checks'],targets),combined['structure_confidence'],base.CONFIDENCE_HIGH)
        if scores==record['quality_rounds'][-1].get('average_confidence') and scores['status']=='accepted':matched=True
    partial_record=record.get('partial_verification')
    if partial_record and record.get('quality_rounds',[{}])[-1].get('policy')=='question_average_confidence_v1':
        from pipeline_contracts import validate
        first_path=Path(partial_record['first']); second_path=Path(partial_record['second'])
        if (digest(first_path)!=partial_record['first_sha256'] or digest(second_path)!=partial_record['second_sha256']):
            raise ValueError('补充核验调用来源发生变化')
        first=json.loads(first_path.read_text()); second=json.loads(second_path.read_text())
        v1=json.loads(first['content']);v2=json.loads(second['content']);validate('verify',v1);validate('verify',v2)
        keys=[x['id'] for x in v1['checks']];missing={k:v for k,v in targets.items() if k not in keys}
        Pipeline.checks_by_id(v1['checks'],{k:targets[k] for k in keys});Pipeline.checks_by_id(v2['checks'],missing)
        image_list=lambda c:[p.get('sha256') for m in c.get('request',{}).get('messages',[])
                             if isinstance(m.get('content'),list) for p in m['content'] if p.get('type')=='image_url']
        combined={'plan_valid':v1['plan_valid'] and v2['plan_valid'],
                  'plan_reason':v1['plan_reason']+'；遗漏字段补核：'+v2['plan_reason'],
                  'structure_valid':v1['structure_valid'] and v2['structure_valid'],
                  'structure_confidence':min(v1['structure_confidence'],v2['structure_confidence']),
                  'checks':v1['checks']+v2['checks']}
        if (first.get('validation_error')!='复核结果必须完整且仅覆盖请求的字段，不能遗漏、重复或新增'
                or not second.get('contract_valid') or first.get('input',{}).get('task')!=task
                or first.get('input',{}).get('plan')!=plan or first.get('input',{}).get('targets')!=targets
                or second.get('input',{}).get('task')!=task or second.get('input',{}).get('plan')!=plan
                or second.get('input',{}).get('targets')!=missing or not missing
                or not image_list(first) or image_list(first)!=image_list(second)
                or partial_record.get('combined')!=combined):
            raise ValueError('遗漏字段补核与原题、字段或原图不一致')
        scores=average_confidence_route(Pipeline.checks_by_id(combined['checks'],targets),combined['structure_confidence'],base.CONFIDENCE_HIGH)
        if scores==record['quality_rounds'][-1].get('average_confidence') and scores['status']=='accepted':matched=True
    if not matched:
        raise ValueError('找不到匹配最终计划及全部依赖的原图核验调用')
    rules=evaluate_rules(table,plan['rules'],number)
    if (record.get('quality_rounds',[{}])[-1].get('policy') != 'question_average_confidence_v1'
            and any(r['state']=='failed' for r in rules)):
        raise ValueError('解析结果适用规则失败')
    missing = record.get('confirmed_missing_fields', [])
    if missing:
        verified = False
        for p in calls.glob('call-*-verify.json'):
            d=json.loads(p.read_text());data=d.get('input',{})
            if d.get('contract_valid') and data.get('task')==task and data.get('missing_fields')==missing and data.get('table')==verification_view(table):
                v=json.loads(d['content'])
                if not v['checks'] and v['plan_valid'] and v['structure_valid'] and v['structure_confidence']>base.CONFIDENCE_HIGH:
                    verified=True
        if not verified:raise ValueError('缺少匹配的原图字段未提供核验，禁止输出空字符串')
    footnotes = record.get('confirmed_footnotes', [])
    if footnotes:
        review = record.get('footnote_review') or {}; path = Path(review.get('call', ''))
        if (review.get('status') != 'passed' or review.get('candidates') != footnotes
                or raw_numeric_footnotes(numeric_expression_targets(targets, plan), record.get('parser_import_manifest', []), source_hash) != footnotes
                or not path.is_file() or digest(path) != review.get('sha256')):
            raise ValueError('脚注数值缺少匹配的原始解析与核验来源')
        call = json.loads(path.read_text()); payload = call.get('input', {}); verdict = review.get('verdict', {})
        pages = payload.get('page_numbers', []); folder = Path(record['recognition_artifact'])
        originals = [folder/'input'/f'page-{page:03d}-original.png' for page in pages]
        images = [part.get('sha256') for message in call.get('request', {}).get('messages', [])
                  if isinstance(message.get('content'), list) for part in message['content'] if part.get('type') == 'image_url']
        items = verdict.get('items', []); by_id = {item.get('target_id'):item for item in items}
        if (call.get('phase') != 'footnote-verify' or not call.get('contract_valid')
                or not str(call.get('model', '')).lower().startswith('qwen') or payload.get('task') != task
                or payload.get('plan') != plan or payload.get('candidates') != footnotes
                or not pages or not all(path.is_file() for path in originals)
                or payload.get('page_hashes') != [digest(path) for path in originals] or images != payload.get('page_hashes')
                or json.loads(call['content']) != verdict or not verdict.get('valid')
                or verdict.get('confidence', 0) <= base.CONFIDENCE_HIGH or not verdict.get('reason', '').strip()
                or len(items) != len(footnotes) or len(by_id) != len(footnotes)
                or any(item['target_id'] not in by_id or by_id[item['target_id']].get('numeric_text') != item['numeric_text']
                       or by_id[item['target_id']].get('marker') != item['marker'] for item in footnotes)):
            raise ValueError('数值脚注独立原图核验未通过')
    if evidence_only:
        return
    answer=execute(table,plan,question['question_type'],context,confirmed_missing_fields=missing,confirmed_footnotes=footnotes)
    if record.get('numeric_markup_output') and not (plan.get('kind')=='values' and question.get('answer_format')=='json_array'
            and record.get('quality_rounds') and record['quality_rounds'][-1].get('semantic_multi_link_review')):
        raise ValueError('数值装饰清理缺少对应的多值独立核验证据')
    formatted=base.format_answer(json.dumps({'answer':answer},ensure_ascii=False),question['question_type'],question.get('answer_format'),[table,context],numeric_wrappers=bool(record.get('numeric_markup_output')),currency_normalization=bool(record.get('currency_normalization')))
    if formatted!=record['answer']:
        raise ValueError('解析结果重算与保存答案不同')
