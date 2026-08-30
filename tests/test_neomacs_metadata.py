from pathlib import Path
import plistlib
import re
import unittest
from xml.sax.saxutils import escape


TEMPLATE = Path(__file__).resolve().parents[1] / "packages" / "neomacs-Info.plist.in"


def render_metadata(template, app_version="0.8.3", upstream_version="0.8.3-test"):
    replacements = {
        "appVersion": app_version,
        "upstreamVersion": upstream_version,
    }
    for name, value in replacements.items():
        template = template.replace(
            "@" + name + "@", escape(value, {'"': "&quot;", "'": "&apos;"})
        )
    return plistlib.loads(template.encode("utf-8"))


class NeomacsMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.metadata = render_metadata(cls.template)
        cls.documents = cls.metadata["CFBundleDocumentTypes"]

    def test_template_has_only_expected_substitutions(self):
        self.assertEqual(
            set(re.findall(r"@([A-Za-z][A-Za-z0-9]*)@", self.template)),
            {"appVersion", "upstreamVersion"},
        )
        self.assertEqual(self.template.count("@appVersion@"), 2)
        self.assertEqual(self.template.count("@upstreamVersion@"), 1)

    def test_bundle_identity_and_platform_metadata(self):
        expected = {
            "CFBundleName": "Neomacs",
            "CFBundleDisplayName": "Neomacs",
            "CFBundleIdentifier": "org.neomacs.nix",
            "CFBundleExecutable": "neomacs-launcher",
            "CFBundlePackageType": "APPL",
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleIconFile": "neomacs",
            "LSMinimumSystemVersion": "12.0",
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(self.metadata[key], value)
        self.assertIs(self.metadata["NSHighResolutionCapable"], True)
        self.assertIs(self.metadata["LSUIElement"], True)

    def test_version_substitutions_preserve_xml_characters(self):
        self.assertEqual(self.metadata["CFBundleVersion"], "0.8.3")
        self.assertEqual(self.metadata["CFBundleShortVersionString"], "0.8.3")
        self.assertEqual(
            self.metadata["CFBundleGetInfoString"], "Neomacs 0.8.3-test (Nix)"
        )
        app_version = '1.2.3<&"\''
        upstream_version = 'development<&"\''
        metadata = render_metadata(self.template, app_version, upstream_version)
        self.assertEqual(metadata["CFBundleVersion"], app_version)
        self.assertEqual(metadata["CFBundleShortVersionString"], app_version)
        self.assertEqual(
            metadata["CFBundleGetInfoString"], "Neomacs " + upstream_version + " (Nix)"
        )

    def test_three_document_types_are_alternate_handlers(self):
        self.assertEqual(len(self.documents), 3)
        self.assertEqual(
            [document["CFBundleTypeRole"] for document in self.documents],
            ["Editor", "Editor", "Viewer"],
        )
        for document in self.documents:
            with self.subTest(name=document["CFBundleTypeName"]):
                self.assertTrue(document["CFBundleTypeName"])
                self.assertEqual(document["LSHandlerRank"], "Alternate")

    def test_generic_editor_is_limited_to_text(self):
        self.assertEqual(self.documents[0]["LSItemContentTypes"], ["public.text"])
        self.assertNotIn("CFBundleTypeExtensions", self.documents[0])

    def test_explicit_extensions_are_not_shadowed_by_content_types(self):
        document = self.documents[1]
        self.assertNotIn("LSItemContentTypes", document)
        extensions = document["CFBundleTypeExtensions"]
        self.assertEqual(extensions, sorted(set(extensions)))
        for extension in extensions:
            with self.subTest(extension=extension):
                self.assertRegex(extension, r"^[a-z0-9][a-z0-9+]*$")

    def test_core_text_and_source_formats_have_explicit_extensions(self):
        extensions = set(self.documents[1]["CFBundleTypeExtensions"])
        expected = {
            "text", "txt", "html", "css", "xml", "xsl", "yml",
            "markdown", "mkdn", "md", "mkd", "mdown", "nix", "yaml",
            "toml", "rs", "go", "ts", "tsx", "jsx", "zsh", "bash",
            "fish", "ini", "conf", "log", "csv", "tsv", "js", "py",
            "rb", "sh", "c", "h", "cpp", "hpp", "java", "el", "org",
            "json", "tex", "bib", "texi", "hs", "lhs", "lua", "php",
            "tcl", "pl", "f90", "pas", "ada",
        }
        self.assertFalse(expected - extensions, sorted(expected - extensions))

    def test_unknown_and_extensionless_files_are_viewer_only(self):
        fallback = self.documents[2]
        self.assertEqual(fallback["CFBundleTypeRole"], "Viewer")
        self.assertEqual(fallback["LSItemContentTypes"], ["public.data"])
        self.assertNotIn("CFBundleTypeExtensions", fallback)

    def test_no_universal_editor_or_private_type_ownership(self):
        self.assertNotIn("UTExportedTypeDeclarations", self.metadata)
        self.assertNotIn("UTImportedTypeDeclarations", self.metadata)
        content_types = {
            content_type
            for document in self.documents
            for content_type in document.get("LSItemContentTypes", [])
        }
        self.assertEqual(content_types, {"public.text", "public.data"})
        for document in self.documents:
            self.assertNotIn("CFBundleTypeOSTypes", document)
            self.assertNotIn("*", document.get("CFBundleTypeExtensions", []))
            self.assertNotIn("", document.get("CFBundleTypeExtensions", []))
            if document["CFBundleTypeRole"] == "Editor":
                self.assertTrue(
                    set(document.get("LSItemContentTypes", [])) <= {"public.text"}
                )


if __name__ == "__main__":
    unittest.main()
