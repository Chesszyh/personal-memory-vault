# 贡献指南

从 [README](README.md#安装与快速开始) 安装开发环境。Python 核心没有第三方运行依赖；PDF 和语义检索依赖按需安装。Node.js 用于浏览器恢复扩展的合成测试。

## 检查

```bash
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -v
node --test archive_extension/test/*.test.mjs
git diff --check
python -m pip install build
python -m build
```

测试使用合成数据，不需要个人 vault 或模型认证。语义单元测试使用替身模型；实际模型效果按 [评测说明](docs/recall-evaluation.md) 验证。扩展的浏览器集成测试及额外依赖见 [扩展说明](archive_extension/README.md#纯合成浏览器冒烟)。

Python 包从 `src/personal_vault/` 构建；阅读器第三方资源与许可证必须同时包含在 wheel 和源码包中。Pi 集成和工作台脚本通过 Git checkout 使用。

修改导入、检索或存储行为时，为可复现的问题补充合成输入，并更新对应文档。保留原始来源与派生记录的区别。提交说明应描述最终行为及相关测试结果。

## 提交问题与改进

在 [Issues](https://github.com/Chesszyh/personal-memory-vault/issues) 提供运行命令、版本、错误信息和最小合成输入。功能提案说明使用场景和期望结果。贡献遵循项目 [MIT 许可证](LICENSE)。
