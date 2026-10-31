# 构建和发布

## 测试

Windows / Python 3.13，先安装 `desktop/requirements.txt`：

```powershell
cd desktop
python -B -m unittest discover -q
```

测试使用合成账号和模拟响应，不执行真实课程或真实交卷。Tkinter 测试需要可用的 Windows 桌面会话。

## 构建全量升级包

需要 Inno Setup 6.7+ 或 7.x，以及一个**独立准备、可搬移的 Windows x64 CPython 3.13 运行目录**，含 `python.exe`、`pythonw.exe`、`python313.dll`、`Lib` 和已安装的锁定依赖。不要直接使用带个人配置或开发资料的目录，也不要把普通 venv 当作可搬移运行环境。

```powershell
python scripts/build_desktop.py --runtime C:\build\python313-runtime --iscc "C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
```

脚本使用源码清单构造空白配置的安装素材，复制运行环境，生成 SHA-256 清单并编译。输出在 `dist/`，不进入 Git。更换依赖或运行环境后，应重新核对第三方许可证，并在隔离目录验证安装器。

升级安装包从临时目录运行自带 Python，可以替换旧版运行环境。升级前需要旧程序已停止；默认保留账号资料与旧配置。完整备份位于原程序 `backups/`，请保留。

## 发布流程

1. 运行 `python scripts/check_public_source.py`，确认没有运行数据和非模板配置。
2. 跑离线测试并记录结果；更改安装器后实测旧版升级、数据校验与回退。
3. 提交源码并创建版本标签；在 GitHub Releases 选择同一标签。
4. 上传安装包、校验文件和验证说明，并提供对应源码位置。

工作流在主分支测试通过后使用全新 Windows 环境构建，执行安装器冒烟测试，并创建带安装包及校验文件的发布草稿。首次使用 Fork 时，GitHub 可能要求先在 Actions 页面启用工作流；启用后可选择 Desktop offline tests → Run workflow。最终状态以 Actions 和 Releases 页面为准。草稿可在核对后发布；现有本地安装包不会因源码上传而自动成为 Release。

