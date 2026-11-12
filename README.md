# 三谋实用插件

三国：谋定天下的本地战报采集、无损归档与查询工具。提供战报档案、每日统计、队伍胜率、对手历史阵容和无双记录，也支持把队伍及战法配置完整导出为 Excel。

本仓库是可移植的软件副本，不包含使用者的数据库、采集缓存、账号配置或运行日志。大型战报库通过独立归档提供；[下载清单、校验值和恢复工具](docs/database-archive.md)随数据归档发布，数据库恢复到 `data/database/battle_reports.db` 后即可查阅。软件也可以创建自己的新数据库。

2026-11-13 完整数据库： [Google Drive 单文件（7.12 GB）](https://drive.google.com/file/d/14q4KEkgUb4kM-S0b2oiVC9dvC_JQ0BRQ/view?usp=drivesdk) · [GitHub Release 分卷](https://github.com/ziyi612/sanmou-utility-plugins/releases/tag/data-20261113) · [逐文件校验清单](data/releases/2026-11-13.json)。原 DB 与 WAL 合计约 114.21 GB；恢复前请阅读[空间需求及步骤](docs/database-archive.md)。

## 安装

使用 Windows 和 Python 3.10 或更新版本。官方 Windows Python 通常包含 Tkinter；实时采集另需 `frida`、`psutil`，Excel 导出使用 `openpyxl`，不需要 Node 或任何私人运行环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

先运行不连接游戏的环境检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_battle_archive.ps1 -Check
```

双击 `start_battle_archive.cmd` 打开界面，或直接运行：

```powershell
.\.venv\Scripts\python.exe -B battle_reports_gui.py
```

启动器优先使用项目的 `.venv`，随后读取 `config/launcher.json` 的可选 `python_executable`，最后查找 PATH 中的 Python；也可传 `-PythonExecutable <解释器路径>`。默认文件不包含任何个人解释器路径。

## 采集与查询

打开窗口不会自动连接游戏或开始采集。登录角色后，点击“开始采集”；游戏和工具需要使用匹配的权限。点击“停止采集”结束；关闭窗口后，已启动的后台采集仍会继续，因此需要停止时请先使用停止按钮。再次打开会识别已有采集器并防止重复启动。

界面默认仅采集 PVP；需要 PVE 时先停止再修改选项。采集范围包括当前角色可见的个人、同盟和收藏战报及其子报。CLI 默认包含全部类型，需要 PVP 筛选时显式传 `--pvp-only`。

```powershell
python -B battle_reports.py collect --console --pvp-only
python -B battle_reports.py status
python -B battle_reports.py query "SELECT battle_uid,completeness,event_count FROM report_current_snapshots" --limit 20
python -B battle_reports.py export 1 .\output\exports\snapshot_1.json
```

每轮默认等待 60 秒；失败记录保留重试和续采状态。原始字节按命名空间和 SHA 去重，保存历史快照、未知字段及失败原因，不以较差快照替换完整事件。完整性指单封五组件检查，不表示整个账号所有战报已经采齐。

数据库及采集输出所在磁盘默认至少保留 **5 GiB** 空间。启动、本地恢复、下一页、下一封和入库前会检查；不足时结束本次采集，保存已收到的原文和待入库检查点，释放空间后需要手动启动，不会自动恢复采集。CLI 可通过 `--min-free-gib` 调整正数阈值。查询与原文导出不受该采集阈值限制；保留阈值不能阻止其他程序同时占满磁盘，也不会自动删除历史数据。

## 文件与导出

- `data/database/`：本地 SQLite 战报库，数据库及 WAL/SHM 不进入 Git。
- `data/incoming/runtime/`：各账号分开的原始采集文件、检查点和续采状态，不进入 Git。
- `data/dictionaries/`：中文语义字典；公开副本已移除采集账号身份元数据，保留映射、适用范围和来源哈希。
- `logs/`、`output/`：本地日志及用户导出，不进入 Git。
- `.runtime/temp/`：默认临时目录；可用 `SANMOU_TEMP_DIR` 指定其他位置。

队伍胜率与具体战法配置分别要求至少 20 场胜平负已知结果，未知结果不凑门槛。Excel 导出包含“队伍胜率”“战法配置”两张表及全部符合当前筛选的档位和配置，保留精确文本 ID、筛选、排序、百分比和公式缓存。导出拒绝覆盖已有文件或数据库及其附属文件。

## 验证范围

当前公开配置默认并行窗口为 **1**。窗口 2–4 仍为实验参数；此前冷缓存双封实测出现组件超时，不能承诺提速或完整采集。已有实机结果证明特定客户端下的列表读取、PVP 筛选和部分单封五组件导出，不代表所有客户端版本、账号或历史战报都可恢复。

游戏过期、权限限制、服务器未返回或已消失的数据不能凭空补回。对手名单来自历史战报，不表示实时出战阵容；无双记录仅接受来源明确的实际游戏判定。中文解释以随附的静态与运行时来源快照为依据，未知项继续显示原值。离线测试不作为实时游戏采集成功的证明。

详细保存结构与边界见 [数据库说明](docs/battle_database.md)。本仓库不包含资源规划器、其他游戏工具、逆向转储或使用者的私人文件，也没有替第三方字典和数据指定统一授权。

## 离线测试

```powershell
python -m pip install -r requirements-dev.txt
python -B -m pytest tests/test_collection_storage_guard.py tests/test_team_winrate_export.py tests/test_collection_stats.py tests/test_battle_ui_data.py -q --basetemp=.runtime/pytest
```

这些测试使用小型合成数据库或桥接替身，不连接游戏。旧的个人盘符策略测试在公开版中停用；依赖未分发真实战报样本或原始反编译 Lua 的历史集成测试会跳过，避免把私人样本一起发布。不要把归档中的真实数据库或账号配置加入 Git。
