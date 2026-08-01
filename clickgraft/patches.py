"""
clickgraft.patches — Uniform patch engine supporting replace, append, and json_set.
Enforces the anchor contract (exactly-once) and strict json_set path validation.
"""

import json


class PatchEngine:
    def __init__(self, manifest_patches):
        """
        manifest_patches is a list of patch dicts from the manifest:
        [
          {
            "path": "package.json",
            "why": "...",
            "ops": [ { "type": "json_set", "path": "hp_configs.crashAutoSubmit", "value": false } ]
          }, ...
        ]
        """
        self.patches_by_path = {}
        for p in manifest_patches:
            self.patches_by_path[p["path"]] = p.get("ops", [])

    def get_patched_paths(self):
        return list(self.patches_by_path.keys())

    def apply_patches_for_path(self, rel_path, orig_bytes):
        if rel_path not in self.patches_by_path:
            return orig_bytes

        ops = self.patches_by_path[rel_path]
        content_text = orig_bytes.decode("utf-8")

        for op in ops:
            op_type = op.get("type")
            if op_type == "replace":
                anchor = op["anchor"]
                replacement = op["replacement"]
                count = content_text.count(anchor)
                if count != 1:
                    raise ValueError(
                        f"Patch error in file '{rel_path}': anchor '{anchor}' occurred {count} times (expected exactly 1)"
                    )
                content_text = content_text.replace(anchor, replacement)

            elif op_type == "append":
                text_to_append = op["text"]
                content_text += text_to_append

            elif op_type == "json_set":
                try:
                    data = json.loads(content_text)
                except Exception as e:
                    raise ValueError(f"json_set error in '{rel_path}': content is not valid JSON: {e}")

                key_path = op["path"].split(".")
                val = op["value"]
                allow_create = op.get("create", False)

                curr = data
                for seg in key_path[:-1]:
                    if not isinstance(curr, dict) or seg not in curr:
                        raise ValueError(
                            f"json_set error in '{rel_path}': parent segment '{seg}' in path '{op['path']}' does not exist"
                        )
                    if not isinstance(curr[seg], dict):
                        raise ValueError(
                            f"json_set error in '{rel_path}': parent segment '{seg}' in path '{op['path']}' is not a dict"
                        )
                    curr = curr[seg]

                leaf_key = key_path[-1]
                if not isinstance(curr, dict):
                    raise ValueError(f"json_set error in '{rel_path}': target parent for '{leaf_key}' is not a dict")

                if leaf_key not in curr and not allow_create:
                    raise ValueError(
                        f"json_set error in '{rel_path}': leaf key '{leaf_key}' in path '{op['path']}' does not exist (set create=True to allow adding new keys)"
                    )

                curr[leaf_key] = val
                content_text = json.dumps(data, indent=2)

            else:
                raise ValueError(f"Unknown patch op type: '{op_type}' for path '{rel_path}'")

        return content_text.encode("utf-8")
