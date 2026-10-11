---
feature: secure-vault
status: in-progress
updated: 2026-10-09
branch: feat/secure-vault
commits: # 留空，实现后填写
---

# Secure Vault（DEK 多因子解锁）

## Report

## [S1] Problem

当前应用密码（`feat/app-password`）只校验「是否输对密码」，**数据库仍是明文 SQLite**。用户期望：

1. 新用户强制：扫码绑定 B 站账号 → 强制设应用密码 → Windows Hello/PIN 绑定 → 才能使用。  
2. 日常打开：必须先过 Windows Hello/PIN，不行则输应用密码，再不行则扫码找回。  
3. 忘记密码时，扫码验证身份后仍希望**尽量保留旧数据**（嵌套密钥：数据←DEK←多把 KEK）。  
4. 弄清：数据库是否安全、何时解锁、密钥是不是 id+cookie。

**产品决策（2026-10-09 定案）**：

- **恢复码为唯一离线恢复手段**（用户自行保管）。UI 必须用**醒目、加粗、加大、多次**警告：务必保存好恢复码。  
- **不做** 2FA/验证器；**不做** 开发者万能钥；**不用** 机器指纹混入 DEK/KEK。  
- 扫码 = 身份门；恢复码 = 开箱钥匙。「认证后发新 DEK」**不能**恢复旧密文。

## [S2] Design

### 2.1 密钥嵌套（DEK / KEK）

```text
library.sqlite3（AEAD 整库或敏感字段）
        │  DEK 加密
        ▼
      DEK（32 字节随机，仅内存明文）
        │  磁盘只存被 KEK 包过的 DEK（多份 Wrap）
        ▼
 ┌──────────────┬───────────────┬─────────────────┬─────────────────┐
 │ KEK_password │ KEK_device    │ KEK_account     │ KEK_recovery    │
 │ KDF(应用密码) │ Hello/DPAPI   │ KDF(mid‖cookie) │ KDF(恢复码)     │
 └──────────────┴───────────────┴─────────────────┴─────────────────┘
```

- **DEK**（数据加密密钥）：随机生成，真正加密数据库内容。  
- **KEK**（密钥加密密钥）：包装 DEK；改密码/换绑只是换 Wrap，不是换 DEK。  
- **id+cookie ≠ DEK**：`mid` 为身份；cookie 只参与 `KEK_account`，登录态有效时重写 Wrap_account。  
- **解锁时刻**：任一 KEK 打开 Wrap → 内存中得到 DEK → 允许业务 API；锁定时清 DEK。

### 2.2 用户流程

**新用户（onboarding 门闩：未完成 1–3 则业务 API 403）**

1. 强制「添加账号」：B 站扫码 → `bound_mid` + 生成 DEK + Wrap_account。  
2. 强制设应用密码（≥6 位）→ Wrap_password。  
3. **强制展示恢复码**（高熵，一次生成）并 **多次醒目警告**（见 2.2.1）。  
4. 弹出 Windows Hello/PIN → Wrap_device；可「稍后绑定」，UI 说明将每次输密码。

**2.2.1 恢复码 UI 强制规范**

- 恢复码区域：**大号字体 + 加粗 + 警示色背景**；文案形如「**请立即抄写并离线保存。丢失且忘记密码后，任何人都无法恢复你的数据（包括作者）**」。  
- **重复警告至少两处**（页顶横幅 + 码块下方）。  
- 必须勾选「**我已保存恢复码**」才能继续；提供复制、打印。  
- 关闭后**不再展示明文**；如需再看，须输入应用密码后仅本次会话显示。  
- 磁盘只存 Wrap_recovery，**永不存恢复码明文**。

**日常打开**：Hello → 应用密码 → 进入找回。

**找回（扫码认人 + 用户输入恢复码）**

1. 扫码登录，`mid` 必须等于 `bound_mid`。  
2. **再次**大字加粗提示：「请输入你保存的恢复码（应用无法代查）」。  
3. **用户手动输入恢复码** → 打开 Wrap_recovery（或仍有效的 Wrap_device）→ 保留旧 DEK → 强制改密码。  
4. 全部 Wrap 打不开：明确「无法解开历史数据」→ 重置新库 或 导入加密备份；禁止假装找回成功。

### 2.3 Cookie 与风险控制

- 登录 cookie 仍存 `secrets.json`（现状）；Wrap_account 的 KDF 输入不落盘明文副本。  
- 登录态启动/解锁且 cookie 有效时刷新 Wrap_account。  
- Cookie、恢复码、DEK 不进日志与导出 bundle。  
- 找回成功后必须重设应用密码。

### 2.4 数据保护范围

- 推荐：`library.sqlite3` 整库文件级 AEAD。  
- 至少覆盖：`providers.api_key` 等敏感字段。  
- 导出 bundle 为密文 + Wrap 元数据；不导出恢复码/密码。

### 2.5 与现有 app-lock 关系

- 「解锁会话」升级为「解锁 DEK」。  
- 未解锁 → `app_locked`；onboarding 未完成 → `app_onboarding`。

### 2.6 测试边界

- Wrap 往返、密码/恢复码重设后数据可读、mid 不一致拒绝、全失效不声称无损、onboarding 门闩。  
- CI 不调真实 B 站；无 Hello 环境只测 fallback。

## [S3] Out of Scope

- **2FA / TOTP / FIDO2 作为恢复因子。**  
- **开发者万能钥、机器指纹入钥、云端托管 DEK。**  
- 多用户体系、列级 SQLCipher 调优、网盘自动备份、改 B 站协议。

## Tasks

- [ ] T1: DEK/KEK 模块（生成、Wrap/Unwrap、PBKDF2、恢复码编码）— acceptance: 任一 KEK 可解同一 DEK，重设密码后数据仍可解 (covers: S2.1)
- [ ] T2: 整库 AEAD 读写封装 — acceptance: 未解锁读密文；解锁后等价明文；导入导出为密文 (covers: S2.4)
- [ ] T3: Onboarding（扫码→密码→恢复码强警告→Hello）— acceptance: 未完成前三步业务 API 403；未勾「已保存」不能继续 (covers: S2.2)
- [ ] T4: 启动解锁 Hello → 密码 → 找回 UI — acceptance: 三级顺序可走通 (covers: S2.2)
- [ ] T5: 找回策略（mid 校验、用户输入恢复码、无损 vs 重置）— acceptance: mid 不符拒绝；全 Wrap 失效不声称无损 (covers: S2.2, S2.3)
- [ ] T6: 回归与全量测试 — acceptance: unittest 全绿 (covers: S2.6; depends: T1–T5)
