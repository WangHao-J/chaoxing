"""Run upstream code inside a single isolated account directory."""
import argparse
import json
import os
import sys
from pathlib import Path

from account_store import AccountStore, AlreadyRunning, ProfileLock, selected_courses
from password_dialog import ask_password
from runtime_safety import install_runtime_safety, RunNeedsAttention, IncompleteRun
from resume_tasks import ResumeStore


def prepare(root, profile):
    os.chdir(profile.directory)
    os.environ["TEMP"] = os.environ["TMP"] = str(profile.directory / "temp")
    os.environ["PYTHONUTF8"] = "1"
    sys.path.insert(0, str(root / "app"))
    import main
    return main


def configure(main, profile, fresh_login, choose_courses=False):
    original_init_config = main.init_config
    original_init_chaoxing = main.init_chaoxing

    def user_input(prompt=""):
        if "密码" in prompt:
            print("请在弹出的密码窗口中输入密码，点击“确认”继续。")
            return ask_password(profile.name)
        return input(prompt)

    main.input = user_input

    def init_config():
        common, tiku, notification, path = original_init_config()
        if fresh_login:
            common["use_cookies"] = False
        elif (profile.directory / "cookies.txt").is_file() and not common.get("username"):
            common["use_cookies"] = True
        return common, tiku, notification, path

    def init_chaoxing(*args, **kwargs):
        cx = original_init_chaoxing(*args, **kwargs)
        original_login = cx.login

        def login(login_with_cookies=False):
            result = original_login(login_with_cookies=login_with_cookies)
            if not result.get("status") and login_with_cookies:
                print("保存的登录状态已失效，请重新登录此账户。")
                cx.account = main.Account(input("学习通手机号：").strip(), ask_password(profile.name))
                result = original_login(login_with_cookies=False)
            return result

        cx.login = login
        return cx

    def filter_courses(courses, requested):
        if not courses:
            raise main.InputFormatError("没有找到可处理的课程")
        resume = ResumeStore(profile)
        chosen = None
        if choose_courses:
            requested = None
        elif not requested:
            try:
                chosen = resume.select(courses)
            except ValueError as error:
                raise main.InputFormatError(str(error)) from error
            if chosen:
                print(f"已恢复上次选择的 {len(chosen)} 个课程班级，将从待处理章节继续。")
        if not requested and chosen is None:
            print("\n课程列表：")
            for course in courses:
                print(f"ID: {course['courseId']}  班级: {course['clazzId']}  {course['title']}")
            requested = input("输入课程 ID，多门用英文逗号分隔；输入 * 选择全部：\n").split(",")
        try:
            if chosen is None:
                chosen = selected_courses(courses, requested)
            resume.save_selection(chosen, reset=choose_courses)
            return chosen
        except ValueError as error:
            raise main.InputFormatError(str(error)) from error

    main.init_config = init_config
    main.init_chaoxing = init_chaoxing
    main.filter_courses = filter_courses


def check_profile(root, profile, main):
    from api.config import GlobalConst
    from api.answer import CacheDAO, Tiku
    from api.cxsecret_font import resource_path
    from api.notification import Notification
    paths = {
        "config": str(Path(Tiku.CONFIG_PATH).resolve()),
        "cookies": str(Path(GlobalConst.COOKIES_PATH).resolve()),
        "log": str(Path("chaoxing.log").resolve()),
        "cache": str(Path(CacheDAO.DEFAULT_CACHE_FILE).resolve()),
        "font_resource": str(Path(resource_path("resource/font_map_table.json")).resolve()),
        "notification_config": str(Path(Notification.CONFIG_PATH).resolve()),
        "temp": os.environ["TEMP"],
    }
    assert all(Path(path).is_relative_to(profile.directory) for path in paths.values())
    assert Path(paths["font_resource"]).is_file()
    common, _, _ = main.load_config_from_file(paths["config"])
    assert common["jobs"] >= 1
    print(json.dumps({"profile": profile.id, "paths": paths, "shared_source": main.__file__,
                      "python": sys.executable, "isolated": True}, ensure_ascii=False))


def entry(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--fresh-login", action="store_true")
    parser.add_argument("--choose-courses", action="store_true")
    args = parser.parse_args(argv)
    code = 0
    safety = None
    try:
        root = args.root.resolve()
        profile = AccountStore(root).get(args.profile)
        with ProfileLock(profile.directory):
            if os.name == "nt" and not args.check:
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW("学习通 · " + profile.name)
            main = prepare(root, profile)
            if args.check:
                check_profile(root, profile, main)
            else:
                print(f"当前账户：{profile.name}\n")
                configure(main, profile, args.fresh_login, args.choose_courses)
                safety = install_runtime_safety(main, profile)
                sys.argv = [str(root / "app" / "main.py"), "-c", str(profile.directory / "config.ini")]
                setup_retries = 0
                while True:
                    safety.issue = None
                    try:
                        main.main()
                        if not safety.completed:
                            raise IncompleteRun("本次运行未全部完成，请重新启动后继续。")
                        break
                    except (RunNeedsAttention, IncompleteRun):
                        raise
                    except Exception:
                        if safety.issue is None:
                            raise
                        from types import SimpleNamespace
                        task = SimpleNamespace(point={"title": "登录或读取课程"})
                        action = safety.recover(safety.issue, task, setup_retries)
                        if action != "retry" or setup_retries >= 2:
                            raise RunNeedsAttention("请处理验证或在管理界面重新登录后，再启动当前账户。") from None
                        setup_retries += 1
                        safety.state("running", "正在重新检查登录状态或读取课程")
    except AlreadyRunning as error:
        print(error)
        code = 2
    except KeyboardInterrupt:
        print("运行已停止。")
        code = 130
    except (RunNeedsAttention, IncompleteRun) as error:
        print(str(error))
        code = 3
    except Exception as error:
        print(f"运行失败：{error}")
        code = 1
    finally:
        if safety is not None and code != 0 and getattr(safety, "current_status", "") != "needs_attention":
            safety.state("stopped" if code == 130 else "needs_attention", "运行未完成，待处理章节仍保留")
        if not args.check and code != 3 and sys.stdin.isatty():
            try:
                input("\n按回车关闭这个运行窗口……")
            except (EOFError, KeyboardInterrupt):
                pass
    return code


if __name__ == "__main__":
    raise SystemExit(entry())
