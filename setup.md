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
