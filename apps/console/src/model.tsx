/** Chinese vocabulary, icon vocabulary, and derived views over raw API records.
 *
 * Every concept a reviewer meets in the console has one icon and keeps it:
 * a stage carries the same glyph in the overview rail, in the run timeline and
 * in the routing table. That is what makes a run scannable by shape alone.
 */
import {
  BadgeCheck, Bug, CircleCheckBig, CircleX, Crosshair, FileCheck, FileDiff, FlaskConical,
  FolderTree, GitCommitHorizontal, Inbox, LifeBuoy, ListOrdered, Lock, MessageSquareQuote,
  Package, PackageCheck, PlayCircle, RotateCcw, Ruler, Scale, ScanSearch, Search, ShieldAlert,
  ShieldCheck, Stethoscope, Target, Timer, TriangleAlert, Waypoints,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { Conclusion, RunEvent, RunRecord, Scenario } from './api';

export type Verdict = 'pass' | 'hold' | 'fail' | 'unsafe' | 'pending';

export const VERDICT: Record<Verdict, { stamp: string; label: string; note: string; icon: LucideIcon }> = {
  pass:    { stamp: '放行', label: '可信交付', icon: BadgeCheck, note: '复现、测试、范围与合规检查全部通过，可交给维护者审阅' },
  hold:    { stamp: '待复核', label: '已拦截', icon: ShieldAlert, note: '补丁越过了声明的策略边界，未生成可合并的 PR，等待维护者决定' },
  fail:    { stamp: '未通过', label: '修复失败', icon: TriangleAlert, note: '在有限重试内未通过验证，没有交付任何补丁' },
  unsafe:  { stamp: '越界', label: '不安全交付', icon: CircleX, note: '补丁越过策略边界仍被交付（仅出现在对照组）' },
  pending: { stamp: '执行中', label: '执行中', icon: Timer, note: '运行尚未结束' },
};

export function verdictOf(conclusion: Conclusion | string | undefined): Verdict {
  switch (conclusion) {
    case 'TRUSTED_DELIVERY': return 'pass';
    case 'NEEDS_REVIEW': return 'hold';
    case 'FAILED':
    case 'INFRA_ERROR': return 'fail';
    case 'UNSAFE_DELIVERY': return 'unsafe';
    default: return 'pending';
  }
}

export const SCENARIO: Record<Scenario, { name: string; promise: string; icon: LucideIcon }> = {
  normal:  { name: '正常修复', promise: '复现问题，生成补丁，一次通过全部检查', icon: CircleCheckBig },
  failure: { name: '失败恢复', promise: '首次验证失败后诊断原因，回滚并重试成功', icon: LifeBuoy },
  risk:    { name: '风险拦截', promise: '补丁触碰受保护路径，策略门禁拒绝交付', icon: ShieldAlert },
};

/** The six policy checks. Icons match the stage that produced the evidence. */
export const CHECKS: { key: string; name: string; hint: string; icon: LucideIcon }[] = [
  { key: 'reproduction',     name: '问题复现',     hint: '在固定 commit 上复现了 Issue 描述的失败', icon: Bug },
  { key: 'target_tests',     name: '目标测试',     hint: '针对 Issue 的测试在补丁后通过', icon: Target },
  { key: 'regression_tests', name: '回归测试',     hint: '仓库原有测试没有被破坏', icon: RotateCcw },
  { key: 'diff_scope',       name: '改动范围',     hint: '只修改了策略允许的目录', icon: Ruler },
  { key: 'sensitive_paths',  name: '敏感路径',     hint: '没有触碰凭据、CI 或发布配置', icon: Lock },
  { key: 'license_sbom',     name: '许可证与依赖', hint: '依赖与许可证扫描没有发现问题', icon: Scale },
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

export const SKILL: Record<string, { name: string; icon: LucideIcon }> = {
  inspect_repository:       { name: '检查仓库结构', icon: FolderTree },
  inspect_dependencies:     { name: '检查依赖', icon: Package },
  search_symbol:            { name: '定位符号', icon: Search },
  reproduce_pytest_failure: { name: '复现测试失败', icon: Bug },
  diagnose_failure:         { name: '诊断失败原因', icon: Stethoscope },
  propose_patch:            { name: '生成候选补丁', icon: FileDiff },
  apply_patch_safely:       { name: '安全应用补丁', icon: FileCheck },
  generate_regression_test: { name: '生成回归测试', icon: FlaskConical },
  run_regression_suite:     { name: '运行回归测试', icon: PlayCircle },
  static_risk_scan:         { name: '静态风险扫描', icon: ScanSearch },
  license_sbom_scan:        { name: '许可证与 SBOM 扫描', icon: Scale },
  build_evidence_bundle:    { name: '打包证据', icon: PackageCheck },
};

export function skillName(key: string) { return SKILL[key]?.name ?? key; }
export function SkillIcon({ skill, size = 13 }: { skill: string; size?: number }) {
  const Icon = SKILL[skill]?.icon ?? Package;
  return <Icon size={size} />;
}

export const METHOD: Record<string, { name: string; desc: string; icon: LucideIcon }> = {
  direct_llm:      { name: '直接生成', icon: MessageSquareQuote, desc: '把 Issue 交给模型直接出补丁，不复现、不重试' },
  linear_agent:    { name: '线性 Agent', icon: ListOrdered, desc: '按固定顺序执行命令，失败可重试，但没有策略门禁' },
  patchpilot_full: { name: 'PatchPilot', icon: ShieldCheck, desc: '技能路由、沙箱验证、失败恢复、策略门禁与证据链' },
};

/** Ordered pipeline stages. The order is real: each one consumes the last. */
export const STAGES: { kind: string; name: string; note: string; icon: LucideIcon }[] = [
  { kind: 'intake',       name: '接收任务', note: '读取 Issue 与固定 commit', icon: Inbox },
  { kind: 'snapshot',     name: '固定快照', note: '在隔离工作区检出基线', icon: GitCommitHorizontal },
  { kind: 'route',        name: '技能路由', note: '按契约与评分选出技能', icon: Waypoints },
  { kind: 'compliance',   name: '合规扫描', note: '依赖与许可证预检', icon: ScanSearch },
  { kind: 'reproduction', name: '复现问题', note: '确认失败可稳定重现', icon: Bug },
  { kind: 'localization', name: '定位代码', note: '定位到需要修改的符号', icon: Crosshair },
  { kind: 'patch',        name: '生成补丁', note: '生成候选补丁并应用', icon: FileDiff },
  { kind: 'verification', name: '验证门禁', note: '失败则回滚重试，越界则拦截', icon: ShieldCheck },
  { kind: 'bundle',       name: '封存证据', note: '打包补丁、草稿与证据', icon: PackageCheck },
];

const LOOSE_STAGE: Record<string, { name: string; icon: LucideIcon }> = {
  model_plan: { name: '模型规划建议', icon: MessageSquareQuote },
  recovery:   { name: '诊断与恢复', icon: LifeBuoy },
  retry:      { name: '回滚重试', icon: RotateCcw },
};

export function stageName(kind: string) {
  return STAGES.find((s) => s.kind === kind)?.name ?? LOOSE_STAGE[kind]?.name ?? kind;
}

export function StageIcon({ kind, size = 14 }: { kind: string; size?: number }) {
  const Icon = STAGES.find((s) => s.kind === kind)?.icon ?? LOOSE_STAGE[kind]?.icon ?? Timer;
  return <Icon size={size} />;
}

export type Attempt = { attempt: number; status: string; checks: Record<string, boolean>; failure?: { failure_class: string; message?: string } | null };

export function attemptsOf(events: RunEvent[]): Attempt[] {
  return events
    .filter((e) => e.kind === 'verification')
    .map((e, i) => ({
      attempt: i + 1,
      // Older runs stored status inside data; newer API payloads expose it on
      // the event. Keep both so historical evidence remains readable.
      status: e.data?.status ?? e.status,
      checks: e.data?.checks || {},
      failure: e.data?.failure,
    }));
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
export const bytes = (n: number) => n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;

export function when(value: string | number | undefined) {
  if (!value) return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value.replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
}
