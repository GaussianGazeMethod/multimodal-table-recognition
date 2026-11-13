# -*- coding: utf-8 -*-
"""语言属性核验：比较独立语言结论并保留可定位的关键词，绝不代替其他字段核验。"""
def clean(text):
    return ''.join(text.split()).casefold()

def language_name(text):
    """仅归一明确同义的语言名称，不改变表格原文或最终答案。"""
    name=clean(text)
    aliases={'英语':'英语','英文':'英语','english':'英语',
             '中文':'中文','汉语':'中文','chinese':'中文'}
    return aliases.get(name,name)

def accept_language(verdict, plan, targets, threshold):
    expected=plan['expressions'][0]['languages']
    if not expected or any(not language_name(x) for x in expected) or len(set(map(language_name,expected))) != len(expected):
        return False,'计划语言名称为空或重复'
    if not verdict['scope_valid'] or verdict['confidence'] <= threshold:
        return False,'语言或表格范围未通过既有高阈值'
    if set(map(language_name,verdict['languages'])) != set(map(language_name,expected)):
        return False,'独立原图语言判断与计划冲突'
    if not verdict['reason'].strip() or not verdict['evidence']:
        return False,'缺少语言判断依据'
    for item in verdict['evidence']:
        target=targets.get(item['id']);text=clean(item['text'])
        if (not target or not item['position_consistent'] or not text
                or not any(c.isalpha() for c in text) or text not in clean(target['text'])):
            return False,'语言关键词缺少对应原文或位置依据'
    return True,'独立原图语言、目标表范围与关键词来源通过核验'
