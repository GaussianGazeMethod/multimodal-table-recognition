#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只准备可识别的页面输入，不定位表格、不解题。"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import fitz
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3]/'.env')
import table_solver as base
from table_pipeline import Pipeline, preparation_dependencies
from input_quality import prepare


def main():
    parser=argparse.ArgumentParser(description='质量检查、按需渲染/旋转/放大，保留原始文件及处理记录')
    parser.add_argument('input',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not base.API_KEY or not base.VISION_MODEL.lower().startswith('qwen'):
        raise ValueError('请配置DASHSCOPE_API_KEY并使用千问模型')
    folder=args.output or base.OUTPUT_DIR/('input-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    with OpenAI(api_key=base.API_KEY,base_url=base.BASE_URL,timeout=base.TIMEOUT_SECONDS,max_retries=0) as client:
        pipeline=Pipeline(client,folder/'calls',base)
        result=prepare(args.input,folder,base,pipeline.ask)
    if any(page['actions'] for page in result['quality']['pages']):
        if args.input.suffix.lower()=='.pdf':
            target=folder/'parser-input.pdf'
            with fitz.open(args.input) as source, fitz.open() as prepared:
                for index, image in enumerate(result['page_paths']):
                    rect=source[index].rect
                    angle=result['quality']['pages'][index]['assessment']['rotation_clockwise']
                    width,height=(rect.height,rect.width) if angle in (90,270) else (rect.width,rect.height)
                    page=prepared.new_page(width=width,height=height)
                    page.insert_image(page.rect,filename=str(image))
                prepared.save(target)
        else:
            target=Path(result['page_paths'][0])
    else:
        target=args.input.resolve()
    files=[folder/'quality.json',*sorted(folder.glob('page-*-original.png')),*result['page_paths']]
    sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    metadata={'source_sha256':sha(args.input),'parser_input':str(target.resolve()),'parser_input_sha256':sha(target),
              'quality_dir':str(folder.resolve()),'dependencies':preparation_dependencies(base),
              'artifacts':{p.name:sha(p) for p in files}}
    (folder/'prepared-manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"已准备 {len(result['page_paths'])} 页；处理记录：{folder/'quality.json'}")


if __name__=='__main__':main()
