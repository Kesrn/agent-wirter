"""后端测试环境隔离。

生产环境默认禁用 Mock Provider；测试套件显式开启它，确保回归测试不会读取
开发者本机 .env、访问真实模型接口或消耗 token。环境变量必须在测试模块导入
应用配置前设置。
"""

import os

os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["ALLOW_MOCK_PROVIDER"] = "true"
