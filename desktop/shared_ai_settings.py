"""One encrypted AI configuration; legacy account files are retained for rollback."""
import json
from types import SimpleNamespace

from quiz_assistant import QuizError, read_legacy_settings, save_settings


def shared_profile(root):
    return SimpleNamespace(directory=root, name="所有账号共用")


def legacy_candidates(store):
    rows = []
    for profile in store.list():
        try:
            settings = read_legacy_settings(profile)
            if settings["endpoint"] and settings["model"] and settings["key"]:
                rows.append((profile, settings))
        except QuizError:
            continue
    return rows


def migrate_shared_settings(store):
    path = store.root / "ai_quiz.json"
    if path.exists():
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("migration_pending") is True:
                return "choose"
        except (OSError, ValueError, AttributeError):
            pass
        return "existing"
    rows = legacy_candidates(store)
    enabled = [row for row in rows if row[1]["enabled"]]
    usable = enabled or rows
    identities = {(row[1]["enabled"], row[1]["endpoint"].rstrip("/"), row[1]["model"], row[1]["key"])
                  for row in usable}
    if len(identities) == 1:
        save_settings(shared_profile(store.root), usable[0][1])
        return "migrated"
    if len(identities) > 1:
        # No account's provider/key is chosen silently when multiple differ.
        path.write_text(json.dumps({"enabled": False, "endpoint": "", "model": "",
                                    "protected_key": "", "migration_pending": True}), encoding="utf-8")
        return "choose"
    return "empty"
