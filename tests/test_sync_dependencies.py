"""harbor/environments/dependencies.py: whether changed content needs settings that are not synced yet"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.environments import dependencies  # noqa: E402

PAGE, COMPOSITION, TEXT, NEW_TYPE, TEMPLATE, CAMPAIGN, ELEMENT, BLOCKS = (
    "aaaaaaaa-0000-0000-0000-00000000000" + str(index) for index in range(1, 9))


def content_type(key, alias, name, compositions=(), definitions=()):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ContentType Key="{key}" Alias="{alias}" Level="1">
  <Info><Name>{name}</Name><Compositions>{''.join(f'<Composition Key="{item}">c</Composition>' for item in compositions)}</Compositions></Info>
  <GenericProperties>{''.join(f'<GenericProperty><Alias>p{index}</Alias><Definition>{item}</Definition></GenericProperty>' for index, item in enumerate(definitions))}</GenericProperties>
</ContentType>"""


def data_type(key, name, config="{}"):
    return f'<?xml version="1.0" encoding="utf-8"?>\n<DataType Key="{key}" Alias="{name}" Level="1"><Info><Name>{name}</Name></Info><Config><![CDATA[{config}]]></Config></DataType>'


def template(key, name):
    return f'<?xml version="1.0" encoding="utf-8"?>\n<Template Key="{key}" Alias="{name}" Level="1"><Name>{name}</Name></Template>'


def page(name, alias, template_key, value="Hello"):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Content Key="bbbbbbbb-0000-0000-0000-{abs(hash(name)) % 10**12:012d}" Alias="{name}" Level="1">
  <Info><ContentType>{alias}</ContentType><NodeName Default="{name}" /><Template Key="{template_key}">t</Template></Info>
  <Properties><text><Value><![CDATA[{value}]]></Value></text></Properties>
</Content>"""


class DependencyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.site = Path(folder.name)
        self.write("ContentTypes/page.config", content_type(PAGE, "page", "Page", [COMPOSITION], [TEXT]))
        self.write("ContentTypes/composition.config", content_type(COMPOSITION, "composition", "SEO"))
        self.write("DataTypes/text.config", data_type(TEXT, "Text"))
        self.write("Templates/page.config", template(TEMPLATE, "Page"))
        self.write("Content/home.config", page("Home", "page", TEMPLATE))
        # As it was synced
        self.settings_sent = dependencies.manifest(self.site, ["settings"])
        self.data_sent = dependencies.manifest(self.site, ["content"])

    def write(self, name, text):
        path = self.site / "uSync/v18" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def reasons(self):
        return dependencies.reasons(self.site, self.site, self.settings_sent, self.data_sent, ["content"], "Production")

    def test_nothing_changed(self):
        self.assertEqual(self.reasons(), [])

    def test_a_text_and_a_data_type_nobody_uses(self):
        """The two have nothing to do with each other: the data may go without the settings"""
        self.write("Content/home.config", page("Home", "page", TEMPLATE, "Welcome"))
        self.write("DataTypes/new.config", data_type(NEW_TYPE, "Colour"))
        self.assertEqual(self.reasons(), [])

    def test_a_page_on_a_new_template(self):
        self.write("Templates/campaign.config", template(CAMPAIGN, "Campaign"))
        self.write("Content/offer.config", page("Offer", "page", CAMPAIGN))
        self.assertEqual(self.reasons(), ["Offer uses the template Campaign, which has changed since it was synced to Production"])

    def test_a_changed_data_type_a_changed_page_uses_through_its_type(self):
        self.write("DataTypes/text.config", data_type(TEXT, "Text", '{"maxChars": 80}'))
        self.write("Content/home.config", page("Home", "page", TEMPLATE, "Welcome"))
        self.assertEqual(self.reasons(), ["Home uses the data type Text, which has changed since it was synced to Production"])

    def test_a_changed_composition_counts_too(self):
        self.write("ContentTypes/composition.config", content_type(COMPOSITION, "composition", "SEO", definitions=[TEXT]))
        self.write("Content/home.config", page("Home", "page", TEMPLATE, "Welcome"))
        self.assertEqual(len(self.reasons()), 1)

    def test_a_changed_setting_an_unchanged_page_uses_holds_nothing_back(self):
        """Only content that changed is sent: what is already there is not touched"""
        self.write("DataTypes/text.config", data_type(TEXT, "Text", '{"maxChars": 80}'))
        self.assertEqual(self.reasons(), [])

    def test_an_element_type_in_a_block_value(self):
        self.write("ContentTypes/element.config", content_type(ELEMENT, "banner", "Banner"))
        self.write("Content/home.config", page("Home", "page", TEMPLATE, f'{{"contentData":[{{"contentTypeKey":"{ELEMENT}"}}]}}'))
        self.assertEqual(self.reasons(), ["Home uses the document type Banner, which has changed since it was synced to Production"])

    def test_an_element_type_through_a_block_list(self):
        self.write("DataTypes/blocks.config", data_type(BLOCKS, "Blocks", f'{{"blocks":[{{"contentElementTypeKey":"{ELEMENT}"}}]}}'))
        self.write("ContentTypes/page.config", content_type(PAGE, "page", "Page", [COMPOSITION], [TEXT, BLOCKS]))
        self.settings_sent = dependencies.manifest(self.site, ["settings"])
        self.write("ContentTypes/element.config", content_type(ELEMENT, "banner", "Banner"))
        self.write("Content/home.config", page("Home", "page", TEMPLATE, "Welcome"))
        self.assertEqual(self.reasons(), ["Home uses the document type Banner, which has changed since it was synced to Production"])

    def test_not_known_without_what_was_sent(self):
        self.assertIsNone(dependencies.reasons(self.site, self.site, None, {}, ["content"], "Production"))


if __name__ == "__main__":
    unittest.main()
