# Terminal UI 与交互设计契约

协议版本：1  
状态：已锁定并随 `core/terminal_ui.py`、`core/execution_context.py` 与 `core/report_viewer.py` 实现。

## 1. 场景分类与视觉原则（Quiet Engineering Console）

- **场景类型**：`Operate` 高信噪比工程控制台，兼容 macOS Terminal、iTerm2、tmux 与非交互 CI/管道。
- **继承终端底色**：不硬编码背景色；严格尊重 `--format text|json|tui`、`NO_COLOR`、`TERM=dumb|unknown` 以及 `isatty()` 状态。
- **单一主强调色 + 语义色体系**：

| 语义角色 | ANSI SGR | 视觉表现 | 适用位置 |
| --- | --- | --- | --- |
| `title` / `heading` | `\x1b[1;36m` | 青色粗体 | 命令页头、分节标题 |
| `cyan` / `pending` / `cmd` | `\x1b[36m` | 青色 | `→ 待同步`/`→ 待发布`、建议下一步可复制命令 |
| `success` | `\x1b[32m` | 绿色 | `✓ 已同步`/`✓ 已对齐`/`✓ 通过`/`✓ 已发布` |
| `warning` | `\x1b[33m` | 黄色 | `! 有冲突`/`! 有本地改动`/`! 需注意` |
| `error` | `\x1b[1;31m` | 红色粗体 | `✗ 已阻断`/`✗ 失败`/`✗ 校验失败` |
| `muted` | `\x1b[2m` | 弱化灰 | `·` 次要路径、Machine/Profile 路径、Plan ID、已跳过提示 |

## 2. 四层核心原语（零外部 TUI 依赖）

1. **`core/terminal_ui.py`（统一组件与宽度对齐）**
   - 所有命令共用 `style()`、`badge()`、`heading()`、`section()`、`meta_line()`、`next_steps()`、`display_width()`、`pad_display()`、`truncate_display()`。
   - 使用 `unicodedata.east_asian_width` 计算东亚宽字符列宽（中文 2 列、ASCII 1 列），忽略 ANSI 转义后做对齐填充。
   - **静态契约守卫**：除 `core/terminal_ui.py`、`core/reporting.py`、`core/report_viewer.py` 外，业务命令文件禁止硬编码 `\033[` 或 `\x1b[`。
2. **`core/execution_context.py`（`stderr` 单行原地进度与交互静音锁）**
   - 当 `stderr.isatty()` 为真且终端非 `dumb/unknown` 时，进度消息通过 `\r\033[2K` 在**同一行原地刷新**并按终端宽度截断；阶段清零（`progress(None)`）或退出上下文时自动擦除当前进度行。
   - 非 TTY（文件、管道、单元测试 `StringIO`）保留普通带时间戳的单行日志，并对千级文件计步做节流。
   - **唯一交互入口 `prompt_input()` / `pause_progress()`**：所有等待用户输入前强制暂停并擦除进度/心跳，禁止其他模块直接裸调 `input()`，防止后台心跳线程冲乱用户交互行。
3. **`core/reporting.py`（结构化报告静态渲染）**
   - 与 `core/terminal_ui.py` 共享相同 SGR 色板，`--format json` 的 stdout 只输出合法 JSON，进度/提示全部走 `stderr`（或非 JSON 模式末尾）。
4. **`core/report_viewer.py`（`curses` 备选屏幕树形查看器）**
   - `--format tui` 与 `claude-config report` 共用同一套语义色（青粗体标题/分组、绿/黄/红状态、反显选中行），退出后保留一行摘要，不修改业务配置。
