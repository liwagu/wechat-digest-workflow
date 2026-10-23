# 依赖与兼容性

## WeChat Toolkit

- 集成使用的公开项目：[1939869736luosi/wechat-toolkit](https://github.com/1939869736luosi/wechat-toolkit)。
- 其上游：[nan1888/wechat-toolkit](https://github.com/nan1888/wechat-toolkit)。
- 许可证：MIT，上游版权声明为 `Copyright (c) 2026 Xiaowei`；本仓库不分发其源码。
- 本次接口核对所用源码修订：`b7dbdff61000a2e31b74900cf0192e1b8e77b96b`。这是接口基线，不代表所有微信版本已通过兼容性测试。

配置的 CLI 和 `toolkit_src` 应指向同一份 Toolkit 安装。需要的接口：

| 模块/入口 | 所需能力 |
| --- | --- |
| `wechat groups search` | 返回群 ID 与名称 |
| `wechat msg` | 取得按名称查询的 CLI 基线 |
| `wechat_keys.ensure_keys` | 获得本机数据库访问缓存 |
| `wechat_keys.get_db_base` | 定位本机数据库目录 |
| `wechat_keys.sqlcipher_query` | 执行数据库查询 |
| `wechat_query.resolve_contacts` | 将发送人标识映射为本机可见名称 |
| `wechat_query._find_msg_table` | 根据群 ID 定位消息表 |
| `wechat_query._get_msg_dbs` | 枚举消息分库 |

下划线开头的函数属于内部接口。上游更新后可能需要适配，不能仅凭 CLI 命令存在判断兼容。

脚本按目前的消息表字段读取：`local_id`、`server_id`、`create_time`、`local_type`、`message_content`、`compress_content`、`real_sender_id`，并关联 `Name2Id`。微信数据库结构变化时应明确报错，不能将失败当作空讨论。

## 其他运行依赖

- Python 3.11+：标准库处理时间、文件、XML、进程调用与 JSON。
- SQLCipher：由 Toolkit 查询加密数据库。项目：[sqlcipher/sqlcipher](https://github.com/sqlcipher/sqlcipher)。
- libzstd：由 Python `ctypes` 调用，解压消息内容。项目：[facebook/zstd](https://github.com/facebook/zstd)。
- Codex：外部执行和分析环境。本项目不包含模型服务、模型密钥或定时调度器。

依赖遵循各自许可证。本仓库的 MIT 许可证仅覆盖本仓库内容。

## 验证范围

自动测试覆盖合成消息的正文解析、zstd 解压、引用关系、精确时间边界、范围计数、去重、错误退出与生成文件。模拟 Toolkit 的集成测试不访问真实微信，无法证明所有账号、微信版本、操作系统或 Codex 运行环境都兼容。
