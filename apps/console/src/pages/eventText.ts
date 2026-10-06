import type { RunEvent } from '../api';
import { CHECKS, FAILURE_CLASS, RECOVERY_ACTION, SKILL } from '../model';

/** One plain-Chinese sentence describing what an orchestrator event established. */
export function describe(e: RunEvent): string {
  const d = e.data || {};
  switch (e.kind) {
    case 'intake': return `收到任务 ${d.task_id}。`;
    case 'snapshot': return `在 ${d.repo} 的 commit ${d.commit} 上建立隔离工作区，之后所有步骤都基于这个快照。`;
    case 'route': {
      const picked: string[] = d.selected || [];
      return `从 ${(d.nodes || []).length} 个技能中按前置条件、证据增益和风险选出 ${picked.length} 个：${picked.slice(0, 4).map((s) => SKILL[s] || s).join('、')}${picked.length > 4 ? ' 等' : ''}。`;
    }
    case 'model_plan': return d.summary
      ? `远程模型（${d.model}）给出规划建议：“${d.summary}”。建议仅作参考，执行仍受技能契约和策略约束。`
      : '模型规划建议已记录。';
    case 'compliance': return d.ok
      ? `依赖与许可证扫描通过，发现 ${(d.license_files || []).length} 个许可证文件，${(d.findings || []).length} 项问题。`
      : `依赖与许可证扫描发现 ${(d.findings || []).length} 项问题。`;
    case 'reproduction': return d.returncode === 0
      ? '测试命令在原始代码上通过，没有观察到 Issue 描述的失败。'
      : '测试命令在原始代码上失败，Issue 被成功复现。';
    case 'localization': return `由复现输出中失败的用例定位：${(d.files || []).join('、')}${d.symbols?.length ? `（${d.symbols.join('、')}）` : ''}。`;
    case 'patch': return d.provider === 'patch_file'
      ? `第 ${d.attempt} 个候选补丁来自 ${d.source}，已应用。`
      : d.provider === 'none' ? `第 ${d.attempt} 次尝试没有候选补丁。` : `第 ${d.attempt} 个候选补丁已应用。`;
    case 'test_patch': return `复现前已应用测试补丁 ${d.source}。`;
    case 'verification': {
      const failed = CHECKS.filter((c) => d.checks && d.checks[c.key] === false).map((c) => c.name);
      if (d.status === 'PASSED') return '六项检查全部通过。';
      if (d.status === 'BLOCKED') return `策略门禁拦截：${failed.join('、')}未通过。`;
      return `验证未通过：${failed.join('、') || '见原始记录'}。`;
    }
    case 'recovery': return `失败被归类为“${FAILURE_CLASS[d.failure_class] || d.failure_class}”，执行恢复：${(d.recovery_actions || []).map((a: string) => RECOVERY_ACTION[a] || a).join(' → ')}。`;
    case 'retry': return `开始第 ${d.attempt} 次尝试：${(d.actions || []).map((a: string) => RECOVERY_ACTION[a] || a).join(' → ')}。`;
    case 'bundle': return d.conclusion === 'TRUSTED_DELIVERY'
      ? '证据包已封存，结论为可信交付，PR 草稿可交给维护者审阅。'
      : d.conclusion === 'NEEDS_REVIEW'
        ? '证据包已封存，结论为待复核，PR 草稿没有标记为可合并。'
        : '证据包已封存。';
    default: return e.title;
  }
}

/** Visual tone of a timeline entry. */
export function toneOf(e: RunEvent): 'ok' | 'bad' | 'warn' | 'plain' {
  if (e.kind === 'verification') return e.data?.status === 'PASSED' ? 'ok' : e.data?.status === 'BLOCKED' ? 'bad' : 'warn';
  if (e.kind === 'recovery' || e.kind === 'retry') return 'warn';
  if (e.kind === 'bundle') return e.data?.conclusion === 'TRUSTED_DELIVERY' ? 'ok' : 'bad';
  return 'plain';
}
