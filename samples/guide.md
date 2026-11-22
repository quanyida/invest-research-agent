# 安装引导：ifind-finance-data 金融数据查询 SKILL

> **你的任务**：按本文档的步骤，自动完成此 SKILL 技能包的下载、解压、配置和验证。
>
> **安装包下载地址**：
> ```
> {ZIP_URL}=https://s.thsi.cn/cd/ifind-java-ds-bff-web-container/ifind-mcp-web/skills/ifind-finance-data-1.5.1.zip
> ```
> **核心步骤**：确定 SKILL 目录 → 下载 ZIP → 解压（去版本号，目录名固定为 `ifind-finance-data`）→ 探测 Node/Python 环境 → 让用户提供密钥写入配置 → 运行自检脚本验证 → 报告结果。
>
> 严格按步骤执行。每步先执行再判断，不要跳过任何一步。若所有重试均失败，引导用户前往 iFinD MCP 官网使用指南（https://mcp.51ifind.com/#/docs/guide-prepare）寻求人工配置（注意：ZIP下载链接文字与真实版本无关，仅检查内容即可）。

---

## 执行原则（优先级最高，所有步骤均受此约束）

1. **不要默认推断安装范围**：不得根据当前工作目录或打开文件路径推断安装目录。只有用户明确指定路径或明确选择了范围才视为确认。像"安装""装一下""配置一下"这类表述不算确认。
2. **不要跳过自检**：文件下载完成不等于功能可用，必须完成第 6 步自检才能报告安装成功。
3. **不要用占位符密钥发请求**：`mcp_config.json` 中的 `"your ifind-mcp key"` 是占位符，拿到真实密钥之前不得发起 API 请求。
4. **不要直接运行 `node call-node.js` 或 `python call.py`**：这两个脚本是函数库，直接运行只打印提示，不代表成功或失败。验证必须通过第 6 步的临时自检脚本。
5. **路径加双引号**：含空格或中文的路径，命令中一律用双引号包裹。
6. **Windows PowerShell 注意编码**：含中文参数时先执行 `chcp 65001` 或 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`。
7. **每步先执行再判断**：不要凭推理假设条件满足，必须实际运行检测命令（如 `ls`、`dir`、`node --version`）获取真实结果后再进入下一步。

---

## 总览

解压后应得到的目录结构（`<SKILL_ROOT>/ifind-finance-data/` 下）：

```
SKILL.md                     ← 技能说明文档
mcp_config.json              ← 需写入用户密钥
call-node.js                 ← Node.js 调用脚本（推荐，零依赖）
call.py                      ← Python 调用脚本（备选，需 requests 库）
references/                  ← 各数据类别参考文档（8 个 .md 文件）
```

---

## 第 1 步：确定 SKILL 安装目录

**必须实际执行文件系统检测命令**，不要凭推理判断目录是否存在。

### 1.1 自动探测（按优先级逐条执行检测）

对下表中的每个候选目录，**实际执行检测命令**（如 `ls`、`dir`、`Test-Path`），命中的第一个真实存在的目录即作为 `SKILL_ROOT`。如果该目录属于当前智能体自身或通用目录，直接使用，无需询问用户。

| 智能体 | 候选 SKILL 根目录 | 检测命令示例 |
| :--- | :--- | :--- |
| Claude Code | `~/.claude/skills/` | `Test-Path "$env:USERPROFILE\.claude\skills"` 或 `ls -d ~/.claude/skills/` |
| Cursor | `~/.cursor/skills/` | `Test-Path "$env:USERPROFILE\.cursor\skills"` 或 `ls -d ~/.cursor/skills/` |
| Cline / Roo | `~/.cline/skills/` 或 `~/.roo/skills/` | 同上方式检测 |
| GitHub Copilot / Codex CLI | `~/.agents/skills/` | 同上方式检测 |
| Windsurf | `~/.windsurf/skills/` | 同上方式检测 |
| Trae | `~/.trae/skills/` | 同上方式检测 |
| 通用 | `~/.config/agents/skills/` | 同上方式检测 |

### 1.2 命中处理

- 命中的目录是**当前智能体自身**的默认目录，或通用目录（如 `~/.config/agents/skills/`）→ 直接使用，无需询问。
- 命中的目录**疑似其他智能体软件**的 SKILL 目录（如当前是 Cursor 但仅探测到 `~/.windsurf/skills/` 存在）→ 向用户确认："检测到以下可能适用的 SKILL 目录：`<列表>`，请问安装到哪个？"

### 1.3 均未命中

**不要自己盲目创建目录**。列出上表中的候选路径，询问用户希望使用哪个或自行指定。可推荐 `~/.config/agents/skills/` 作为通用选项。

### 1.4 目录不存在则创建

确定 `SKILL_ROOT` 后，若该目录不存在，创建它（`mkdir -p` / `New-Item -ItemType Directory -Force`）。

**成功判定**：已确定一个明确的 `SKILL_ROOT` 绝对路径，且该目录已存在（可以是刚创建的）。
**失败处理**：询问用户协同确定 SKILL 目录，不可自行假设。

### 1.5 已有同名技能
- 若用户SKILL目录下已存在命名为 "ifind-finance-data-x.x.x" 的SKILL目录，主动询问用户是否要用重新下载并覆盖旧版本 

---

## 第 2 步：下载 ZIP 安装包

下载到系统临时目录。

**Windows (PowerShell)**：
```powershell
$zip = "$env:TEMP\ifind-skill.zip"
Invoke-WebRequest -Uri "{ZIP_URL}" -OutFile $zip -UseBasicParsing
```

**macOS / Linux**：
```bash
zip_file="/tmp/ifind-skill.zip"
curl -fSL -o "$zip_file" "{ZIP_URL}"
```

**成功判定**：执行 `ls -la` 或 `dir` 确认文件大小 > 1KB。

**失败处理**：SSL/证书错 → curl 加 `-k`，PowerShell 加 `-UseBasicParsing`；404/超时/0字节 → 告知用户链接 `{ZIP_URL}` 可能失效，不要自行更换 URL；下载异常小 → 重试一次，仍失败则同上处理。

---

## 第 3 步：解压到 SKILL_ROOT

> **关键**：最终目录名必须是 `ifind-finance-data`（**不带版本号**），结构为 `<SKILL_ROOT>/ifind-finance-data/SKILL.md`。
>
> ZIP 内部可能有多层嵌套（如外层目录带版本号、或双层同名嵌套），下面的脚本已自动按 SKILL.md 位置纠偏，请严格复制执行。

复制以下代码块前，**先用实际路径替换 `$SKILL_ROOT` 变量**，然后执行。

**Windows (PowerShell)**：
```powershell
$SKILL_ROOT = "<替换为第1步确定的绝对路径>"
$zip = "$env:TEMP\ifind-skill.zip"
$tmpExtract = "$env:TEMP\ifind_extract"
Remove-Item -Recurse -Force $tmpExtract -ErrorAction SilentlyContinue
Expand-Archive -Path $zip -DestinationPath $tmpExtract -Force
$src = Get-ChildItem -Recurse -Filter "SKILL.md" $tmpExtract |
       Select-Object -First 1 -ExpandProperty DirectoryName
Copy-Item -Recurse -Force $src "$SKILL_ROOT\ifind-finance-data"
```

**macOS / Linux**：
```bash
SKILL_ROOT="<替换为第1步确定的绝对路径>"
zip_file="/tmp/ifind-skill.zip"
tmp_extract="/tmp/ifind_extract_$$"
rm -rf "$tmp_extract"; mkdir -p "$tmp_extract"
unzip -q "$zip_file" -d "$tmp_extract" 2>/dev/null || tar -xf "$zip_file" -C "$tmp_extract"
src=$(find "$tmp_extract" -name SKILL.md -print -quit | xargs dirname)
mkdir -p "$SKILL_ROOT"
cp -R "$src" "$SKILL_ROOT/ifind-finance-data"
```

**成功判定**：逐一确认以下文件存在：
- `$SKILL_ROOT/ifind-finance-data/SKILL.md`
- `$SKILL_ROOT/ifind-finance-data/call-node.js`
- `$SKILL_ROOT/ifind-finance-data/mcp_config.json`
- `$SKILL_ROOT/ifind-finance-data/call.py`
- `$SKILL_ROOT/ifind-finance-data/references/`（目录存在即可）

**失败处理**：目录名带版本号 → 重命名为 `ifind-finance-data`；多层嵌套 → 把包含 SKILL.md 的最内层目录内容平移到 `ifind-finance-data/`；Linux 缺 `unzip` → `apt-get install -y unzip` 或 `brew install unzip` 后重试；`Expand-Archive` 不可用 → 改用 `[System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmpExtract)`。

---

## 第 4 步：探测运行环境

Node.js 或 Python，满足其一即可（推荐 Node.js）。

执行以下检测命令，**必须实际运行**：

```bash
node --version
# 或
python --version      # Windows 通常指向 Python 3
python3 --version     # macOS / Linux
python -c "import requests"  2>&1
python3 -c "import requests" 2>&1
```

环境优先级：Node.js >= 14 首选（零依赖）；Python 3 + requests 已安装可用；Python 3 缺 requests → `pip install requests` 后可用。

**成功判定**：至少 1 个环境可用。记录实际可用的版本。

**失败处理**：两个都没有 → 提示安装 Node.js LTS（https://nodejs.org/）；requests 安装失败 → 建议切换 Node.js。

---

## 第 5 步：配置密钥

必须有真实密钥，不可用占位符。

1. 提示用户："请到同花顺 MCP 官网 → 个人中心 → 密钥管理（https://mcp.51ifind.com）获取 API 密钥，然后直接把密钥发给我。"

2. 收到密钥后先清洗：去除首尾空白字符。

3. 构造 JSON 写入（**不要用字符串拼接**）：读取 `mcp_config.json` → 写入 `{"auth_token": "<清洗后的密钥>"}` → 用 `JSON.stringify` / `json.dump` 序列化。

4. 写后验证：读回确认 `auth_token` 不等于占位符、不为空、不含首尾空格。

5. 完成后对用户说"密钥已安全写入配置文件"，**不要**展示完整密钥。

**成功判定**：`auth_token` 不是占位符且非空，JSON 合法。

**失败处理**：密钥明显不合法（如单字符）→ 提示确认完整复制；用户无密钥 → 引导注册 https://mcp.51ifind.com；JSON 写入报错或读回异常 → 重新"读取 → 修改对象 → 序列化 → 写入"流程。

---

## 第 6 步：可用性自检

> `call-node.js` 和 `call.py` 是函数库，直接运行只打印提示，必须通过临时自检脚本验证。

### 6.1 路径准备

复制脚本前，**把 `<SKILL_DIR>` 替换为 `ifind-finance-data` 目录的绝对路径**。Windows 反斜杠路径需用 `\\` 或 `/`。

### 6.2 执行自检

优先 Node.js，不可用则 Python。

**Node.js** — 写入 `$TEMP/__skill_selfcheck.js`，执行 `node "$env:TEMP\__skill_selfcheck.js"`（Windows）或 `node "$TEMP/__skill_selfcheck.js"`（macOS/Linux）：

```javascript
try {
  const { listTools } = require("<SKILL_DIR>/call-node.js");
  (async () => {
    try {
      const r = await listTools("stock");
      console.log("SELFCHECK_RESULT:", JSON.stringify(r).slice(0, 500));
    } catch (e) {
      console.log("SELFCHECK_ERROR:", e.message);
    }
  })();
} catch (e) {
  console.log("SELFCHECK_ERROR (require):", e.message);
}
```

**Python** — 写入 `$TEMP/__skill_selfcheck.py`，执行 `python "$env:TEMP\__skill_selfcheck.py"` 或 `python3 "$TEMP/__skill_selfcheck.py"`：

```python
import sys, json
sys.path.insert(0, r"<SKILL_DIR>")
try:
    from call import list_tools
    r = list_tools("stock")
    print("SELFCHECK_RESULT:", json.dumps(r, ensure_ascii=False, default=str)[:500])
except Exception as e:
    print("SELFCHECK_ERROR:", str(e))
```

### 6.3 结果判定

**成功**：输出含 `SELFCHECK_RESULT:` 且后续为有效 JSON（即使部分工具无权限，通信成功即通过）。

**失败参照**：

| 输出 | 处理 |
| :--- | :--- |
| `SELFCHECK_ERROR (require):` | 检查 SKILL_DIR 路径和反斜杠转义 |
| `401` / `auth` 相关 | 回第 5 步，确认密钥完整无多余字符 |
| `403` / 权限不足 | 安装已成功，告知用户当前权益受限 |
| `404` / 网络错误 / 超时 | 安装已成功（404 属服务端），网络错误则检查 `api-mcp.51ifind.com:8643` 可达性 |
| `Cannot find module` / `ImportError` | 检查路径和文件名大小写 |
| `No module named 'requests'` | `pip install requests` 后重试 |
| 输出"请按说明发起调用" | 直接执行了脚本入口而非自检脚本，重新按本节方式创建 |

### 6.4 收尾

自检完成后删除临时脚本文件（`__skill_selfcheck.js` / `__skill_selfcheck.py`）。

---

## 第 7 步：最终报告与引导

1. **输出安装摘要**，格式如下：

   ```
   === ifind-finance-data SKILL 安装报告 ===
   安装目录：<SKILL_ROOT>/ifind-finance-data
   运行环境：Node vX.X.X / Python 3.X.X
   自检结果：通过 / 未通过
   自检方法：list_tools("stock")
   密钥状态：已配置（脱敏显示为 ***）
   技能状态：当前已可用 / 当前不可用（原因）
   ```

2. **提醒用户**：可能需要**重启当前智能体或重新加载 Skills**，宿主才能识别到新安装的 Skill。如果安装后看不到技能，请先尝试重启。

3. **主动提供试用引导**："SKILL 已就绪，想现在试一个金融数据查询吗？比如查最新行情：'帮我查一下贵州茅台（600519）的最新行情'。"

---

## 附录 A：SKILL 目录不匹配时的特殊情况处理

### 如果目标目录已存在旧版本

先询问用户："检测到目录 `<SKILL_ROOT>/ifind-finance-data` 已存在，是否覆盖安装？（选择"是"将删除旧版本后安装新版本）"
- 用户确认后，删除旧目录，继续安装流程
- 用户拒绝后，询问是否需要安装到其他目录

### 如果 `mcp_config.json` 已有有效密钥

安装过程发现已有有效密钥时，**保留现有密钥**，不要覆盖。仅当密钥为空或仍为占位符时才进入第 5 步配置流程。

---

## 附录 B：常见 LLM 终端操作注意事项

1. **占位符替换**：本文档中所有 `<SKILL_DIR>`、`<SKILL_ROOT>`、`{ZIP_URL}` 等占位符，必须在执行前替换为实际值。其中 `{ZIP_URL}` 的值为文档顶部声明的下载地址。
2. **不得篡改 URL**：下载地址即为文档顶部声明的 `{ZIP_URL}`，不得自行修改域名或路径。
3. **命令分步执行**：每一步的命令都应该在独立终端中执行，确认成功后再进入下一步。不要把所有命令拼成一个大脚本。
4. **实际读取输出**：不要假设命令成功，必须实际读取命令输出并对照「成功判定」逐项检查。
5. **如果你无法执行终端命令**（如你是 Web 聊天界面而非 IDE 内置智能体），请直接告诉用户："我无法在您的环境中执行终端命令。请将此安装引导文档发送给您的 IDE 内置 AI 助手（如 Cursor、Claude Code、Cline 等），它们可以自动完成安装。"
