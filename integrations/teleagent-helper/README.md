# TeleAgent Helper

TeleAgent Helper 是一套 Windows 工具，用于管理 TeleAgent 桌面端在本地的数据目录。

TeleAgent 把数据按「账号」组织在 `users/` 和 `Partitions/` 下，用
`current-owner.json` 记录当前激活账号。本工具可以：

- 轻量切换账号（只改一个几字节的配置文件，不复制数据）
- 把账号归入「会话」，一套会话 = 一份完整、可独立使用的数据，内含多个账号
- 把旧版本、旧结构的数据目录接入 / 转换为账号
- 对整套数据做存档（备份 / 恢复 / 新建空白会话）

## 模型

```
会话（TeleAgent-<名字>）== 整套数据目录
  ├── users/v1_public_<userId>/           一个账号的数据
  ├── Partitions/owner%3Av1_public_<id>/  该账号的 Web 登录分区
  └── current-owner.json                  当前激活账号指针
```

- **账号**：一份账号数据，必须存在于某个会话里，可给它起便于识别的名称。
- **会话**：账号的载体，也是整套数据快照。一个会话可含多个账号。
- **接入 / 转换旧数据**：把旧的会话目录零复制关联进当前会话（新版格式），
  或复制构建为账号（旧版格式），旧目录始终保留不动。
- 带对勾标记的会话 / 账号就是正在使用的，只有当前会话里的账号可以操作。

## 文件

| 文件 | 说明 |
| --- | --- |
| `teleagent-session-gui.py` | 图形界面，主程序（PyQt6） |
| `teleagent-session.py` | 命令行：整目录会话管理器（保存 / 切换 / 新建） |
| `teleagent_accounts.py` | 核心：账号、会话、接入、转换、删除等逻辑 |
| `dist\TeleAgentHelper.exe` | 打包好的单文件图形程序（Windows 10 11 x64） |

> `teleagent_accounts.py` 的 `SHARE` 指向程序数据的根目录。默认取
> 环境变量 `SESSION_TOOL_SHARE`，未设置时使用用户目录 `.local/share`。

## 图形界面（GUI）

左侧列会话，右侧显示所选会话里的账号：

- **设为当前**：切换会话 / 账号（切换账号需重启 TeleAgent 生效）
- **保存**：把当前正在使用的数据复制为一份新会话并存档
- **新建**：新建空白会话 / 新建账号后重启登录
- **删除**：删除会话（需输入会话名二次确认）；删除账号（接入账号仅解除关联，
  正式账号永久删除，当前使用的账号不可删）
- **转为正式**：把接入账号数据复制进来，解除对原会话的依赖
- **接入 / 转换旧数据…**：把旧的数据目录接入或转换为当前会话的账号

切换、新建、删除等操作前，工具会自动退出正在运行的 TeleAgent，防止数据被回写覆盖。

## 命令行（CLI）

```
python teleagent-session.py        # 会话：切换 / 保存 / 新建 / 列表
python teleagent_accounts.py       # 账号：切换 / 新建 / 列表 / 接入 / 转换
```

## 从源码运行

需要 Python 3.10+ 与 PyQt6：

```powershell
pip install PyQt6
python teleagent-session-gui.py
```

## 打包为 exe

```powershell
python -m PyInstaller --noconfirm --onefile --windowed --name TeleAgentHelper teleagent-session-gui.py
```

产物在 `dist\TeleAgentHelper.exe`。

> 打包时如需限定数据根目录，可在运行 / 打包前设置环境变量 `SESSION_TOOL_SHARE`。

## 安全说明

- 删除为强操作：删除会话 / 正式账号会永久清除数据，务必先备份（保存会话）。
- 接入账号的数据始终留在原会话目录，删除「接入账号」只解除关联。
- 请勿在 TeleAgent 运行过程中手动修改 `current-owner.json`。