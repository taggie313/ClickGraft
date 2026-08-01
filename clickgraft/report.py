"""
clickgraft.report — Human-readable and JSON reporting formatting.
"""

import json


def format_human_report(title, data_dict):
    lines = [
        f"============================================================",
        f"  {title}",
        f"============================================================"
    ]

    for key, val in data_dict.items():
        if isinstance(val, dict):
            lines.append(f"\n[{key}]")
            for sub_k, sub_v in val.items():
                lines.append(f"  - {sub_k}: {sub_v}")
        elif isinstance(val, list):
            lines.append(f"\n[{key}]")
            for item in val:
                lines.append(f"  - {item}")
        else:
            lines.append(f"  - {key}: {val}")

    lines.append("============================================================")
    return "\n".join(lines)


def format_json_report(data_dict):
    return json.dumps(data_dict, indent=2)
