# 构建和发布

## 测试

Windows / Python 3.13，先安装 `desktop/requirements.txt`：

```powershell
cd desktop
python -B -m unittest discover -q
```

测试使用合成账号和模拟响应，不执行真实课程或真实交卷。Tkinter 测试需要可用的 Windows 桌面会话。

## 构建完整安装包

需要 Inno Setup 6.7+ 或 7.x，以及一个**独立准备、可搬移的 Windows x64 CPython 3.13 运行目录**，含 `python.exe`、`pythonw.exe`、`python313.dll`、`Lib` 和已安装的锁定依赖。不要直接使用带个人配置或开发资料的目录，也不要把普通 venv 当作可搬移运行环境。

```powershell
python scripts/build_desktop.py --runtime C:\build\python313-runtime --iscc "C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
```

脚本使用源码清单构造空白配置的安装素材，复制运行环境，生成 SHA-256 清单并编译。输出在 `dist/`，不进入 Git。更换依赖或运行环境后，应重新核对第三方许可证，并在隔离目录验证安装器。

普通安装包直接安装到用户选择的目录，可用于空目录。安装器包含运行环境，不要求已安装旧版本，没有升级或回退操作。已有 `app/config.ini` 使用 `onlyifdoesntexist` 保留，并且卸载时不删除。用户运行数据不在安装文件清单中。

构建后执行实际安装器验证：

```powershell
python scripts/smoke_setup.py --installer dist/Chaoxing-Desktop-Setup-1.7.7-20261102-win64.exe --manifest dist/setup-build/setup-manifest.json --output dist/setup-verification.json
```

它会在独立测试目录中验证新装、内置 Python/Tk、离线检查、重复安装及卸载后资料保留。历史升级模板和回退代码仅供旧版本维护，当前默认构建不会包含或执行它们。

## 发布流程

1. 运行 `python scripts/check_public_source.py`，确认没有运行数据和非模板配置。
2. 跑适用的离线测试并记录结果；更改安装器后实测新装、重复安装、资料保留和卸载。
3. 提交源码并创建版本标签；在 GitHub Releases 选择同一标签。
4. 上传安装包、校验文件和验证说明，并提供对应源码位置。

工作流在主分支测试通过后使用全新 Windows 环境构建，执行安装器冒烟测试，并创建带安装包及校验文件的发布草稿。首次使用 Fork 时，GitHub 可能要求先在 Actions 页面启用工作流；启用后可选择 Desktop offline tests → Run workflow。最终状态以 Actions 和 Releases 页面为准。草稿可在核对后发布；现有本地安装包不会因源码上传而自动成为 Release。

