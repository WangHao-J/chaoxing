# 学习工作台 · Windows 桌面版

基于 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 的桌面扩展与维护分支，由 [WangHao-J](https://github.com/WangHao-J) 维护。本 Fork 保留上游命令行项目和历史，在 `desktop/` 中提供多账号工作台。

> 桌面维护版本：**1.7.7 / full-rounds-20261030**。桌面所内置的上游代码基线为 `45d2106b16764231c2de2038fb042e71b23d878f`；本分支没有承诺自动兼容上游之后的所有变更。

## 桌面版增加了什么

- 一个可见工作台管理多个账号，每个账号使用独立后台、会话和资料目录。
- 登录成功后自动读取课程；账号分类、搜索和模式选择。
- 统一 API 设置，Windows 用户级加密保存密钥。
- 运行记录、可关闭提示、分账号待处理与运行报告。
- 媒体时长估计显示读取覆盖范围，未完整读取时提供已知下限。
- 失败章节保留原因，继续其他可执行章节；首轮结束后最多重跑三轮，共最多四轮。
- 已确认完成的任务不重复执行；提交结果不确定时保留核对记录。
- 全量离线升级：覆盖前备份、账号和配置保留、失败恢复及程序回退。

## 从源码运行

推荐 Windows 10/11 x64、Python 3.13，需支持 Tkinter。安装依赖后运行：

```powershell
cd desktop
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe account_manager.py
```

运行时会在 `desktop/` 下生成账号、设置和报告。它们已经加入忽略规则，请勿将个人配置复制进公开仓库。

## 安装包与升级

已发布 [1.7.7 全量升级包](https://github.com/WangHao-J/chaoxing/releases/tag/desktop-1.7.7-2)，含 Windows x64 运行环境与 SHA-256 校验文件。该包通过 579 项离线测试，以及安装器升级、资料保留、运行环境导入和回退检查。后续下载见 [Releases](https://github.com/WangHao-J/chaoxing/releases)。

已有本项目旧版本时，选择**原程序目录**升级，先正常退出工作台，不要先卸载。全量包包含运行环境，升级前备份并校验；账号、Cookie、配置、待处理和历史报告保留。需要回退时再次运行同一个全量包并选择恢复程序。

详见 [使用说明](desktop/使用说明.md)、[构建与发布](docs/BUILDING.md)、[验证范围](docs/VALIDATION.md)。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `desktop/` | 桌面程序、空白配置和离线测试 |
| `desktop/app/` | 桌面版使用的上游代码及适配 |
| `scripts/` | 可移植构建与发布前检查 |
| `packaging/` | 全量升级安装器模板 |
| `docs/` | 设计、构建、验证和依赖许可说明 |
| 根目录 `main.py`、`api/` 等 | 保留的上游命令行项目 |

上游使用方式见 [原始 README](docs/UPSTREAM_README.md)。

## 验证与限制

发布整理前，桌面维护分支已有 571 项离线回归通过；全量升级另有 8 项保护测试及 1.0.0 程序目录升级、启动、回退实测。仓库整理后的测试结果以 `docs/VALIDATION.md` 和 GitHub Actions 为准，不将历史结果冒充新一次 CI 成绩。

平台页面、登录和访问限制可能变化。离线测试通过不等于每门真实课程都可完成；课程结果以平台确认状态为准。AI 输出需要判断和核对，程序不保证答案正确或课程通过。请仅在获得授权的账号和符合课程要求的场景中使用。

## 贡献与来源

本项目包含 AI 辅助开发。功能设计、代码适配和测试应以仓库实际提交为依据；欢迎提交可复现问题和不含隐私的测试案例。新增桌面功能不代表全部平台访问能力由本分支原创。

本项目及其衍生修改按 **GPL-3.0** 发布，保留上游版权和许可声明。打包的第三方依赖分别遵循自身许可证，见 [第三方说明](docs/THIRD_PARTY_NOTICES.md)。

