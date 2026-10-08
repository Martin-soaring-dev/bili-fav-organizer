"""作者署名与许可信息必须留在程序、发布包与文档里。

防的是"改名重打包后抹掉来源"：顶栏署名、exe 属性、发布包内的许可全文
和文档说明各自独立，去掉任何一处都要显式改代码或改构建脚本。

仓库文件部分是静态检查；只有 /api/version 需要导入应用，因此在导入前
才把数据目录指向测试目录，并且只在自己首个导入时生效，避免影响其它测试。
"""
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
STYLE = ROOT / "static" / "style.css"
APP_JS = ROOT / "static" / "app.js"
SPEC = ROOT / "BiliFavOrganizer.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "release-windows.yml"
MANUAL = ROOT / "packaging" / "使用说明.txt"
README = ROOT / "README.md"
SERVER = ROOT / "server.py"
LICENSE_FILE = ROOT / "LICENSE"
NOTICE_FILE = ROOT / "NOTICE"

AUTHOR = "Martin-soaring-dev"
HOMEPAGE = f"https://github.com/{AUTHOR}/bili-fav-organizer"
LICENSE_NAME = "PolyForm Noncommercial License 1.0.0"


def _client():
    """导入应用并返回测试客户端；导入前把数据目录隔离到测试目录。"""
    if "server" not in sys.modules:
        test_root = Path(__file__).resolve().parent / ".test-data" / "attribution"
        test_root.mkdir(parents=True, exist_ok=True)
        os.environ["BILI_FAV_ORGANIZER_DATA_DIR"] = str(test_root)
        os.environ["LOCALAPPDATA"] = str(test_root)
        settings = test_root / "BiliFavOrganizer"
        settings.mkdir(exist_ok=True)
        for filename in ("config.json", "secrets.json"):
            (settings / filename).write_text("{}", encoding="utf-8")
        # 预置空库：否则 store 会把开发者的旧数据目录迁移/导入进来。
        with sqlite3.connect(test_root / "library.sqlite3") as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations"
                         "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(6, 'test')")
    import server
    from fastapi.testclient import TestClient

    return TestClient(server.app)


class ApiVersionTests(unittest.TestCase):
    def test_version_endpoint_exposes_author_source_and_license(self):
        body = _client().get("/api/version").json()
        self.assertEqual(AUTHOR, body["author"])
        self.assertEqual(HOMEPAGE, body["homepage"])
        self.assertEqual(LICENSE_NAME, body["license"])
        self.assertIn(AUTHOR, body["copyright"])
        for field in ("version", "commit", "build_date"):
            self.assertIn(field, body, f"/api/version must report {field}")

    def test_build_markers_stay_replaceable_by_the_release_workflow(self):
        text = SERVER.read_text(encoding="utf-8")
        for marker in ('BUILD_VERSION = "dev"', 'BUILD_COMMIT = ""', 'BUILD_DATE = ""'):
            self.assertEqual(1, text.count(marker),
                             f"{marker} must appear exactly once for the workflow to replace it")


class TopbarCreditTests(unittest.TestCase):
    def test_topbar_links_the_author_to_the_repository(self):
        html = INDEX.read_text(encoding="utf-8")
        block = re.search(r'<div class="brand">(.*?)</div>', html, re.S)
        self.assertIsNotNone(block, "topbar .brand block not found")
        credit = block.group(1)
        self.assertIn(f'href="{HOMEPAGE}"', credit)
        self.assertIn(AUTHOR, credit)
        self.assertIn('rel="noopener noreferrer"', credit)

    def test_credit_and_version_share_the_same_css_slot(self):
        css = STYLE.read_text(encoding="utf-8")
        self.assertRegex(css, r"\.brand-credit\s*{", "style.css must style .brand-credit")

    def test_version_label_appends_the_build_commit(self):
        script = APP_JS.read_text(encoding="utf-8")
        self.assertIn("info.commit", script, "the topbar must show which commit the build came from")


class ExecutableAttributionTests(unittest.TestCase):
    def test_spec_writes_company_name_and_copyright_into_the_exe(self):
        spec = SPEC.read_text(encoding="utf-8")
        self.assertRegex(spec, r'AUTHOR\s*=\s*"Martin-soaring-dev"')
        self.assertIn('StringStruct("CompanyName", AUTHOR)', spec)
        self.assertIn('StringStruct("LegalCopyright", COPYRIGHT)', spec)
        self.assertIn("version=VERSION_INFO", spec)

    def test_release_workflow_embeds_build_info_and_ships_license_and_notice(self):
        flow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("'BUILD_COMMIT = \"\"'", flow)
        self.assertIn("'BUILD_DATE = \"\"'", flow)
        self.assertIn('Copy-Item "LICENSE"', flow)
        self.assertIn('Copy-Item "NOTICE"', flow)


class LicenseFileTests(unittest.TestCase):
    def test_license_is_polyform_noncommercial_not_creative_commons(self):
        text = LICENSE_FILE.read_text(encoding="utf-8")
        self.assertIn("PolyForm Noncommercial License 1.0.0", text)
        self.assertIn("Noncommercial Purposes", text)
        self.assertIn("Patent License", text)
        self.assertNotIn("Creative Commons", text,
                         "the license text must be the PolyForm original, not the replaced CC one")

    def test_notice_carries_the_required_notice_line_and_brand_statement(self):
        text = NOTICE_FILE.read_text(encoding="utf-8")
        required = [line for line in text.splitlines() if line.startswith("Required Notice:")]
        self.assertEqual(1, len(required), "NOTICE must carry exactly one Required Notice: line")
        self.assertIn(AUTHOR, required[0])
        self.assertIn(HOMEPAGE, required[0])
        self.assertIn("BiliFav Organizer", text)


class DocumentedNoticeTests(unittest.TestCase):
    def test_manual_states_author_source_and_license(self):
        text = MANUAL.read_text(encoding="utf-8")
        self.assertIn(AUTHOR, text)
        self.assertIn(HOMEPAGE, text)
        self.assertIn(LICENSE_NAME, text)
        self.assertIn("LICENSE.txt", text, "the manual must point at the packaged license file")
        self.assertIn("NOTICE.txt", text, "the manual must point at the packaged notice file")
        self.assertNotIn("CC BY-NC", text, "the manual must not keep the replaced license")

    def test_readme_states_author_and_keeps_the_license_notice(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("© 2026", text)
        self.assertIn(AUTHOR, text)
        self.assertIn(LICENSE_NAME, text)
        self.assertIn(HOMEPAGE, text)


if __name__ == "__main__":
    main()
