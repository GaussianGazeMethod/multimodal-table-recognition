# -*- coding: utf-8 -*-
from pathlib import Path
import os
import sys
from dotenv import load_dotenv

here = Path(__file__).resolve().parent
load_dotenv(here.parents[1] / '.env')
sys.path.insert(0, str(here / 'code'))
import table_solver

try:
    sys.exit(table_solver.main())
except (ValueError, OSError) as error:
    print(f'错误：{error}', file=sys.stderr)
    sys.exit(1)
