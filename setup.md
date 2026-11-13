# setup.py 用法用例

## 前置条件

项目要求 Python 版本满足：

```text
>=3.11,<3.12
```

## 在线安装依赖

使用默认清华镜像源安装 `requirements.txt` 中的依赖：

```bash
python setup.py install_deps
```

显式指定清华镜像源：

```bash
python setup.py install_deps --mirror https://pypi.tuna.tsinghua.edu.cn/simple
```

指定其他镜像源：

```bash
python setup.py install_deps --mirror https://mirrors.aliyun.com/pypi/simple/
```

## 离线安装依赖

假设本地 wheel 目录为 `./wheels`：

```bash
python setup.py install_deps --offline-dir ./wheels
```

使用绝对路径：

```bash
python setup.py install_deps --offline-dir D:\wheelhouse
```

## 指定依赖清单

默认读取根目录下的 `requirements.txt`。也可以临时指定其他依赖清单：

```bash
python setup.py install_deps --requirements requirements.txt
```

## 查看命令帮助

查看 `install_deps` 支持的参数：

```bash
python setup.py install_deps --help
```

查看项目包名和版本：

```bash
python setup.py --name
python setup.py --version
```

## 冲突限制

`--offline-dir` 和 `--mirror` 不能同时使用：

```bash
python setup.py install_deps --offline-dir ./wheels --mirror https://pypi.tuna.tsinghua.edu.cn/simple
```

上面的命令会被拒绝并提示二者只能选择一个。

## 打包成 exe

构建一个合并的 `--onedir` 目录包和一个微信冷启动器：

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Target All
```

只构建合并包：

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Target Bundle
```

只构建微信启动器：

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Target Start
```

如果已经安装过构建依赖，可以跳过安装步骤：

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Target All -SkipDependencyInstall
```

构建产物位于：

```text
dist\HandWeChat\HandWeChatGui.exe
dist\HandWeChat\HandWeChatApi.exe
dist\HandWeChatStart.exe
```

GUI 和 API 共用 `dist\HandWeChat\data\` 保存配置和日志，也共用同一个 `ocr_calibration.json`。离线 OCR 模型位于 `dist\HandWeChat\_internal\ocr_models\`，并会随程序一起打包。

在新电脑上，必须先运行 `dist\HandWeChatStart.exe` 冷启动微信。它会开启系统读屏标志并自动定位微信安装路径。若微信已经在运行，它不会自动重启微信；此时需要从托盘彻底退出微信，再重新运行启动器。登录完成后再启动并附加 `HandWeChatGui.exe` 或调用 `HandWeChatApi.exe`。

## 纯 OCR 通道

OCR 通道不依赖 UIA 控件树，只读取微信当前打开的聊天，支持接收文本和发送文本。消息读取使用三段流水线：

```text
截图线程 -> 截图队列(maxsize=8) -> PaddleOCR 线程 -> 解析队列(maxsize=8) -> 消息差异线程
```

截图完成不会立刻执行 OCR；OCR 慢时会通过有界队列形成背压，避免解析压力打满后继续无节制占用内存。

在 `data\api_config.json` 中配置 OCR 轮询和限速：

```json
{
  "ocr_poll_interval": 2.0,
  "ocr_max_send_per_minute": 20
}
```

启动 API 后先启动 OCR 监听：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/listener/start
```

查询健康状态：

```bash
curl http://127.0.0.1:8000/api/v1/health
```

读取 OCR 新消息：

```bash
curl http://127.0.0.1:8000/api/v1/messages?cursor=0^&limit=100^&timeout=10
```

发送文本消息：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/send `
  -H "Content-Type: application/json" `
  -d '{"text":"OCR 通道测试"}'
```

查询发送结果：

```bash
curl http://127.0.0.1:8000/api/v1/send/send_20260112_000001
```

停止 OCR 通道：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/listener/stop
```

OCR 消息复用全局 `MessageBuffer` 的自增 `cursor`。`/api/v1/health` 会返回队列大小和截图去重状态：`last_frame_signature` 是最新截图签名，`last_accepted_signature` 是最近一次成功 OCR 的签名，`ocr_skip_identical_count` 表示已跳过的相同内容帧数。OCR 签名只覆盖标题区和聊天区；窗口移动或输入框变化不会触发 OCR。首次启动时 PaddleOCR 需要加载 `ocr_models` 离线模型，通常需要等待十几秒。
