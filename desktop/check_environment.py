import json
import sys
from pathlib import Path
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "app"))
import main
from api.answer import Tiku
from api.notification import Notification

root = Path(__file__).resolve().parent
locations = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "source": main.__file__,
}
assert all(Path(value).resolve().is_relative_to(root) for value in locations.values()), locations
common, tiku_config, notify_config = main.load_config_from_file("config.ini")
assert common["username"] == common["password"] == ""
assert common["jobs"] == 1 and common["speed"] == 1.0
assert common["work_redo_enabled"] is False
assert common["add_learning_count"] is False
bank = Tiku.get_tiku_from_config(tiku_config, config_path="config.ini")
bank.init_tiku()
assert bank.DISABLE
notification = Notification()
notification.config_set(notify_config)
notification = notification.get_notification_from_config()
notification.init_notification()
assert notification.disabled
print(json.dumps({"python": sys.version.split()[0], "locations": locations, "defaults_verified": True}, ensure_ascii=False, indent=2))