# T9 GitHub 保护设置只读核验（2026-10-20）

- 仓库：`Martin-soaring-dev/bili-fav-organizer`
- 目标候选：`4baa66e628538cfb2762b63bed82facd1b47f28a`（tree `2b4a7263705ff345fa1fbab6c6173f0510b55673`）
- 方式：GitHub REST API 只读 GET（`Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-11-28`），
  使用仓库所有者在 Git Credential Manager 中已存的凭据。**未发出任何写请求，未修改任何设置、workflow 或分支规则。**
- 权限前提：`GET /repos/{repo}` 返回 `permissions.admin = true`、`maintain = true`。
  因此下面的「空结果 / 404」是**可信的否定结论**，而不是权限不足导致的盲区。

## 1. 读取结果

| 检查项 | 端点 | HTTP | 返回 | 判定 |
|---|---|---|---|---|
| Environment（审批环境） | `/repos/{repo}/environments` | 200 | `{"total_count":0,"environments":[]}` | 仓库**没有任何 Environment** |
| publish 使用的 environment | `/repos/{repo}/environments/release-approval` | 404 | — | `release-approval` 环境**不存在** |
| main 分支保护 | `/repos/{repo}/branches/main/protection` | 404 | — | `main` **未配置分支保护** |
| Rulesets（含父级） | `/repos/{repo}/rulesets?includes_parents=true` | 200 | `[]` | **没有任何 ruleset**，因此也没有 tag 不可变/保护规则 |
| 旧版 tag protection | `/repos/{repo}/tags/protection` | 404 | — | 无 tag 保护记录 |
| Actions 总权限 | `/repos/{repo}/actions/permissions` | 200 | `{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}` | 动作来源不限制；**未强制 SHA 固定** |
| 默认 workflow 令牌权限 | `/repos/{repo}/actions/permissions/workflow` | 200 | `{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}` | 默认只读；不允许 workflow 自批 PR |

原始逐条探测记录见 `logs/t9-github-api-readonly.log.txt`。

## 2. workflow 侧静态事实（来自候选 tree，可复核）

- `.github/workflows/publish-release.yml`：唯一触发器为 `workflow_dispatch`；顶层 `permissions: contents: read`；
  只有 publish job（第 163 行起）声明 `environment: release-approval` 且 `permissions: contents: write, actions: read`。
- 该 job 内部会复核来源 run 的 `event`、`head_branch`、`ref`、`GITHUB_SHA`/tree、manifest 与六项资产哈希。
- `release-beta.yml` / `release-stable.yml` / `ci-beta-package.yml` / `ci.yml` 均为 `contents: read`，
  build-only 仅在 `main` 上 `workflow_dispatch` 触发。

这些是 **workflow 文件级**事实，**不能替代**仓库设置层的保护。

## 3. 结论：T9 = FAIL（保护缺失），不是 BLOCKED

任务清单要求核实的四项仓库侧保护，本轮在具备 admin 读取权限的前提下逐项确认为**不存在**：

1. **Environment required reviewers** — 无 Environment，`release-approval` 尚未创建。
   后果：`publish-release.yml` 首次 dispatch 时 GitHub 会自动创建一个**无审阅人**的环境，
   publish job 将在无人批准的情况下直接运行，`contents: write` 的发布路径没有人工闸门。
2. **自审限制（prevent self-review）** — 无 Environment，故该设置不存在、更未启用。
3. **管理员绕过策略（can_admins_bypass）** — 同上，无 Environment 即无此策略可核。
4. **tag protection / immutability** — 无任何 ruleset，旧版 tag protection API 也为 404。
   后果：tag 可被移动或删除，发布物与 tag 之间缺少不可变绑定。

因此 T9 不能判 PASS。此项属于**仓库设置缺口**，修复动作（创建受保护 Environment、配置 required reviewers 与
prevent self-review、为 tag 建立 immutable ruleset、择机为 `main` 加分支保护）需要维护者在 GitHub 设置中执行，
本轮按授权**没有**做任何修改。

## 4. 本轮未做

- 未 dispatch 任何 build-only 或 publish workflow。
- 未创建/删除 Environment、ruleset、tag 或分支保护。
- 未创建 PR、未合并、未打 tag、未发布。
