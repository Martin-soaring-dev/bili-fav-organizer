# 接口文档（离线副本）

来源仓库：[pskdje/bilibili-API-collect](https://github.com/pskdje/bilibili-API-collect)（SocialSisterYi/bilibili-API-collect 的同步复刻，2026-01-28 已归档只读）
分支/版本：main（文件最后修改时间见各文档 git 历史）
拉取日期：2026-09-22
说明：本网络环境下 raw.githubusercontent.com 被 DNS 污染，改经 jsDelivr CDN 镜像（cdn.jsdelivr.net）拉取的**原始 markdown 全文**，与原仓库字节一致，未做任何改写。

## 与本项目的对应关系

| 文档 | 内容 | 本项目用处 |
|---|---|---|
| [fav/action.md](fav/action.md) | 收藏夹操作：**新建/修改/删除夹**；**批量复制 copy / 批量移动 move / 批量删除 batch-del / 清空失效 clean** | 审查日志 v2 · P1-6 批量重构的接口依据（resources 参数格式 `aid:2,aid:2,...`，错误码 11010 等） |
| [fav/info.md](fav/info.md) | 收藏夹元数据、created/list-all、collected/list、resource/infos | bili_api.list_folders 对应接口；attr 属性位含"是否默认收藏夹"判定 |
| [fav/list.md](fav/list.md) | resource/list（内容明细，**ps 定义域 1-20**）、resource/ids（全部id） | bili_api.iter_folder_videos 对应接口；has_more 翻页语义；medias[].attr 失效标记（0 正常/1 删除/9 UP自删）；批内校验可回读 ids 接口 |
| [misc/sign/wbi.md](misc/sign/wbi.md) | WBI 签名算法（w_rid/wts、mixinKeyEncTab 完整实现与多语言示例） | bili_api._wbi_sign 的算法依据与验证参照 |
| [misc/errcode.md](misc/errcode.md) | 公共错误码 | bili_api._request 中 -101/-658/-352/-503/412 等分支的语义核对 |
| [video/action.md](video/action.md) | 稿件观众操作（收藏 deal 接口的另一份描述，含 add/del_media_ids 语义） | bili_api.add_with_aid / remove_from_folder 参数依据 |
| [login/login_info.md](login/login_info.md) | nav 接口（获取 mid、wbi_img 密钥来源） | get_mid / _refresh_wbi_keys 的响应结构参照 |
| [login/login_action/QR.md](login/login_action/QR.md) | 网页二维码登录（generate/poll、状态码 86101/86090/86038） | server.py 扫码登录接口的依据 |

## 批量重构（P1-6）前需留意的文档要点

- `resources` 参数：`{内容id}:{类型}` 逗号分隔，视频类型固定 `2`，内容 id 用 **avid**（非 bvid）；
- 文档未标注单请求条数上限 → 仍需按审查日志 v2 实测（从 10 条起探测）；
- 错误码：`-101 未登录`、`-111 csrf失败`、`-400 请求错误`、`11010 内容不存在`（批内混入已消失视频时注意）；
- `fav/resource/list` 的 `ps` 上限 20 是读接口的分页限制，与写接口批量大小无关，勿混淆。
