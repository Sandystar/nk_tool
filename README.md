# NKTool

## 版本支持
目前支持到最新版：v3.33.0

## 目录

| 路径 | 内容 |
|---|---|
| `license_server/` | 许可证服务
| `python_tool/` | Python 工具 + 配置

```text
Project
├── 1-download-samples.bat        下载最新版样本 + 镜像
├── 2-build-license-server.bat    构建许可证服务器
├── 3-patch-samples.bat           对本地样本打补丁
├── 4-patch-docker.bat            对远程Docker容器打补丁
├── license_server\
│   ├── main.go                   许可证服务源码（标准库，无第三方依赖）
│   ├── key_pkcs8.pem             私钥，go:embed 进二进制
│   ├── spki.der                  对应公钥，补丁时写进客户端
│   ├── key.json                  密钥指纹
│   ├── build.py                  交叉编译 6 个平台 → bin/
│   └── bin\<平台>\nplicense[.exe]（构建产物）
├── python_tool\
│   ├── download_samples.py       下载样本 + GHCR 镜像
│   ├── patch_samples.py          补丁工具
│   ├── patch_docker.py           补丁工具
│   ├── activate_local.py         激活工具
│   ├── patch_samples_config.json 配置
│   └── patch_docker_config.json  配置
```

---

## 一、环境

- Python 3.10+
- Go 1.21+（**只在**构建 `nplicense` 时用）
- `paramiko`（远程 Docker 补丁需要）

```powershell
python -m pip install -r requirements.txt
```

---

## 二、工作流

```text
1-download-samples.bat     →  asset/samples/<版本>/
2-build-license-server.bat →  license_server/bin/<平台>/   （只需一次）
3-patch-samples.bat        →  asset/patch/<版本>/
4-patch-docker.bat         →  远程容器内部落地
```

### 1. 下载样本

```powershell
python .\python_tool\download_samples.py --dest asset/samples
```

每次拉取**最新** Release 的 6 个平台 + `ghcr.io/vscodev/neokikoeru:latest` 的两个架构。


### 2. 构建许可证服务（首次必做）

```powershell
python .\license_server\build.py                # 6 个平台
python .\license_server\build.py linux-amd64    # 只构建一个
python .\license_server\build.py --clean        # 清空 bin/
```

### 3. 本地补丁

```powershell
python .\python_tool\patch_samples.py --samples-dir asset\samples --out asset\patch
python .\python_tool\patch_samples.py --samples-file ...\neokikoeru.exe --out D:\out
python .\python_tool\patch_samples.py --samples-dir asset\samples --out D:\out --report-only
```

参数只有 `--out`（必填）、`--samples-file` / `--samples-dir`（二选一）、`--report-only`（仅做分析，不打补丁）。

**首次启动后必须激活**（二选一）：
- 网页 → 系统 / 许可证 → 密钥 `Hello-World`。
-  `python .\python_tool\activate_local.py`。

#### `patch_samples_config.json`

| 键 | 默认 | 作用 |
|---|---|---|
| `license_port` | `18443` | 写进客户端的许可证端点端口，也是 `nplicense` 的监听端口 |
| `client_port` | `5233` | 默认客户端端口（只影响启动脚本与提示） |
| `license_key` | `Hello-World` | 激活密钥 |
| `license_server_dir` | `../license_server` | 许可证服务目录（相对配置文件所在目录） |
| `output_suffix` | `_patched` | 产物后缀 |
| `copy_license_server` | `true` | 是否把 `nplicense` 复制到产物目录 |
| `emit_start_script` | `true` | 是否生成启动脚本 |
| `start_script_name` | `start_neokikoeru` | 启动脚本名（不含扩展名） |
| `license_server_log` | `nplicense.log` | 服务日志文件名 |
| `client_args` | `["serve"]` | 传给客户端的参数 |
| `work_dir` | `.work` | 临时目录，**相对当前执行目录**，默认就是执行处的 `.work\` |

### 4. 远程 Docker 补丁

```powershell
python .\python_tool\patch_docker.py            # 读配置，走完整流程
python .\python_tool\patch_docker.py --recon    # 只读体检
python .\python_tool\patch_docker.py --verify   # 打印 /api/sysinfo
python .\python_tool\patch_docker.py --restore  # 回滚到原始客户端
```

命令行参数全部可选，都会覆盖 `patch_docker_config.json` 里的同名项。

#### `patch_docker_config.json`

| 键 | 说明 |
|---|---|
| `host` | NAS / Docker 主机地址 |
| `ssh_port` / `ssh_user` / `ssh_password` / `ssh_key` | SSH 连接方式 |
| `app_port` | 客户端 HTTP 端口（`0` = 用 `patch_samples_config.json` 的 `client_port`） |
| `container` | 容器名，留空则按 `match` 匹配镜像/容器名 |
| `match` | 匹配串，默认 `neokikoeru` |
| `admin_user` / `admin_password` | 可选；留空则优先用容器内 `config.json` 的 `jwt_secret` 直接签管理员 token |
| `reset_password` | 登录失败时是否重置管理员密码 |
| `auto_activate` | 打完补丁是否自动激活 |
| `verbose` | 打印 SSH 细节 |
| `license_port` / `license_key` / `license_server_dir` / `work_dir` | 留空则继承 `patch_samples_config.json` |

容器内落点：

```text
/opt/neokikoeru-native/neokikoeru.orig   原始备份（只在首次落盘，不覆盖）
/opt/neokikoeru-native/neokikoeru.bin    补丁后的客户端
/opt/neokikoeru-native/nplicense         许可证服务
/opt/neokikoeru-native/nkentry.sh        入口包装脚本
/opt/neokikoeru-native/nplicense.log     许可证服务日志
/usr/local/bin/neokikoeru                包装脚本（原来是 ELF）
```

#### 工作目录

补丁过程会把拉回来的原始客户端、补丁后的客户端、待上传的载荷放到
**当前执行目录下的 `.work\`**（可通过 `work_dir` 改）：

```text
<当前目录>\.work\docker\192_168_0_101\
├── neokikoeru.pulled          容器里取回的原始客户端
├── neokikoeru.patched         补丁后的客户端
├── neokikoeru.remote.orig     原始备份（回滚用）
└── _up\                       待写进容器的三个文件
```

---
