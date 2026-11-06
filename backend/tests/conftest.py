"""后端测试环境隔离。

生产环境默认禁用 Mock Provider；测试套件显式开启它，确保回归测试不会读取
开发者本机 .env、访问真实模型接口或消耗 token。环境变量必须在测试模块导入
应用配置前设置。

另外统一在用例之间重置限流器：限流计数是进程级全局状态，而测试里所有
TestClient 请求都来自同一个客户端地址，不重置的话同一个文件里跑几十个用例就会
把认证端点的 IP 配额（默认 20 次/分钟）耗尽，表现为后面与限流无关的用例 429 失败。
"""

import os

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("CHECKPOINTER_BACKEND", "memory")

import pytest

os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["ALLOW_MOCK_PROVIDER"] = "true"


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """每个用例前后清空认证相关限流窗口。

    延迟导入：限流器模块会 import config.settings，必须在本文件的 os.environ
    设置之后加载（pytest 导入 conftest 时就执行模块级代码，这里放到 fixture
    内部可以确保顺序）。
    """
    from api.rate_limiter import auth_ip_limiter, auth_limiter

    auth_limiter.reset()
    auth_ip_limiter.reset()
    yield
    auth_limiter.reset()
    auth_ip_limiter.reset()
