# 依赖管理

四层结构，改对入口再编译，不要手改锁文件：

| 文件 | 用途 |
|---|---|
| `requirements.in` | 直接依赖（代码 import + 运行时动态加载的方言/multipart） |
| `requirements-dev.in` | 测试/开发依赖，文件头 `-r requirements.in` |
| `requirements.lock` | 编译产物，`==` 精确版本，Python 3.10 通用锁 |
| `requirements-dev.lock` | 同上，含测试依赖 |

## 新增依赖

1. 把包加进 `requirements.in`（import 名与发行版名不一致时，同步更新
   `tests/test_requirements_declared.py` 里的映射表）；
2. 重新编译：`uv pip compile requirements.in -o requirements.lock --universal --python-version 3.10`
   （dev 同理）；带 `-c <现状freeze>` 约束可把版本钉在当前环境；
3. 提交 `.in` 和 `.lock` 两个文件。

## 安装

- 运行：`pip install -r backend/requirements.txt`（薄入口，指向 lock，CI/打包无需改动）；
- 开发/测试：再装 `pip install -r backend/requirements-dev.lock`。

## 为什么分层

`>=` 宽约束会让不同时间安装得到不同版本（langchain/pydantic 大版本升级常不兼容）；
锁文件保证今天和三个月后装出同一份环境。`requirements.in` 让人审「用了什么」，
lock 让机器保证「装了哪个版本」。
