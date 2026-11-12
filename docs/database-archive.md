# 战报数据库归档

完整数据库以独立压缩归档发布，源码仓库保留数据库结构、下载清单和恢复工具。直接克隆仓库不会下载上百 GB 的 SQLite 文件。

本批数据为 2026 年 11 月 13 日停止采集后的数据库快照。源数据库约 113.97 GB，另有约 242 MB 的 WAL；包含 2,213 封逻辑战报和 18,787 个保存快照。快照保留中间响应和补全过程，不能将快照数当作独立战报数。真实归档包含游戏内昵称、角色 ID、联盟及战斗记录，记录的是历史观察。

## 下载和核对

下载入口、实际压缩大小、SHA-256、原始 DB/WAL 校验值及生成时间记录在 `data/releases/` 的发布清单中。Google Drive 提供完整单文件，GitHub Release 提供不超过 1 GiB 的分卷。

从 GitHub 下载时，将同一批次的所有 `.part001` 等分卷放入 `downloads`，再用清单逐片验证并合并：

```powershell
python -B tools/join_database_parts.py data/releases/2026-11-13.json --parts-dir .\downloads --output .\downloads\battle_reports_20261113.tar.zst
```

合并需要额外一份压缩归档的空间。脚本保留下载分卷并拒绝覆盖；完成后可自行移除已合并的下载分卷，避免重复占用。不要混用不同批次的文件。如文件系统不支持硬链接，脚本会保留已验证的 `.partial` 文件，可核对 SHA-256 后手动改名。

Windows PowerShell 可使用以下命令计算文件 SHA-256：

```powershell
Get-FileHash .\downloads\battle_reports_20261113.tar.zst -Algorithm SHA256
```

## 校验与恢复

安装恢复工具依赖：

```powershell
python -m pip install -r requirements-data.txt
```

只校验归档，不解压数据库：

```powershell
python -B tools/restore_battle_database.py check .\downloads\battle_reports_20261113.tar.zst
```

恢复到一个尚不存在的目录：

```powershell
python -B tools/restore_battle_database.py restore .\downloads\battle_reports_20261113.tar.zst .\restored-database
```

完整恢复至少需要约 114.22 GB 加 256 MiB 的可用空间；压缩文件仍会保留，因此还要为下载归档预留空间。脚本先流式验证全部成员和 SHA-256，再检查容量并恢复，拒绝覆盖现有目录、路径越界、符号链接及未知成员。校验会解码整个大库，耗时取决于 CPU 和磁盘速度。

恢复结果必须同时包含：

```text
battle_reports.db
battle_reports.db-wal
```

两者属于同一份快照。不要丢弃 WAL、把它与其他数据库配对，或使用忽略 WAL 的只读方式判断数据库是否损坏。`-shm` 是运行时文件，归档不包含它，SQLite 会在需要时重建。

## 在软件中打开

停止正在运行的采集并关闭使用该库的窗口。保留现有数据库后，将恢复得到的 DB 和同名 WAL 一起放入项目的 `data/database/`，再打开界面。仅查看历史数据不需要连接游戏；恢复归档不会自动启动采集。

如需保留现有工作库，可从 CLI 显式指定恢复目录中的数据库，而不替换默认文件：

```powershell
python -B battle_reports.py status --db .\restored-database\battle_reports.db
```

原始响应、历史版本、未知字段和中文解释均保留在归档里。归档完整性不等同于“账号的全部战报都已采齐”，也不表示历史阵容是当前出战阵容。

## 本批次的验证范围

归档时同时禁止 DB 与 WAL 被写入或删除；归档完成后，完整流式解压读取了约 114.21 GB 的内容，并逐个核对原文件大小、SHA-256、成员清单和压缩帧校验。归档文件与分卷另有 MD5、SHA-256，供下载后验证。这些检查证明备份与当时的源文件字节一致。

另外执行的一次只读 SQLite `quick_check(1)` 在 600 秒上限内未完成，已按预定上限取消；它没有返回 `ok`，也没有返回损坏诊断。因此本批次不宣称全库结构检查通过。检查前后 DB 与 WAL 的大小、修改时间和文件身份一致。
