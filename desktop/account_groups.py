"""Atomic account organization, independent of credentials and task state."""
import copy
import json
import uuid
from pathlib import Path


UNGROUPED = "ungrouped"


class AccountGroups:
    def __init__(self, root):
        self.path = Path(root) / "account_groups.json"
        self.data = {"version": 1, "groups": [], "accounts": {}, "collapsed": []}
        self.error = ""
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                assert value["version"] == 1 and isinstance(value["groups"], list)
                assert isinstance(value["accounts"], dict) and isinstance(value["collapsed"], list)
                ids, names = set(), {"未分类"}
                for group in value["groups"]:
                    assert isinstance(group["id"], str) and len(group["id"]) == 32
                    assert group["id"] not in ids
                    assert isinstance(group["name"], str) and group["name"].strip() and len(group["name"]) <= 40
                    assert group["name"].casefold() not in names
                    ids.add(group["id"]); names.add(group["name"].casefold())
                assert all(isinstance(key, str) and item in ids for key, item in value["accounts"].items())
                assert all(item in ids | {UNGROUPED} for item in value["collapsed"])
                self.data = value
            except (OSError, UnicodeError, ValueError, KeyError, TypeError, AssertionError):
                self.error = "账号分类记录无法读取，原文件已保留；当前按未分类显示。"

    def groups(self):
        return [{"id": UNGROUPED, "name": "未分类"}, *copy.deepcopy(self.data["groups"])]

    def group_for(self, profile_id):
        return self.data["accounts"].get(profile_id, UNGROUPED)

    def save(self, value):
        if self.error:
            raise ValueError(self.error)
        temporary = self.path.with_name(".account-groups-" + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.data = value

    def name(self, name, excluding=None):
        name = str(name).strip()
        if not name or len(name) > 40 or any(ord(char) < 32 for char in name):
            raise ValueError("分类名称需为 1 至 40 个字符。")
        if any(group["name"].casefold() == name.casefold() and group["id"] != excluding for group in self.groups()):
            raise ValueError("这个分类名称已经存在。")
        return name

    def create(self, name):
        value, key = copy.deepcopy(self.data), uuid.uuid4().hex
        value["groups"].append({"id": key, "name": self.name(name)})
        self.save(value)
        return key

    def rename(self, key, name):
        value = copy.deepcopy(self.data)
        group = next((row for row in value["groups"] if row["id"] == key), None)
        if group is None:
            raise ValueError("请选择一个自建分类；“未分类”不能重命名。")
        group["name"] = self.name(name, key)
        self.save(value)

    def remove(self, key):
        if key == UNGROUPED or not any(group["id"] == key for group in self.data["groups"]):
            raise ValueError("请选择一个自建分类。")
        value = copy.deepcopy(self.data)
        value["groups"] = [group for group in value["groups"] if group["id"] != key]
        value["accounts"] = {account: group for account, group in value["accounts"].items() if group != key}
        value["collapsed"] = [group for group in value["collapsed"] if group != key]
        self.save(value)

    def assign(self, account, key):
        if not any(group["id"] == key for group in self.groups()):
            raise ValueError("目标分类不存在。")
        value = copy.deepcopy(self.data)
        if key == UNGROUPED:
            value["accounts"].pop(account, None)
        else:
            value["accounts"][account] = key
        self.save(value)

    def collapse(self, key, collapsed):
        value = copy.deepcopy(self.data)
        current = set(value["collapsed"])
        updated = (current | {key}) if collapsed else (current - {key})
        if updated != current:
            value["collapsed"] = sorted(updated)
            self.save(value)
