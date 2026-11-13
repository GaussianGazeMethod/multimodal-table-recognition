#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立表格定位入口，不依赖题目清单或解题结果。"""
import argparse
from datetime import datetime
from pathlib import Path
from openai import OpenAI
import table_solver as base
from table_pipeline import Pipeline

# 独立工具的配置入口，密钥沿用安全环境变量读取。
API_KEY = base.API_KEY
BASE_URL = base.BASE_URL
MODEL = base.VISION_MODEL


def main():
    parser = argparse.ArgumentParser(description='定位 PDF 或图片中的所有表格，导出裁剪图和目录')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not API_KEY or not MODEL.lower().startswith('qwen'):
        raise ValueError('请配置 DASHSCOPE_API_KEY 并使用千问模型')
    base.VISION_MODEL = MODEL
    output = args.output or base.OUTPUT_DIR / ('locate-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    with OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=base.TIMEOUT_SECONDS, max_retries=0) as client:
        result = Pipeline(client, output, base).locate(args.input)
    print(f"识别到 {len(result['tables'])} 个逻辑表格，目录和裁剪图：{result['folder']}")


if __name__ == '__main__':
    main()
