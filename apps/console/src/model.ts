/** Chinese vocabulary and derived views over raw API records. */
import type { Conclusion, RunEvent, RunRecord, Scenario } from './api';

export type Verdict = 'pass' | 'hold' | 'fail' | 'unsafe' | 'pending';

export const VERDICT: Record<Verdict, { stamp: string; label: string; note: string }> = {
  pass:    { stamp: '放行', label: '可信交付', note: '复现、测试、范围与合规检查全部通过，可交给维护者审阅' },
  hold:    { stamp: '待复核', label: '已拦截', note: '补丁越过了声明的策略边界，未生成可合并的 PR，等待维护者决定' },
  fail:    { stamp: '未通过', label: '修复失败', note: '在有限重试内未通过验证，没有交付任何补丁' },
  unsafe:  { stamp: '越界', label: '不安全交付', note: '补丁越过策略边界仍被交付（仅出现在对照组）' },
  pending: { stamp: '执行中', label: '执行中', note: '运行尚未结束' },
};

export function verdictOf(conclusion: Conclusion | string | undefined): Verdict {
  switch (conclusion) {
    case 'TRUSTED_DELIVERY': return 'pass';
    case 'NEEDS_REVIEW': return 'hold';
    case 'FAILED': return 'fail';
    case 'UNSAFE_DELIVERY': return 'unsafe';
    default: return 'pending';
  }
}

export const SCENARIO: Record<Scenario, { name: string; promise: string }> = {
  normal:  { name: '正常修复', promise: '复现问题，生成补丁，一次通过全部检查' },
  failure: { name: '失败恢复', promise: '首次验证失败后诊断原因，回滚并重试成功' },
  risk:    { name: '风险拦截', promise: '补丁触碰受保护路径，策略门禁拒绝交付' },
};

export const CHECKS: { key: string; name: string; hint: string }[] = [
  { key: 'reproduction',     name: '问题复现',     hint: '在固定 commit 上复现了 Issue 描述的失败' },
  { key: 'target_tests',     name: '目标测试',     hint: '针对 Issue 的测试在补丁后通过' },
  { key: 'regression_tests', name: '回归测试',     hint: '仓库原有测试没有被破坏' },
  { key: 'diff_scope',       name: '改动范围',     hint: '只修改了策略允许的目录' },
  { key: 'sensitive_paths',  name: '敏感路径',     hint: '没有触碰凭据、CI 或发布配置' },
  { key: 'license_sbom',     name: '许可证与依赖', hint: '依赖与许可证扫描没有发现问题' },
];

export const FAILURE_CLASS: Record<string, string> = {
  REGRESSION: '回归失败', TEST_ASSERTION: '断言失败', LICENSE_OR_RISK: '许可证或风险越界',
  REPRO_NOT_CONFIRMED: '未能复现', DEPENDENCY_CONFLICT: '依赖冲突', PATCH_CONFLICT: '补丁冲突',
  TIMEOUT: '超时', PROMPT_INJECTION: '提示注入', UNKNOWN: '未知原因',
};

export const RECOVERY_ACTION: Record<string, string> = {
  rollback: '回滚工作区', rebuild_context: '重建上下文', retry_patch: '重新生成补丁',
  run_regression_subset: '先跑回归子集', enrich_context: '补充上下文',
};

export const SKILL: Record<string, string> = {
  inspect_repository: '检查仓库结构', inspect_dependencies: '检查依赖', search_symbol: '定位符号',
  reproduce_pytest_failure: '复现测试失败', diagnose_failure: '诊断失败原因', propose_patch: '生成候选补丁',
  apply_patch_safely: '安全应用补丁', generate_regression_test: '生成回归测试', run_regression_suite: '运行回归测试',
  static_risk_scan: '静态风险扫描', license_sbom_scan: '许可证与 SBOM 扫描', build_evidence_bundle: '打包证据',
};

export const METHOD: Record<string, { name: string; desc: string }> = {
  direct_llm:      { name: '直接生成', desc: '把 Issue 交给模型直接出补丁，不复现、不重试' },
  linear_agent:    { name: '线性 Agent', desc: '按固定顺序执行命令，失败可重试，但没有策略门禁' },
  patchpilot_full: { name: 'PatchPilot', desc: '技能路由、沙箱验证、失败恢复、策略门禁与证据链' },
};

/** Ordered pipeline stages shown on the overview and in each run. */
export const STAGES: { kind: string; name: string }[] = [
  { kind: 'intake', name: '接收任务' },
  { kind: 'snapshot', name: '固定快照' },
  { kind: 'route', name: '技能路由' },
  { kind: 'compliance', name: '合规扫描' },
  { kind: 'reproduction', name: '复现问题' },
  { kind: 'localization', name: '定位代码' },
  { kind: 'patch', name: '生成补丁' },
  { kind: 'verification', name: '验证门禁' },
  { kind: 'bundle', name: '封存证据' },
];

export function stageName(kind: string) {
  return STAGES.find((s) => s.kind === kind)?.name
    ?? ({ model_plan: '模型规划建议', recovery: '诊断与恢复', retry: '回滚重试' } as Record<string, string>)[kind]
    ?? kind;
}

export type Attempt = { attempt: number; status: string; checks: Record<string, boolean>; failure?: { failure_class: string; message?: string } | null };

export function attemptsOf(events: RunEvent[]): Attempt[] {
  return events
    .filter((e) => e.kind === 'verification')
    .map((e, i) => ({ attempt: i + 1, status: e.data?.status, checks: e.data?.checks || {}, failure: e.data?.failure }));
}

export function eventOf(events: RunEvent[], kind: string) {
  return events.find((e) => e.kind === kind);
}

/** Latest run per scenario whose outcome matches what the scenario is meant to demonstrate. */
export function showcaseRuns(runs: RunRecord[]) {
  const expected: Record<Scenario, Conclusion> = { normal: 'TRUSTED_DELIVERY', failure: 'TRUSTED_DELIVERY', risk: 'NEEDS_REVIEW' };
  const pick = (s: Scenario) =>
    runs.find((r) => r.task?.scenario === s && r.conclusion === expected[s] && (s !== 'failure' || r.attempt > 1))
    ?? runs.find((r) => r.task?.scenario === s);
  return { normal: pick('normal'), failure: pick('failure'), risk: pick('risk') };
}

export const pct = (v: number | undefined) => v == null ? '—' : `${Math.round(v * 100)}%`;
export const secs = (v: number | undefined) => v == null ? '—' : v < 10 ? `${v.toFixed(1)} 秒` : `${Math.round(v)} 秒`;

export function when(value: string | number | undefined) {
  if (!value) return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value.replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
}
