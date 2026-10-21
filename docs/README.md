# B 站接口规范与项目验证记录

本项目收藏夹接口以 [pskdje/bilibili-API-collect](https://github.com/pskdje/bilibili-API-collect) 为参考。

## 恢复来源

- 原项目在 2026-09-22 引入离线规范，`fc66b67` 于 2026-09-30 删除了它们。
- 本次将收藏夹相关三份原文从删除前提交 `ed74d0247876a982f4a62df0fbca96629efb70d7` 恢复；文件内容逐字节保留，不改写为项目自己的规范。
- 原始作者：SocialSisterYi 及 bilibili-API-collect 贡献者；引用的同步仓库维护者：pskdje。
- 这些第三方原文遵循 **CC BY-NC 4.0**，见 [fav/LICENSE](fav/LICENSE) 与 [fav/NOTICE.md](fav/NOTICE.md)；不适用本项目代码的 PolyForm 许可。

## 规范与调用位置

| 原始文档 | 接口与用途 | 当前实现 |
|---|---|---|
| [fav/list.md](fav/list.md) | `resource/ids` 完整 ID 清单；`resource/list` 分页明细 | `get_folder_resource_ids`；`iter_folder_videos`（异常兜底） |
| [fav/info.md](fav/info.md) | `resource/infos` 批量明细；收藏夹目录与信息 | `get_resource_infos_bulk`；`list_folders`；`get_folder_info` |
| [fav/action.md](fav/action.md) | 新建、修改、删除收藏夹；批量移动、删除内容 | `bili_api.py` 对应写操作 |

二维码、登录、WBI 与错误码规范继续通过上游仓库查阅。

## 当前扫描约定

每夹一次 `resource/ids`；复用已有的本地明细，把缺少的 ID 合并成最多一次 `resource/infos`（正常收藏夹范围 0–1000 条）。
ID 清单完整但明细少返回时，保留全部已确认的收藏 ID，标明缺失明细，不自动把整夹改成 20 条分页重扫。
缓存完整时只需 ID 清单请求；再次扫描只补查仍缺少明细的 ID。
`resource/list` 的 `ps=20` 是另一接口的分页限制，不是批量读取条数。

## 测试与历史依据

- [API 实测沿革与本次回归](testing/favorite-api-history.md)
- [扫描与 SQLite 方案](design/scan-sqlite-migration-plan.md)
- [持久索引与收藏夹画像](design/persistent-index-folder-profiles.md)

- [任务数量、并发与间隔](user/task-settings.md)
