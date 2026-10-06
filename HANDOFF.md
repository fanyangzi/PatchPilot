# PatchPilot 交接文档

## 当前状态

**Phase 4.1 样张阶段已完成** — 两个样张页面已实现并验证通过，等待用户认可后进入 Phase 4.2 铺开阶段。

### 已完成的工作

1. **仓库体检页** (`apps/console/src/pages/Inspect.tsx`)
   - 结论印章（可信交付 / 待复核 / 不安全交付）
   - 问题元信息（repo / commit / issue / 执行时间）
   - 六项检查逐项展开（复现 / 目标测试 / 回归测试 / diff 范围 / 敏感路径 / 许可证）
   - 证据链与 verdict_hash 显示
   - 响应式布局（桌面 / 移动）

2. **策略页** (`apps/console/src/pages/Policy.tsx`)
   - 左侧：AI_POLICY.md 散文规矩（5 条款）
   - 右侧：编译后的 YAML（allowed_paths / sensitive_patterns / floor）
   - 行号显示 + 语法高亮

3. **样式系统** (`apps/console/src/styles/gate.css`)
   - 346 行完整样式
   - 遵循 tokens.css 既有方向（冷纸底 #EDF1F6 / 蓝黑墨 #0F1A2B / 1px 线）
   - 颜色只承载结论（墨绿可信 / 朱砂封存 / 琥珀待复核）

4. **样本数据** (`apps/console/src/samples.ts`)
   - INSPECT：包含 6 项检查的完整模拟数据
   - POLICY：5 条款 + 3 条底线 + 编译后的 YAML

### 当前可验证的内容

- 开发服务器运行中：`http://localhost:5173`
- 仓库体检页：`http://localhost:5173/#inspect`
- 策略页：`http://localhost:5173/#policy`

### 分支与提交状态

- 当前分支：`frontend-redesign`
- 主分支：`main`
- Git 用户：fanyangzi
- 未提交的文件：
  - 新增：`apps/console/src/pages/Inspect.tsx`
  - 新增：`apps/console/src/pages/Policy.tsx`
  - 新增：`apps/console/src/styles/gate.css`
  - 修改：`apps/console/src/main.tsx`（新增两个页面的路由）

## 下一步工作（Phase 4.2 铺开阶段）

### 1. API 扩展

**文件**: `src/patchpilot/api.py`

需要新增两个路由：

```python
@app.route('/api/runs', methods=['POST'])
def create_run():
    """接受 repo/commit/issue，启动门禁运行"""
    data = request.json
    task = TaskSpec(
        task_id=f'adhoc_{uuid.uuid4().hex[:8]}',
        repo=data['repo'],
        commit=data['commit'],
        issue_title=data['issue_title'],
        issue_body=data.get('issue_body', ''),
        # ...
    )
    result = pp.run_task(task)
    return jsonify(result.to_dict())

@app.route('/api/policy/<run_id>')
def get_policy(run_id):
    """获取指定 run 使用的策略"""
    # 从 evidence.json 读取 policy 事件
    ...
```

### 2. 前端改造

**文件**: `apps/console/src/pages/Inspect.tsx` 和 `Policy.tsx`

- 移除样本数据 (`samples.ts`)
- 改用真实 API 调用
- Inspect 页面：
  - 添加表单（repo URL / commit SHA / issue 文本）
  - "执行门禁" 按钮调用 `POST /api/runs`
  - 轮询或 WebSocket 获取运行状态
  - 从 `GET /api/runs/<run_id>` 获取结果并渲染

- Policy 页面：
  - 从 `GET /api/policy/<run_id>` 获取策略
  - 或读取当前仓库的 `patchpilot.policy.yaml` / `AI_POLICY.md`

### 3. 其余标签完成

**文件**: `apps/console/src/main.tsx` 中的 TABS

当前四个标签：
1. 仓库体检（已完成样张）
2. 总览（需要重做）
3. 运行记录（需要接入真实数据）
4. 对照评测（需要两层赛道数据）

需要：
- 将「总览」改为「策略」（已完成样张）
- 「运行记录」展示真实的两层赛道（受控夹具 + 真实仓库）
- 「对照评测」按两层分别呈现，修正过度乐观的文案

### 4. 一键启动脚本

**文件**: `start.sh`（已创建但需要完善）

当前内容：
```bash
#!/bin/bash
cd "$(dirname "$0")"
PYTHONPATH=src .venv/bin/python -m patchpilot.api &
API_PID=$!
cd apps/console
npm run dev &
CONSOLE_PID=$!
sleep 3
open http://localhost:5173
wait
```

需要：
- 添加错误处理
- 检查依赖是否安装
- 优雅的停止机制

## 关键技术约束

### 后端（Python）

- 不使用、不回显之前粘贴的 GitHub token（已被告知吊销）
- 不推送本地 `main`（含无关历史）
- API 保持未认证且仅绑定 `127.0.0.1`
- 仅在用户要求时提交

### 前端（React + TypeScript）

- 使用 Vite 作为构建工具
- TypeScript 严格模式
- 使用 Lucide React 图标库
- CSS 变量系统（`tokens.css`）
- 不使用外部 UI 框架（Material-UI / Ant Design 等）

### 审美约束（必须遵守）

按 `apps/console/src/styles/tokens.css` 中的注释：

> PatchPilot console — design tokens.
> 
> The subject is a policy-constrained maintenance runtime: every run ends in a
> verdict that a maintainer has to be able to check line by line. The console
> is therefore built as a forensic record, not a dashboard — cool paper, blue
> black ink, hairline rules, and a numbered index down the page.
> 
> Two rules the whole design obeys:
>   1. Colour carries verdict only. Jade / vermilion / amber never decorate.
>   2. Structure is drawn with 1px rules, never with a stack of shadowed cards.

**不得**：
- 使用圆角卡片堆砌
- 用颜色做装饰（颜色只承载结论）
- 使用阴影
- 偏离「取证卷宗」的视觉方向

**必须**：
- 1px 发丝线构建结构
- 冷纸底色（#EDF1F6）
- 蓝黑墨色（#0F1A2B）
- 结论用墨绿（可信）/ 朱砂（封存）/ 琥珀（待复核）

## 已知问题与风险

### 1. 后端尚未完成的部分

根据 `.claude/plans/tingly-twirling-babbage.md`：

- **阶段一**（证据可复核性）：部分完成
  - Event 已有 `prev_hash` 和 `event_hash`
  - Run 已有 `verdict_hash`
  - `Evidence.parents` 尚未真实填充（当前恒为 `[]`）
  - `patchpilot verify` 命令已实现

- **阶段二**（真实仓库赛道）：部分完成
  - `fixtures/tasks/real/` 目录已创建
  - worktree 泄漏已修复
  - 泛化工作区获取已完成

- **阶段三**（远端模型接入）：未开始
  - `RemoteModel.propose_patch()` 尚未实现
  - 环境变量 `PATCHPILOT_USE_REMOTE_PATCH` 尚未接入

### 2. 前端尚未完成的部分

- 真实 API 调用（当前只有样本数据）
- 轮询或 WebSocket 机制（门禁运行需要时间）
- 错误处理（API 失败 / 网络错误）
- 加载状态（运行中的 UI）

### 3. 评测相关

- `evals/run_eval.py` 需要修改为两层报数
- `summary.json` 需要分为 `summary_fixture.json` 和 `summary_real.json`
- 真实模型成本计量尚未实现

## 文件结构清单

### 前端（apps/console）

```
apps/console/
├── src/
│   ├── pages/
│   │   ├── Inspect.tsx          # ✅ 新增（仓库体检）
│   │   ├── Policy.tsx           # ✅ 新增（策略）
│   │   ├── Overview.tsx         # ⚠️  需要重做
│   │   ├── Runs.tsx             # ⚠️  需要接入真实数据
│   │   ├── Eval.tsx             # ⚠️  需要两层赛道数据
│   │   ├── RunDetail.tsx        # ⚠️  需要接入真实数据
│   │   ├── RunDialog.tsx        # ⚠️  需要接入真实数据
│   │   └── Routing.tsx          # ⚠️  可能需要重做或移除
│   ├── styles/
│   │   ├── gate.css             # ✅ 新增（Inspect 和 Policy 样式）
│   │   ├── tokens.css           # ✅ 已有（设计 token）
│   │   ├── base.css             # ✅ 已有
│   │   └── pages.css            # ✅ 已有
│   ├── main.tsx                 # ⚠️  部分修改（路由）
│   ├── samples.ts               # ⚠️  临时文件，真实 API 后移除
│   ├── api.ts                   # ⚠️  需要扩展
│   └── ui.tsx                   # ✅ 已有
└── index.html                   # ✅ 已有
```

### 后端（src/patchpilot）

```
src/patchpilot/
├── api.py                       # ⚠️  需要新增路由
├── cli.py                       # ✅ verify 命令已实现
├── domain/
│   └── models.py                # ✅ Event/Run 哈希字段已添加
├── evidence/
│   └── store.py                 # ✅ 哈希链已实现
├── orchestrator/
│   └── engine.py                # ⚠️  Evidence.parents 尚未填充
├── policy/                      # ⚠️  新目录，需要实现
│   ├── schema.py                # ⚠️  待实现
│   ├── compile.py               # ⚠️  待实现
│   └── gate.py                  # ⚠️  待实现
├── verifier/
│   ├── checks.py                # ✅ 6 项检查已真实化
│   └── diff.py                  # ✅ 新增（diff 解析）
├── adapters/
│   └── remote.py                # ⚠️  propose_patch() 待实现
└── harness/
    └── runner.py                # ✅ 已修改
```

### 评测与夹具

```
evals/
└── run_eval.py                  # ⚠️  需要两层报数

fixtures/
├── tasks/
│   ├── issue-001-normal.yaml    # ✅ 已有
│   ├── ...                      # ✅ 已有（共 9 个）
│   └── real/                    # ⚠️  需要添加真实仓库任务
└── patches/                     # ✅ 已创建
```

### 文档与交付物

```
├── action.yml                   # ⚠️  GitHub Action 待实现
├── start.sh                     # ⚠️  一键启动脚本待完善
└── .claude/
    └── plans/
        └── tingly-twirling-babbage.md  # 完整实施方案
```

## 重要参考文档

1. **实施方案**: `.claude/plans/tingly-twirling-babbage.md`
   - 五个阶段的完整计划
   - 验收条件
   - 关键文件清单

2. **设计 token**: `apps/console/src/styles/tokens.css`
   - 颜色系统
   - 字体系统
   - 几何系统

3. **样本数据**: `apps/console/src/samples.ts`
   - INSPECT 数据结构
   - POLICY 数据结构

## 验证方式

### 前端验证

```bash
cd apps/console
npm run dev
# 访问 http://localhost:5173/#inspect
# 访问 http://localhost:5173/#policy
```

### 后端验证

```bash
cd /Users/leviviya/Documents/ChatGPT/ai赛道
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m patchpilot.cli run --task fixtures/tasks/issue-001-normal.yaml
PYTHONPATH=src .venv/bin/python -m patchpilot.cli verify --run-id <run_id>
```

### 评测验证

```bash
PYTHONPATH=src .venv/bin/python evals/run_eval.py
# 检查生成的 summary.json 和 results.jsonl
```

## 联系方式

- Git 用户：fanyangzi
- 分支：frontend-redesign
- 远端模型配置（如需接入）：
  - Base URL: https://hk.getelucid.com/v1
  - Model: gpt-6.1-sol
  - API Key: 通过环境变量 `OPENAI_API_KEY` 传入

## 最后提醒

1. **不要提交敏感信息**（API key / token）
2. **不要推送本地 main 分支**（含无关历史）
3. **保持审美一致性**（遵循 tokens.css 方向）
4. **诚实命名策略**（不伪造模型调用）
5. **样张先行**（用户认可后再铺开）

---

生成时间：2026-10-06
最后更新：Phase 4.1 样张阶段完成
下一里程碑：Phase 4.2 铺开阶段（接入真实 API）
