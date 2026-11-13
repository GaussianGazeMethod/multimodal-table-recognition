# -*- coding: utf-8 -*-
"""按HTML rowspan/colspan重建候选网格，仅诊断，不自动替换解析缓存。"""
import json
from html.parser import HTMLParser
from pathlib import Path

class Grid(HTMLParser):
    def __init__(self):
        super().__init__();self.row=-1;self.col=0;self.occupied=set();self.cells=[];self.active=None
    def handle_starttag(self,tag,attrs):
        if tag=='br' and self.active is not None:self.active['text']+='\n'
        if tag=='tr':self.row+=1;self.col=0
        if tag in ('td','th'):
            if self.active is not None:raise ValueError('嵌套单元格')
            while (self.row,self.col) in self.occupied:self.col+=1
            a=dict(attrs);rs=int(a.get('rowspan','1'));cs=int(a.get('colspan','1'))
            if rs<1 or cs<1:raise ValueError('非法跨度')
            self.active={'row':self.row,'col':self.col,'rowspan':rs,'colspan':cs,'text':''}
    def handle_data(self,text):
        if self.active is not None:self.active['text']+=text
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.active is not None:
            c=self.active;c['text']=c['text'].strip()
            for r in range(c['row'],c['row']+c['rowspan']):
                for col in range(c['col'],c['col']+c['colspan']):
                    if (r,col) in self.occupied:raise ValueError('HTML合并范围重叠')
                    self.occupied.add((r,col))
            self.cells.append(c);self.col+=c['colspan'];self.active=None

