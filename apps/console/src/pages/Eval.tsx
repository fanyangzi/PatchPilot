import { ChartColumn, CircleCheckBig, Info, ScrollText, ShieldAlert, Table2 } from 'lucide-react';
import { api, type EvalSummary } from '../api';
import { METHOD, SCENARIO, VERDICT, verdictOf } from '../model';
import { Empty, Loading, Meter, Offline, Section, useAsync } from '../ui';

const METHODS = ['direct_llm', 'linear_agent', 'patchpilot_full'] as const;

const METRICS: { key: keyof EvalSummary['methods'][string]; name: string; hint: string; kind: 'rate' | 'num' }[] = [
  { key: 'trusted_delivery_rate', name: '可信交付率', hint: '修好了，且没有越过任何策略边界', kind: 'rate' },
  { key: 'policy_gate_pass_rate', name: '策略合规率', hint: '交付或拦截的决定符合策略', kind: 'rate' },
  { key: 'mean_evidence_completeness', name: '证据完整度', hint: '结论能追溯到的证据比例', kind: 'rate' },
  { key: 'functional_repair_rate', name: '功能修复率', hint: '测试通过即算，不管是否越界', kind: 'rate' },
  { key: 'first_pass_rate', name: '首次通过率', hint: '第一次验证就通过', kind: 'rate' },
  { key: 'mean_attempts', name: '平均尝试次数', hint: '失败后回滚重试的次数', kind: 'num' },
  { key: 'mean_tool_calls', name: '平均工具调用', hint: '更多调用换来复现、扫描和证据', kind: 'num' },
];

const LIMITATIONS = [
  '评测使用 9 个自建的 Python 夹具任务，每个都固定了 commit。',
  '“直接生成”和“线性 Agent”是本地确定性的对照策略，不代表任何特定的托管模型。',
  '所有百分比都是本次运行的任务级结果，不能据此推断在外部仓库上的表现。',
  'PatchPilot 在评测中使用确定性的补丁源，因此模型成本记为零。',
];

function MethodHead({ method }: { method: string }) {
  const Icon = METHOD[method].icon;
  return <span><Icon size={13} /> {METHOD[method].name}</span>;
}

export function EvalPage() {
  const data = useAsync(() => api.evalSummary(), []);
  if (data.loading) return <Loading label="正在读取评测结果" />;
  if (data.error) return <Offline message={data.error === 'evaluation has not been run' ? '还没有评测结果，先运行 evals/run_eval.py' : data.error} onRetry={data.reload} />;
  const s = data.data!;
  if (!s.task_count || !s.rows.length) return <div className="eval"><Empty>评测结果为空，先运行 evals/run_eval.py 生成真实对照数据。</Empty></div>;
  const row = (task: string, method: string) => s.rows.find((r) => r.task_id === task && r.method === method);

  return <div className="eval">
    <header className="eval-head">
      <h1>对照评测</h1>
      <p>同一批 {s.task_count} 个任务，分别交给三种执行方式，共 {s.row_count} 次运行。任务覆盖正常修复、需要恢复的失败和应被拦截的风险改动。</p>
    </header>

    <Section icon={Table2} title="逐任务结果" note={`${s.task_count} 个任务 × ${s.method_count} 种方式`}
      sub="红底的“不安全交付”表示补丁越过了策略边界却仍被交付，这正是维护者最不想看到的情况。">
      <div className="panel-scroll">
        <div className="matrix" role="table" aria-label="逐任务结果">
          <div className="mx-row mx-head" role="row">
            <span role="columnheader">任务</span>
            {METHODS.map((m) => <span role="columnheader" key={m} className={m === 'patchpilot_full' ? 'us' : ''}><MethodHead method={m} /></span>)}
          </div>
          {s.task_ids.map((t) => {
            const scenario = row(t, 'patchpilot_full')?.scenario;
            const ScenarioIcon = scenario ? SCENARIO[scenario].icon : ScrollText;
            return <div className="mx-row" role="row" key={t}>
              <span className="mx-task" role="rowheader">
                <ScenarioIcon size={14} />
                <span><code>{t.replace(/^issue-/, '')}</code>{scenario && <small>{SCENARIO[scenario].name}</small>}</span>
              </span>
              {METHODS.map((m) => {
                const r = row(t, m);
                const v = verdictOf(r?.status);
                return <span role="cell" key={m} className={m === 'patchpilot_full' ? 'us' : ''}>
                  <span className={`cell cell-${v}`}>{VERDICT[v].label}{r && r.attempts > 1 ? <em>重试后</em> : null}</span>
                </span>;
              })}
            </div>;
          })}
        </div>
      </div>
    </Section>

    <Section icon={ChartColumn} title="汇总指标" note={`共 ${s.row_count} 次运行`}
      sub="PatchPilot 的首次通过率和功能修复率低于线性 Agent，是因为它把两个风险任务判为“待复核”而不是交付。这是有意的取舍。">
      <div className="panel-scroll">
        <div className="metrics" role="table" aria-label="汇总指标">
          <div className="mt-row mt-head" role="row">
            <span role="columnheader">指标</span>
            {METHODS.map((m) => <span role="columnheader" key={m} className={m === 'patchpilot_full' ? 'us' : ''}>
              <MethodHead method={m} /><small>{METHOD[m].desc}</small>
            </span>)}
          </div>
          {METRICS.map((k) => <div className="mt-row" role="row" key={k.key}>
            <span className="mt-name" role="rowheader">
              <Info size={14} />
              <span><strong>{k.name}</strong>{k.hint && <small>{k.hint}</small>}</span>
            </span>
            {METHODS.map((m) => {
              const value = Number(s.methods[m]?.[k.key] ?? 0);
              return <span role="cell" key={m} className={m === 'patchpilot_full' ? 'us' : ''}>
                <span className="mt-value">
                  <b>{k.kind === 'rate' ? `${Math.round(value * 100)}%` : value.toFixed(1)}</b>
                  {k.kind === 'rate' && <Meter value={value} tone={m === 'patchpilot_full' ? 'ink' : 'mute'} />}
                </span>
              </span>;
            })}
          </div>)}
        </div>
      </div>
    </Section>

    <Section icon={ShieldAlert} title="这组数据不能说明什么" note="口径说明"
      sub="评测的口径和边界也一并写在控制台里，避免结论被读得比数据本身更大。">
      <ul className="limits">
        {LIMITATIONS.map((l) => <li key={l}><CircleCheckBig size={14} />{l}</li>)}
      </ul>
    </Section>
  </div>;
}
