# 多模态表格识别系统

从 PDF 或图片中定位表格、识别结构与内容，并根据用户意图提取数据或进行计算，最终生成 Excel 结果。系统通过阿里云兼容接口调用千问模型，也提供可选的文档智能解析工具。

## 能做什么

- **结构恢复**：识别行列、单元格文本和合并关系。
- **内容提取**：根据行、列、表头或上下文定位所需数据。
- **内容推理**：在识别结果上进行筛选、加总等计算。
- **质量检查**：记录识别、复核和计算过程，对无法确认的结果保留空值。

处理流程：输入文件与用户需求 → 页面和表格定位 → 结构识别 → 意图理解与计算 → 质量检查 → Excel 结果。

## 文件结构

```text
├── README.md                    项目入口与使用方法
├── docs/                        设计思路与优化策略
├── src/table_app/run.py         主程序入口
├── src/table_app/parse_file.py  可选文档智能解析工具
├── src/table_app/code/          识别、解题、质检与评测模块
├── data_corrections.json        默认输入的修正配置（初始为空）
├── .env.example                 环境变量模板
├── requirements.txt             主程序依赖
├── requirements-lock.txt        主程序依赖版本记录
└── requirements-docmind.txt     文档智能解析工具依赖
```

原始文档、用户需求、评测数据及历史运行结果未包含在本文件夹中。

## 快速开始

以下命令在 macOS 或 Linux 的终端中、项目根目录下执行。

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置模型服务

```bash
cp .env.example .env
```

在 `.env` 中填写 `DASHSCOPE_API_KEY`。模型名称和服务地址位于 `src/table_app/code/table_solver.py`，请根据自己的服务配置确认可用性。模型调用可能产生费用；不要将 `.env` 上传到 GitHub。

### 3. 准备输入

默认读取 `data/questions.xlsx` 和 `data/files/` 中的文档，也可以通过命令行指定其他路径。Excel 使用第一个工作表，首行至少包含以下字段：

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `id` | 唯一任务编号 | `1` |
| `file_name` | 文档目录中的文件名 | `sample.pdf` |
| `question_type` | `structure`、`extract` 或 `thinking` | `extract` |
| `question` | 用自然语言描述的用户需求 | `提取表格中的销售总额` |
| `table_hint` | 可选，帮助定位目标表格 | `销售汇总表` |
| `answer_format` | 可选，指定答案格式 | `number` |

请自行准备有权使用的文件。用户需求应明确目标表格、字段、筛选或计算条件，以及期望的结果格式。程序通过 `question` 字段接收这一需求。

### 4. 检查并运行

```bash
# 检查输入与渲染，不调用模型
python src/table_app/run.py --ids 1 --check-only

# 运行指定任务
python src/table_app/run.py --ids 1

# 查看所有参数，包括自定义输入和输出路径
python src/table_app/run.py --help
```

使用其他输入路径时，可附加 `--tests /path/questions.xlsx --files-dir /path/files`。运行结果默认保存在 `outputs/runs/` 的时间戳目录；其中 `submission.xlsx` 包含 `id` 与 `answer` 两列，其他文件记录过程和状态。缓存默认位于 `outputs/runs/recognition_cache/`。

## 其他说明

- `data_corrections.json` 仅在使用默认需求清单路径时生效，初始配置为空；如需修正输入，请先核实原始数据。
- 可选文档智能解析依赖 `requirements-docmind.txt` 和 `.env.example` 中列出的阿里云凭据。该工具与主流程的接入需要额外准备解析结果清单。
- 输出与缓存可能包含原文、用户需求和模型结果，应按输入数据的敏感程度管理。`data/`、`outputs/` 和 `.env` 已列入 `.gitignore`。
- 此整理版已通过语法和命令行帮助检查，尚未进行真实接口的端到端验证。识别准确率依赖输入质量、模型与具体场景。

更多设计思路见 [问题诊断与优化策略](docs/问题诊断与优化策略.md)。
