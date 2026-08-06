"""
One-time migration script: rewrites every `url_for('old_endpoint', ...)`
call in your Jinja templates to the new blueprint-qualified endpoint name
(e.g. 'dashboard' -> 'dashboard.index', 'employee_edit' -> 'employees.edit').

Usage:
    python fix_templates.py "C:\\Users\\lester.caton\\Desktop\\itsystem\\templates"

It edits files in place. Run it inside a git repo (or make a copy of the
templates folder first) so you can review/revert with `git diff` if
anything looks off.
"""

import re
import sys
from pathlib import Path

# old endpoint name -> new blueprint-qualified endpoint name
ENDPOINT_MAP = {
    "login": "auth.login",
    "logout": "auth.logout",

    "dashboard": "dashboard.index",
    "search": "dashboard.search",

    "employees_list": "employees.list_view",
    "employee_new": "employees.new",
    "employee_detail": "employees.detail",
    "employee_edit": "employees.edit",
    "employee_archive": "employees.archive",

    "assets_list": "assets.list_view",
    "asset_new": "assets.new",
    "asset_detail": "assets.detail",
    "asset_edit": "assets.edit",
    "asset_archive": "assets.archive",
    "asset_transfer": "assets.transfer",

    "workstations_list": "workstations.list_view",
    "workstation_new": "workstations.new",
    "workstation_detail": "workstations.detail",
    "workstation_edit": "workstations.edit",
    "workstation_archive": "workstations.archive",
    "workstation_transfer": "workstations.transfer",
    "workstation_assign_asset": "workstations.assign_asset",
    "workstation_unlink_asset": "workstations.unlink_asset",

    "accountabilities_list": "accountabilities.list_view",
    "accountability_new": "accountabilities.new",
    "accountability_detail": "accountabilities.detail",
    "accountability_close": "accountabilities.close",

    "remark_add": "remarks.add",

    "audits_list": "audits.list_view",
    "audit_new": "audits.new",
    "audit_trail": "audits.trail",

    "users_list": "users.list_view",
    "user_new": "users.new",
    "user_edit": "users.edit",

    "import_inventory": "io.import_inventory",
    "export_assets": "io.export_assets",
    "export_employees": "io.export_employees",
    "report_assets_pdf": "io.report_assets_pdf",
    "accountability_pdf": "io.accountability_pdf",
    "asset_stickers": "io.asset_stickers",
    "workstation_stickers": "io.workstation_stickers",

    "api_employees_search": "api.employees_search",
    "api_assets_search": "api.assets_search",
    "api_stats": "api.stats",
    "api_employees": "api.employees_all",
    "api_assets": "api.assets_all",
    "api_workstations": "api.workstations_all",
    "api_accountabilities": "api.accountabilities_all",
    "api_asset_detail": "api.asset_detail_api",
}


def build_pattern():
    # Matches url_for('old_name'  or  url_for("old_name  — captures the
    # quote char and old name so we can splice in the replacement while
    # leaving everything else (args, kwargs, trailing text) untouched.
    names = "|".join(re.escape(k) for k in ENDPOINT_MAP)
    return re.compile(r"""url_for\(\s*(['"])(""" + names + r""")\1""")


def replace_in_text(text, pattern):
    def _sub(m):
        quote, old_name = m.group(1), m.group(2)
        new_name = ENDPOINT_MAP[old_name]
        return f"url_for({quote}{new_name}{quote}"
    return pattern.subn(_sub, text)


def main():
    if len(sys.argv) != 2:
        print("Usage: python fix_templates.py <path-to-templates-folder>")
        sys.exit(1)

    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"Not a directory: {root}")
        sys.exit(1)

    pattern = build_pattern()
    total_files_changed = 0
    total_replacements = 0

    for html_file in root.rglob("*.html"):
        text = html_file.read_text(encoding="utf-8")
        new_text, count = replace_in_text(text, pattern)
        if count:
            html_file.write_text(new_text, encoding="utf-8")
            total_files_changed += 1
            total_replacements += count
            print(f"  {html_file.relative_to(root)}: {count} replacement(s)")

    print(f"\nDone. {total_replacements} url_for() call(s) updated across {total_files_changed} file(s).")
    print("Review with `git diff` before committing.")


if __name__ == "__main__":
    main()