# EvoForge

**让程序持续改进，同时保存每一步证据。**

EvoForge 是一个 coding agent 自进化实验框架。Agent 根据训练任务中的失败轨迹，修改自己的源代码；外部控制器负责评测、分支选择、资源限制和归档。模型权重保持固定，可演化的部分包括工具逻辑、上下文记忆、执行流程和可复用技能。

[English](README.md) · [架构](docs/architecture.md) · [实验设计](docs/experiments.md) · [扩展接口](docs/extending.md)

## 已实现

| 能力 | 实现方式 |
| --- | --- |
| 自身代码修改 | 每个候选版本都有 `step` 和 `propose`，后者负责构造下一次修改请求 |
| 多分支演化 | 完整保留候选版本，按成功率、token 成本、行为差异和探索比例选择父版本 |
| 组件重组 | 周期性从另一分支移入工具、记忆、流程或技能组件，再评测组合后的完整程序 |
| 独立评测 | 训练轨迹用于修改，验证集用于选择，最终测试在候选冻结后执行 |
| 回归控制 | 比较父子版本在同一任务上的成功率，报告回归任务和配对 bootstrap 区间 |
| 消融实验 | 逐项恢复初始工具、记忆、流程、技能，测量验证集表现变化 |
| 预算与隔离 | 每题固定资源上限；真实模型实验通过无网络、只读根目录、非 root 的 Docker 容器执行，不挂载宿主机目录 |
| 可追溯结果 | SQLite 事件记录、代码快照、diff、用量账本、文件指纹和可点击 HTML 演化树 |

## 直接运行

需要 macOS 或 Linux，以及 Python 3.9+。离线演示没有第三方运行依赖，也不需要 API Key。

```bash
cd /path/to/evoforge
PYTHONPATH=src python3 -m evoforge demo --output runs/demo --rounds 9 --ablations --seal
```

完成后打开 `runs/demo/report.html`。已有同名运行目录时，请换一个新目录。`examples/offline-report.html` 是随项目提供的演示报告。

**离线模式中的回复和修改方案是预设的。** 它运行真实的控制器、评测、筛选和存档流程，用来验证框架机制。演示分数不能作为自主学习能力的证据。

## 接入真实模型

```bash
docker build -t evoforge-runtime:local -f runtime/Dockerfile runtime
cp configs/live.json configs/experiment.json
```

在配置中填写固定模型版本 `model`、兼容 Chat Completions 的 `base_url`，并按服务需要设置环境变量 `EVOFORGE_API_KEY`。端点需要支持 JSON 输出、`temperature` 和 `max_tokens`。默认指向本地服务。

```bash
PYTHONPATH=src python3 -m evoforge run --config configs/experiment.json --output runs/experiment
PYTHONPATH=src python3 -m evoforge ablate runs/experiment
PYTHONPATH=src python3 -m evoforge seal runs/experiment
```

`ablate` 运行组件消融；`seal` 固定选中的版本并执行最终测试，随后不允许继续演化。运行总预算包含演化、消融和最终测试，需要为后两者留出余量。模型用量优先采用服务端返回值，无法确认时使用保守估计。

## 查看证据

```bash
PYTHONPATH=src python3 -m evoforge inspect runs/demo c0004
PYTHONPATH=src python3 -m evoforge audit runs/demo
PYTHONPATH=src python3 -m evoforge resume runs/interrupted-run
PYTHONPATH=src python3 -m evoforge report runs/demo
make test
```

当前任务适配器支持小型 Python 源码目录中的单文件函数修复。内置 24 个任务分布在区间合并、列表分块、字符串规范化、滑动平均四个任务族；训练、验证、测试使用不同的用例和布局变体，但仍共享任务族。它们适合开发框架；要研究跨任务泛化，应导入额外的独立任务集。

技能目前是带标签的程序性经验。自动课程、可执行技能依赖图、任意仓库命令执行和模型权重训练尚未实现。详细边界与扩展方法见英文技术文档。

研究背景包括 [DGM](https://arxiv.org/abs/2505.22954)、[GEPA](https://arxiv.org/abs/2507.19457) 和 [Voyager](https://arxiv.org/abs/2305.16291)。本项目采用独立实现，代码并非这些仓库的复刻，也不宣称复现其论文结果。
