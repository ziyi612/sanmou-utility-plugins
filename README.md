# 三谋实用插件

三国：谋定天下的战报采集、无损归档与查询工具。支持严格夸克云端模式，也保留本地数据库模式。严格模式只在运行内存中处理战报；本地模式另提供每日统计、队伍胜率、对手历史阵容、无双记录及 Excel 导出。

本仓库是可移植的软件副本，不包含使用者的数据库、采集缓存、账号配置或运行日志。大型战报库通过独立归档提供；[下载清单、校验值和恢复工具](docs/database-archive.md)随数据归档发布，数据库恢复到 `data/database/battle_reports.db` 后即可查阅。本地模式也可以创建自己的新数据库；严格夸克模式不会创建本地战报数据库。

2026-11-13 完整数据库： [Google Drive 单文件（7.12 GB）](https://drive.google.com/file/d/14q4KEkgUb4kM-S0b2oiVC9dvC_JQ0BRQ/view?usp=drivesdk) · [GitHub Release 分卷](https://github.com/ziyi612/sanmou-utility-plugins/releases/tag/data-20261113) · [逐文件校验清单](data/releases/2026-11-13.json)。原 DB 与 WAL 合计约 114.21 GB；恢复前请阅读[空间需求及步骤](docs/database-archive.md)。

## 严格夸克云端模式

严格模式仅使用本人授权的夸克网盘保存战报。采集到的原文、列表发现、组件片段、检查点、云端目录索引和续采状态通过管道与内存文件系统处理，上传并重新下载核对长度和 SHA-256 后才确认保存；它们不会写入本地数据库、JSON、缓存、任务文件或战报日志。打开档案时联网读取目录和所选原文，再用内存 SQLite 生成详情；离开或关闭后不留下本地战报副本。

首次使用仍需在夸克官方工具实际支持的 Codex 环境中完成账号授权和归档目录初始化。已有授权后运行：

```powershell
python -B tools/setup_quark.py --memory-only
python -B battle_reports_gui.py
```

安装器会校验固定版本的官方运行时，在项目 `.runtime/quark-memory/` 安装 `memfs@4.80.1` 和 `fs-monkey@1.1.0`，并在本地配置及启动配置中持久记录 `quark-memory` 模式。再次打开或从命令行采集都会沿用该模式；配置缺失或损坏时阻止启动，不能悄悄回退到本地存储。程序、依赖和不含战报的设置仍保存在项目目录；官方工具仅可在原有账号目录读取或刷新既有凭据，凭据不进入 GitHub。这个适配沿用真实授权的 Codex 上下文，不表示夸克提供通用 standalone SDK。

严格模式下，直接运行本仓库已分发的旧入口 `capture/game_assets/battle_collector.py` 或 `capture/game_assets/export_battle_report_runtime.py`，会在创建输出目录或初始化游戏连接前停止，并提示使用 `start_battle_archive.cmd` 或 `python -B battle_reports.py collect`。已有模式配置损坏或含未知值时也会先拒绝执行；未配置及旧本地兼容模式保留原有行为。切换存储模式前请先停止已有采集器，再从统一入口启动。

严格模式有以下使用边界：

- 查阅档案需要联网。云端读取失败会显示错误，不使用旧目录或本地缓存冒充当前结果。
- 采集并行数固定为 1；可选全时段、最近 24 小时、最近 72 小时或最近 7 天，默认最近 24 小时。按战斗发生时间筛选，持续采集时窗口自动滚动。
- 上传、回读校验或云端目录提交失败时暂停采集，未确认的内容保留在当前进程 RAM，联网后重试；没有确认保存的内容不会被当成已完成。
- 关闭窗口会请求停止采集，并等待当前云端保存步骤结束。强制关闭、崩溃或断电会丢失尚未成功上传的 RAM 内容；已在云端确认保存的内容可以重新读取。
- 每日统计、对手历史、无双和队伍胜率暂不提供；所有战报与表格本地导出均禁用。档案详情和事件翻页仍可使用。

已完成一份 2,122 字节小样本对象通过官方运行时的纯 RAM 上传和回读，以及完整目录 53 条记录的读取；对应适配器审计记录战报磁盘写入为 0。它验证的是本次账号和小样本流程，不能替代所有历史文件的迁移核验。历史数据须逐份上传、完整回读验证并确认云端索引后才能清理本地；仅启用模式不会自动清理旧数据，历史原件须通过下述迁移命令独立校验并显式清理。

内存 HTTPS 适配器还完成过一次真实云端单对象完整读取：8,386,149 字节的 SHA-256 与预期一致，适配器记录战报磁盘写入为 0。默认传输仍为 native；这项读取验收不表示已经查明网络错误根因，也不证明批量历史迁移已全部完成。

可选的 urllib 内存 HTTPS 适配器仅在夸克 CDN 出现尚未发出 HTTP 请求的 `EPROTO` 类 TLS 握手错误时，临时通过 AliDNS 获取最多两个公网 IPv4 备用地址；连接仍使用原域名的 Host、SNI 和证书校验，已发出的请求不由此回退重发。备用解析与诊断仅保存在 RAM，不修改系统 DNS，不降低 TLS 校验；默认传输方式仍为 native。

### 历史迁移与清理

迁移前先停止采集器并关闭战报窗口，避免旧数据库、WAL 或原文在核验期间变化。以下命令使用程序既定的战报目录范围，默认只上传与校验，保留本地原件：

```powershell
python -B battle_reports.py cloud migrate --root .
```

确认云端可正常查阅后，需要清理时显式添加 `--delete-local`。也可以重复指定 `--source`，将范围限制为根目录内明确选定的战报来源：

```powershell
python -B battle_reports.py cloud migrate --root . --source .\data\incoming --source .\data\archive
python -B battle_reports.py cloud migrate --root . --delete-local
```

每个本地文件都必须有完整云端内容和清单引用，清理前再次独立回读云端并核对整份文件哈希；上传成功提示本身不能授权删除。Windows 下，同一登录会话内的迁移与采集使用目录互斥锁排除并发写入；数据库读取保持一致视图。路径越界、符号链接或重解析点、硬链接、源文件变化和任一校验失败都会阻止对应清理。默认目录范围不含账号配置、软件、依赖或字典；显式 `--source` 会处理所选路径中的全部普通文件，应仅指定待迁移的战报来源。

### 已有大压缩备份的独立冷归档

已经生成的大型 `.zip`、`.7z` 或 `.tar.zst` 备份可使用独立工具按原字节保存。程序把压缩包视为完整文件，不解压，不新建本地分卷、清单或缓存；为遵守当前授权下载接口实测的单对象 50 MiB 读取上限，每片最多读取 48 MiB 原字节，RAM 内封装后不得超过 50 MiB。冷归档使用授权目录下的独立子文件夹，位置覆盖仅存在于本次运行内存，不修改原夸克配置，也不混入日常档案索引。日常战报仍使用原有小分块策略，读取兼容经过完整描述与长度校验的大分块。

先准备备份原件的 SHA-256，将下面的占位摘要换成实际的 64 位十六进制值。`--expected-sha256` 使用相对于 `--root` 的 `路径=摘要`，并可与 `--source` 一样重复传入。以下示例中的备份文件已存在于根目录内：

```powershell
python -B tools/migrate_quark_archive.py --root . --source .\output\archives\backup.tar.zst --expected-sha256 "output/archives/backup.tar.zst=<64位SHA-256>"
```

默认上传、独立回读校验后保留原件。只验证已经上传的冷归档时加 `--verify-only`，不会创建云文件夹、上传或删除；该操作仍使用所选本地原件预检与比对。需要清理时显式加 `--delete-local`；两种标记不能同时使用：

```powershell
python -B tools/migrate_quark_archive.py --root . --source .\output\archives\backup.tar.zst --verify-only
python -B tools/migrate_quark_archive.py --root . --source .\output\archives\backup.tar.zst --expected-sha256 "output/archives/backup.tar.zst=<64位SHA-256>" --delete-local
```

只有全部所选文件经过新的独立读取器完整回读并核对整份 SHA-256、随后本地原件再次逐份核验通过，才允许开始清理。云端校验、源文件稳定性或路径安全检查失败会阻止清理；中途清理失败会报告未完成并保留尚未删除的文件。冷备保留标准云端清单与分块，可通过标准 `CloudStore.iter_file()` 恢复原始字节流。

已通过真实云端完成一个 48 MiB 分片的上传、新鲜回读和独立读取，完整 SHA-256 一致；本次档案详情和事件翻页调用的适配器审计记录战报磁盘写入为 0。上述验收不表示大型历史备份已整体迁移或本地原件已清理。

冷备中的压缩数据库是历史备份，不能直接作为当前档案执行 SQL，也不承诺将整个大数据库在 RAM 中打开。日常列表与详情仍读取 active 单封战报索引。离线大分片回归验证不等于大归档已完成真实云端迁移或本地清理。

## 本地兼容模式：省空间采集

采集默认使用 `lean` 模式：保留完整原文、五组件、有序事件和统计元数据，省略事件、武将快照及统计组件的重复逐字段展开；相同中文名称映射的完整来源共享保存，事件中文说明在打开详情时按当前页生成。每日统计、队伍胜率、对手、无双和历史阵容查询继续使用本地投影。

`collect --storage-mode compact` 可选择仅精简事件索引的方式，`--storage-mode full` 保存全部字段索引及持久中文事件说明。离线 `import` 默认仍为 `full`。同库可保留不同模式，重复导入不会转换旧快照；已有原件、归档和备份不会自动清理。模式区别与完整来源读取方式见[数据库说明](docs/battle_database.md)。

## 本地兼容模式：夸克原文归档

以下为保留本地数据库和统计投影的兼容方案，与上面的严格模式使用不同存储流程。严格模式不使用这里的磁盘缓存、原文导出或数据库压缩命令。

“夸克云端”页面提供登录、启用归档、查看状态及手动同步。首次启用前需由本人完成夸克账号授权；没有授权、上传超时或空间不足时，本地原文保留。安装器 `python -B tools/setup_quark.py` 会下载并核对固定版本的夸克官方运行时，写入不含令牌的 `config/quark.local.json`；账号凭据由官方工具保存在运行时目录内，不进入 GitHub。

登录后点击“启用战报归档”，程序在本人网盘创建“三谋战报归档”目录。下一次开始采集时，后台线程会分批上传已入库原文；手动“同步接下来10份原文”也可同步。每份原文直接按原字节 gzip 压缩，上传后必须重新下载，验证压缩文件和解压原文的 SHA-256 与长度，最后才在事务中保存云端引用并释放数据库内嵌原文。没有校验完成的上传绝不计为迁移成功；失败任务可以重试，已取得文件ID的任务复用该ID继续回读。

原文预览、导出、完整核验、组件恢复及阵容投影修复都使用统一的原文读取接口：本地原文优先，其次为经过校验的缓存，缺失时从夸克下载。默认缓存上限为 1 GiB；只有可从已验证云对象恢复的缓存可被驱逐。首次释放内嵌原文前会启用数据库版本2，以免旧程序把远程占位误当成空原文；新版仍能读取版本1数据库。

本版仍保留本地完整快照、组件、事件和统计投影，因此日常统计不依赖网络。活动续采JSON与已有历史目录不自动清理。释放的数据库内嵌空间会成为可复用空闲页；停止采集后可用 `python -B battle_reports.py cloud compact` 回收空闲页，不能在采集运行时执行。只读SQL的 `report_files.raw_bytes` 在已迁移行中为空，读取原件须使用 `original_bytes` 或 `export`，云位置与校验状态在 `report_cloud_files`。

```powershell
python -B tools/setup_quark.py
python -B battle_reports.py cloud login
python -B battle_reports.py cloud init
python -B battle_reports.py cloud status
python -B battle_reports.py cloud sync --limit 10
```

已用本人授权的真实账号完成一份小样本上传、完整回读、清除缓存后的再次下载、原样导出与 SHA-256 一致性核验；这只证明该样本流程，不表示全部历史数据已经迁移。

首次初始化需在夸克官方工具实际支持的 Codex 环境中执行。安装器仅在当前环境确实为 `CODEX_SHELL=1` 时保存这个原值，供后续桌面子进程沿用已经授权的适配器；没有该环境或值不符时不会补造标记，也不保存 Codex 线程标识、用户主目录或令牌。已完成本项目桌面子进程清除父进程 Codex 环境后、仅凭初始化配置读取已授权账号的验收；这不代表夸克官方提供通用 standalone SDK。

配置示例见 `config/quark.example.json`。安装和缓存目录应使用有足够空间的工作盘。官方运行时的登录、会员及网络限制仍适用；本地离线测试不会被作为真实账号上传成功的证据。夸克官方工具说明见 [官方仓库](https://github.com/quark-clouddrive/quarkclouddrive_offical)。


## 安装

使用 Windows 和 Python 3.10 或更新版本。官方 Windows Python 通常包含 Tkinter；实时采集另需 `frida`、`psutil`，Excel 导出使用 `openpyxl`。基础采集和查询不需要 Node；可选的夸克云端功能另需 Node.js 16 或更新版本，不依赖作者的私人运行环境。

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

## 本地兼容模式的采集与查询

打开窗口不会自动连接游戏或开始采集。登录角色后，点击“开始采集”；游戏和工具需要使用匹配的权限。点击“停止采集”结束；本地兼容模式关闭窗口后后台采集仍会继续，需要停止时请先使用停止按钮；严格模式关闭窗口会请求停止并等待当前云端校验。再次打开会识别已有采集器并防止重复启动。

界面默认仅采集 PVP；需要 PVE 时先停止再修改选项。采集范围包括当前角色可见的个人、同盟和收藏战报及其子报。CLI 默认包含全部类型，需要 PVP 筛选时显式传 `--pvp-only`。

```powershell
python -B battle_reports.py collect --console --pvp-only
python -B battle_reports.py status
python -B battle_reports.py query "SELECT battle_uid,completeness,event_count FROM report_current_snapshots" --limit 20
python -B battle_reports.py export 1 .\output\exports\snapshot_1.json
```

每轮默认等待 60 秒；失败记录保留重试和续采状态。原始字节按命名空间和 SHA 去重，保存历史快照、未知字段及失败原因，不以较差快照替换完整事件。完整性指单封五组件检查，不表示整个账号所有战报已经采齐。

本地兼容模式要求数据库及采集输出所在磁盘默认至少保留 **5 GiB** 空间。启动、本地恢复、下一页、下一封和入库前会检查；不足时结束本次采集，保存已收到的原文和待入库检查点，释放空间后需要手动启动，不会自动恢复采集。CLI 可通过 `--min-free-gib` 调整正数阈值。查询与原文导出不受该采集阈值限制；保留阈值不能阻止其他程序同时占满磁盘，也不会自动删除历史数据。

## 本地兼容模式的文件与导出

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
