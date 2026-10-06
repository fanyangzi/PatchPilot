/** Fixed data for the two visual samples: 仓库体检 and 策略.
 *
 * Both pages draw from the constants below so the layout can be judged before
 * the measurements behind it exist. Nothing here is a real run, and the
 * repository is fictional. Both pages carry a 样张 marker for that reason.
 * When workstreams 一 and 二 land, these pages read the same shapes off the API.
 */

export type CheckState = 'pass' | 'block' | 'skip';

export type CheckPath = { path: string; change: string; note: string; ok: boolean };
export type CheckHit = { path: string; pattern: string };
export type Evidence =
  | { kind: 'raw'; label: string; text: string }
  | { kind: 'diff'; label: string; text: string }
  | { kind: 'facts'; label: string; rows: [string, string][] };

export type CheckRow = {
  id: string;
  key: string;
  name: string;
  state: CheckState;
  judgement: string;
  basis: string;
  paths?: CheckPath[];
  hits?: CheckHit[];
  evidence?: Evidence[];
};

const COMMIT = '3e5a1c7';
const TARGET = 'tests/test_options.py::test_multiple_default_returns_tuple';
const WORKFLOW = '.github/workflows/release.yml';

export const INSPECT = {
  repo: 'example-org/ledgerkit',
  commit: COMMIT,
  branch: 'main',
  issueTitle: 'multiple=True 的选项不传参时返回空元组，而不是 default',
  issueBody:
    "声明了 @option('--tag', multiple=True, default=('a', 'b')) 之后，命令行不传 --tag，拿到的是 ()，我预期是 ('a', 'b')。显式传一次 --tag x 是正常的。",
  patchSource: '远端模型提案',
  attempts: 1,
  runtimeSec: 12.4,
  verdict: 'hold' as const,
  lead: '修复成立，但补丁越界，已拦截。',
  detail:
    '目标用例由红变绿，另外 411 个用例没有变红。但补丁顺手改了发布工作流，还给它申请了写权限。这超出了仓库在 AI_POLICY.md 里声明的范围，所以没有生成 PR 草稿。',
  next: '是否放行由维护者决定。要改这条判定，先改仓库自己写的策略。',
  sealedFiles: 1,
  checks: [
    {
      id: '01',
      key: 'reproduction',
      name: '问题复现',
      state: 'pass',
      judgement: '目标用例在补丁前稳定失败',
      basis: `${TARGET}，补丁前 3 次里失败 3 次`,
      evidence: [
        {
          kind: 'raw',
          label: `补丁前，固定 commit ${COMMIT}，第 1 次`,
          text: `$ pytest ${TARGET} -q\nF                                                                        [100%]\nFAILED ${TARGET}\n  assert () == ('a', 'b')\n1 failed in 0.38s`,
        },
        {
          kind: 'raw',
          label: '补丁后，同一个用例',
          text: `$ pytest ${TARGET} -q\n.                                                                        [100%]\n1 passed in 0.42s`,
        },
      ],
    },
    {
      id: '02',
      key: 'target_tests',
      name: '目标测试',
      state: 'pass',
      judgement: 'issue 声明的 1 个目标用例全部通过',
      basis: '补丁后 1 passed in 0.42s',
    },
    {
      id: '03',
      key: 'regression_tests',
      name: '回归测试',
      state: 'pass',
      judgement: '补丁前通过的用例，补丁后一个都没有变红',
      basis: '412 passed, 2 skipped；基线是 411 passed, 2 skipped，新增 1 个用例',
      evidence: [
        {
          kind: 'facts',
          label: '两次逐用例结果的集合差',
          rows: [
            ['补丁前通过，补丁后失败', '0 个'],
            ['补丁前通过，补丁后仍通过', '411 个'],
            ['补丁后新增并通过', '1 个'],
          ],
        },
      ],
    },
    {
      id: '04',
      key: 'diff_scope',
      name: '改动范围',
      state: 'block',
      judgement: '3 个改动路径里有 1 个不在允许范围内',
      basis: '路径来自对补丁的解析，不是对任务声明的比对',
      paths: [
        { path: 'src/ledgerkit/options.py', change: 'M', note: '匹配 src/**', ok: true },
        { path: 'tests/test_options.py', change: 'M', note: '匹配 tests/**', ok: true },
        { path: WORKFLOW, change: 'M', note: '不匹配任何允许路径', ok: false },
      ],
      evidence: [
        {
          kind: 'facts',
          label: '解析后的改动',
          rows: [
            ['改动文件', '3'],
            ['新增行', '18'],
            ['删除行', '4'],
            ['重命名', '0'],
            ['删除文件', '0'],
            ['二进制', '0'],
            ['权限位变更', '0'],
          ],
        },
      ],
    },
    {
      id: '05',
      key: 'sensitive_paths',
      name: '敏感路径',
      state: 'block',
      judgement: '改动命中 2 条敏感模式',
      basis: '模式表来自仓库自己的策略，共 14 条',
      hits: [
        { path: WORKFLOW, pattern: '**/.github/workflows/**' },
        { path: WORKFLOW, pattern: '新增行含 permissions: write-all' },
      ],
      evidence: [
        {
          kind: 'diff',
          label: '命中的新增行',
          text: `--- a/${WORKFLOW}\n+++ b/${WORKFLOW}\n@@ -11,3 +11,4 @@ jobs:\n   publish:\n     runs-on: ubuntu-latest\n+    permissions: write-all\n     steps:`,
        },
      ],
    },
    {
      id: '06',
      key: 'license_sbom',
      name: '许可证与依赖',
      state: 'pass',
      judgement: '没有新增依赖，许可证没有变化',
      basis: 'pyproject.toml 未改动',
      evidence: [
        {
          kind: 'facts',
          label: '依赖清单',
          rows: [
            ['运行时依赖', '未改动'],
            ['开发依赖', '未改动'],
            ['LICENSE', 'BSD-3-Clause，与基线一致'],
          ],
        },
      ],
    },
  ] as CheckRow[],
  chain: [
    { seq: '0012', kind: 'verification', prev: '9e2d6a03f7b1', hash: 'c41e8b70a2f9' },
    { seq: '0013', kind: 'gate', prev: 'c41e8b70a2f9', hash: '5d0a37ce19b4' },
    { seq: '0014', kind: 'verdict', prev: '5d0a37ce19b4', hash: 'e77b12a45cd0' },
  ],
  verdictHash: '4a7f2c91b3d8e5063f1a9c24d7b0e8f2a61c4d39e5702b8f1c6a3d94e07b5f28',
  runId: `2026-10-03T14-22-08_${COMMIT}_a41f`,
};

export type PolicyRule = { key: string; value: string; list?: string[]; note?: string; strong?: boolean };

export type Clause = {
  n: string;
  text: string;
  rules: PolicyRule[];
  skipped?: string;
};

const ALLOWED = ['src/**', 'tests/**', 'docs/**', 'CHANGELOG.md'];

const SENSITIVE_PATHS = [
  '**/.github/workflows/**',
  '**/.github/actions/**',
  '**/.gitlab-ci.yml',
  '**/*.lock',
  '**/requirements*.txt',
  '**/pyproject.toml',
  '**/*.pem',
  '**/*.key',
  '**/id_rsa*',
  '**/service-account*.json',
  '**/credentials*',
  '**/.env*',
];
const SENSITIVE_LINES = ['permissions: write-all', 'pull_request_target'];

const REQUIRED = [
  ['reproduction', '问题复现'],
  ['target_tests', '目标测试'],
  ['regression_tests', '回归测试'],
  ['diff_scope', '改动范围'],
  ['sensitive_paths', '敏感路径'],
  ['license_sbom', '许可证与依赖'],
] as const;

const SOURCE = 'AI_POLICY.md';
const SOURCE_COMMIT = 'a3f9e07';
const COMPILED_AT = '2026-10-03';

const sensitiveCount = SENSITIVE_PATHS.length + SENSITIVE_LINES.length;

const quote = (s: string) => `"${s}"`;
const block = (key: string, note: string, body: string[]) => [
  `${`${key}:`.padEnd(30)}# ${note}`,
  ...body.map((line) => `  ${line}`),
];

const yamlLines = [
  `# 由 ${SOURCE}（${SOURCE_COMMIT}）编译，${COMPILED_AT}`,
  'scope:',
  '  branches: [main]',
  ...block('allowed_paths', '第 01 条', ALLOWED.map((p) => `- ${quote(p)}`)),
  ...block('regression', '第 02 条', ['baseline_must_stay_green: true']),
  ...block('dependencies', '第 03 条', ['allow_new_runtime: false']),
  ...block('sensitive_paths', `第 04 条，共 ${SENSITIVE_PATHS.length} 条`, SENSITIVE_PATHS.map((p) => `- ${quote(p)}`)),
  ...block('sensitive_added_lines', `第 04 条，共 ${SENSITIVE_LINES.length} 条`, SENSITIVE_LINES.map((p) => `- ${quote(p)}`)),
  ...block('required_checks', '固定', REQUIRED.map(([k]) => `- ${k}`)),
  ...block('retry', '固定', ['max_attempts: 2', 'by: failure_class']),
  `${'on_missing_policy: deny'.padEnd(30)}# 固定，fail-closed`,
];

export const POLICY = {
  repo: INSPECT.repo,
  source: SOURCE,
  sourceCommit: SOURCE_COMMIT,
  compiledAt: COMPILED_AT,
  scope: 'main',
  preamble: '以下是我们对 AI 辅助补丁的态度，供贡献者阅读。',
  sensitiveCount,
  clauses: [
    {
      n: '01',
      text: '补丁只应改动 src/、tests/、docs/ 下的文件，以及 CHANGELOG.md。',
      rules: [
        {
          key: '允许路径',
          value: `${ALLOWED.length} 条，范围之外一律不收`,
          list: ALLOWED,
          note: '按路径的每一层匹配，src/** 不会匹配 src-old/',
        },
      ],
    },
    {
      n: '02',
      text: '提交前请确保仓库的测试全部通过，而不只是你新加的那一个。',
      rules: [
        {
          key: '回归测试',
          value: '补丁前通过的用例，补丁后必须仍然通过',
          note: '逐用例比对，不看整条命令的退出码',
        },
      ],
    },
    {
      n: '03',
      text: '请不要在补丁里引入新的运行时依赖；如果确有必要，请先在 issue 里说明。',
      rules: [
        {
          key: '依赖与许可',
          value: '禁止新增运行时依赖',
          note: '出现新依赖就拦截。「先在 issue 里说明」没有可判定的条件，放行由维护者决定',
        },
      ],
    },
    {
      n: '04',
      text:
        '以下文件不应出现在普通贡献者的补丁里：CI 配置（.github/ 下的工作流与 actions、.gitlab-ci.yml）、lock 文件、requirements*.txt 与 pyproject.toml，以及密钥与凭据（*.pem、*.key、id_rsa*、service-account*.json、credentials*、.env*）。新增 permissions: write-all 或 pull_request_target 的补丁，一律不收。',
      rules: [
        {
          key: '敏感模式',
          value: `${sensitiveCount} 条，命中即拦截`,
          list: [...SENSITIVE_PATHS, ...SENSITIVE_LINES.map((l) => `新增行含 ${l}`)],
        },
      ],
    },
    {
      n: '05',
      text: '如果你不确定，宁可先开一个 issue 讨论。',
      rules: [],
      skipped: '建议性语句，没有可判定的条件。原文保留，不生成空检查。',
    },
  ] as Clause[],
  floor: [
    {
      key: '必需检查',
      value: '6 项，每一项都要有依据',
      list: REQUIRED.map(([, name]) => name),
      note: '缺任何一项，结论都不会是「可信交付」',
    },
    {
      key: '重试',
      value: '上限 2 次，按失败分类',
      note: '不由场景标签决定，也不对同一个失败反复重试',
    },
    {
      key: '策略缺失时',
      value: '拒绝运行',
      note: '读不到策略，不会被当作「不受限」',
      strong: true,
    },
  ] as PolicyRule[],
  yaml: yamlLines,
};
